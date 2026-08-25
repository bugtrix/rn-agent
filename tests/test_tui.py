"""The interactive terminal, tested without a terminal.

Every interactive surface here is split into a state machine and a thin
prompt_toolkit shell, precisely so the behaviour can be tested: navigation and
filtering in the picker, dispatch in the router, and what a provider or model
switch does to the session. The rules under test are the ones a developer would
notice if they broke - a switch that loses the conversation, a picker that can
select a disconnected model, a slash command that quietly reimplements the CLI.
"""

from __future__ import annotations

import json

import pytest

from rn_agent.agents.intent import Intent, detect
from rn_agent.ai.models import ModelInfo, ModelRegistry, ModelSource
from rn_agent.auth.authenticator import AuthMethod
from rn_agent.auth.manager import AuthenticationManager
from rn_agent.tui import chrome, handlers
from rn_agent.tui.dialogs import Action, choose
from rn_agent.tui.palette import open_palette
from rn_agent.tui.router import CommandRouter, parse_flags
from rn_agent.tui.select import Choice, Selector, fuzzy_rank, select
from rn_agent.tui.session import SessionManager
from rn_agent.tui.wizard import migrate as run_wizard

KEY = "sk-ant-test-0123456789abcdef"


class Backend:
    name = "file"
    label = "test backend"
    secure = True

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self.items.get(account)

    def set(self, account: str, secret: str) -> None:
        self.items[account] = secret

    def delete(self, account: str) -> bool:
        return self.items.pop(account, None) is not None


def build_session(project, tmp_path, *, connect: str | None = "anthropic") -> SessionManager:
    """A session on a scanned project, with an optional connected provider."""
    backend = Backend()
    manager = AuthenticationManager(backend=backend)
    if connect:
        manager.for_provider(connect).login(secret=KEY)
    context = project.scanned(command="terminal")
    context.config.ai.provider = connect
    context.config.ai.model = "claude-sonnet-4-5" if connect == "anthropic" else None
    context.__dict__["auth"] = manager
    return SessionManager(
        context=context,
        auth=manager,
        registry=ModelRegistry(cache_file=tmp_path / "model-cache.json"),
    )


def picker_for(value: str | None):
    """A picker that answers with ``value`` and records what it was shown."""
    seen: dict[str, list[Choice]] = {}

    def picker(title, choices, **kwargs):
        seen.setdefault("choices", list(choices))
        seen["title"] = title  # type: ignore[assignment]
        if value is None:
            return None
        return next((choice for choice in choices if choice.value == value), None)

    picker.seen = seen  # type: ignore[attr-defined]
    return picker


def queue_catalogues(transport, *catalogues: tuple[str, ...]) -> None:
    """Serve one catalogue per provider that will actually be asked.

    Only *connected* providers are queried, and in registry order: the fixture
    connects Anthropic (a key) and Ollama is always reachable (no credential),
    so two catalogues cover a full picker open. Distinct ids per provider matter
    - the same id in two groups is genuinely ambiguous, and `/model` refuses it.
    """
    for ids in catalogues:
        transport.queue(
            body={
                "data": [{"id": model} for model in ids],
                "models": [{"name": model} for model in ids],
            }
        )


#: The catalogues a full picker open needs: Anthropic, then Ollama.
ANTHROPIC_MODELS = ("claude-sonnet-4-5", "claude-opus-4-1")
OLLAMA_MODELS = ("llama3.2",)


def queue_models(transport, *ids: str) -> None:
    """Shorthand for the common case: Anthropic's catalogue, then Ollama's."""
    queue_catalogues(transport, ids or ANTHROPIC_MODELS, OLLAMA_MODELS)


# ---------------------------------------------------------------------------
# the picker
# ---------------------------------------------------------------------------
def choices() -> list[Choice]:
    return [
        Choice("claude-sonnet-4-5", "Claude Sonnet", hint="anthropic", group="Anthropic", current=True),
        Choice("claude-opus-4-1", "Claude Opus", hint="anthropic", group="Anthropic"),
        Choice("gpt-5", "GPT-5", hint="openai", group="Other", disabled=True, note="openai not connected"),
        Choice("gemini-2.5-flash", "Gemini Flash", hint="google", group="Other"),
    ]


def test_the_picker_opens_on_the_current_row():
    state = Selector(title="Select Model", choices=choices())

    assert state.current is not None
    assert state.current.value == "claude-sonnet-4-5"


def test_navigation_skips_headings_and_disconnected_rows():
    state = Selector(title="Select Model", choices=choices())

    visited = []
    for _ in range(4):
        state.move(1)
        assert state.current is not None
        visited.append(state.current.value)

    assert "gpt-5" not in visited, "a disconnected model must not be selectable"
    assert visited[-1] == "claude-opus-4-1"  # wrapped around


def test_search_is_fuzzy_but_per_field():
    state = Selector(title="Select Model", choices=choices())

    state.query = "clop"
    state.rebuild()
    assert [choice.value for choice in state.visible] == ["claude-opus-4-1"]

    state.query = "anthropic"
    state.rebuild()
    assert len(state.visible) == 2  # the hint field groups by provider


def test_a_query_with_no_match_says_so():
    state = Selector(title="Select Model", choices=choices())
    state.query = "zzz"
    state.rebuild()

    assert state.visible == []
    assert "no match" in "".join(text for _, text in state.fragments())


def test_the_disabled_reason_is_rendered_not_hidden():
    state = Selector(title="Select Model", choices=choices())

    rendered = "".join(text for _, text in state.fragments())

    assert "openai not connected" in rendered
    assert "·current" in rendered


def test_refresh_replaces_the_rows():
    state = Selector(
        title="Select Model",
        choices=choices()[:1],
        on_refresh=lambda: choices(),
    )

    state.refresh()

    assert len(state.visible) == 4


def test_ranking_prefers_a_prefix_match():
    ranked = fuzzy_rank("cl", choices())

    assert ranked[0].value.startswith("claude")


def test_a_non_interactive_terminal_never_blocks():
    assert select("Select Model", choices()) is None


# ---------------------------------------------------------------------------
# the router
# ---------------------------------------------------------------------------
def test_every_cli_command_is_reachable_and_not_reimplemented(project, tmp_path):
    session = build_session(project, tmp_path)
    router = CommandRouter(session=session)
    router.extra.update(handlers.session_commands(router=router))
    router.__post_init__()

    names = {command.name for command in router.commands()}

    # Project commands come from the CLI table, session commands from handlers.
    assert {"scan", "health", "review", "fix", "feature", "test"} <= names
    assert {"upgrade", "migrate", "compatibility", "docs", "release"} <= names
    assert {"login", "logout", "whoami", "provider", "model", "status", "context"} <= names
    assert {"clear", "exit", "help"} <= names


def test_an_unknown_command_is_reported_not_executed(project, tmp_path):
    router = CommandRouter(session=build_session(project, tmp_path))

    result = router.dispatch("/nope")

    assert result.handled is False
    assert "unknown command" in (result.warning or "")


def test_aliases_and_argument_splitting(project, tmp_path):
    router = CommandRouter(session=build_session(project, tmp_path))
    router.extra.update(handlers.session_commands(router=router))
    router.__post_init__()

    assert router.get("/q") is router.get("exit")
    assert router.split('/feature "add a screen"') == ("feature", ["add a screen"])
    assert router.split("/model claude-opus") == ("model", ["claude-opus"])


def test_an_ai_command_without_an_account_refuses_early(project, tmp_path):
    session = build_session(project, tmp_path, connect=None)
    router = CommandRouter(session=session)

    result = router.dispatch("/review")

    assert result.exit_code == 1
    assert "/login" in (result.warning or "")


def test_a_project_command_runs_the_real_cli(project, tmp_path):
    session = build_session(project, tmp_path)
    router = CommandRouter(session=session)

    result = router.dispatch("/scan --no-tools")

    assert result.exit_code == 0
    # The real command wrote the real context file.
    assert (project.root / ".rn-agent" / "project-context.json").is_file()


def test_cli_flags_are_the_cli_s_own(project, tmp_path):
    """A bad flag is rejected by the CLI parser, not by a copy of it."""
    session = build_session(project, tmp_path)
    router = CommandRouter(session=session)

    result = router.dispatch("/health --not-a-flag")

    assert result.exit_code != 0


def test_parse_flags_handles_values_and_switches():
    positional, options = parse_flags(
        ["0.86.0", "--to", "0.87.0", "--offline", "--kind=android"],
        flags=("offline",),
    )

    assert positional == ["0.86.0"]
    assert options == {"to": "0.87.0", "offline": True, "kind": "android"}


# ---------------------------------------------------------------------------
# switching provider and model
# ---------------------------------------------------------------------------
def test_switching_model_keeps_the_conversation(project, tmp_path):
    session = build_session(project, tmp_path)
    session.remember("user", "why is my list slow?")
    session.remember("assistant", "the row renderer is recreated each render")

    session.switch_model("claude-opus-4-1", persist=False)

    assert session.model_name == "claude-opus-4-1"
    assert len(session.history) == 2
    assert session.context.has_project_context()


def test_switching_provider_keeps_the_project_and_resets_the_model(project, tmp_path):
    session = build_session(project, tmp_path)
    session.remember("user", "hello")
    before = session.snapshot().rn_version

    session.switch_provider("google", persist=False)

    assert session.provider_name == "google"
    assert session.model_name  # the new provider's default, not the old model
    assert not session.model_name.startswith("claude")
    assert session.history  # the conversation survived
    assert session.snapshot().rn_version == before


def test_a_switch_invalidates_the_cached_provider(project, tmp_path):
    session = build_session(project, tmp_path)
    session.context.__dict__["ai"] = object()

    session.switch_model("claude-haiku-4-5", persist=False)

    assert "ai" not in session.context.__dict__


def test_the_snapshot_reports_the_real_auth_method(project, tmp_path):
    session = build_session(project, tmp_path)

    snapshot = session.snapshot()

    assert snapshot.provider == "anthropic"
    assert snapshot.auth_method is AuthMethod.API_KEY
    assert snapshot.auth_label == "API Key"
    assert snapshot.connected is True
    assert snapshot.ready is True


def test_an_unconnected_session_is_not_ready(project, tmp_path):
    session = build_session(project, tmp_path, connect=None)

    snapshot = session.snapshot()

    assert snapshot.ready is False
    assert session.ready() is False
    assert "not connected" in chrome.status_line(snapshot)
    assert "/login" in chrome.status_line(snapshot)


def test_the_status_line_shows_what_matters(project, tmp_path):
    session = build_session(project, tmp_path)

    line = chrome.status_line(session.snapshot())

    assert "Anthropic" in line
    assert "claude-sonnet-4-5" in line
    assert "RN 0.81.0" in line
    assert "Git" in line or "no git" in line


# ---------------------------------------------------------------------------
# /login, /provider, /model
# ---------------------------------------------------------------------------
def test_login_shows_each_provider_with_its_real_auth_method(project, tmp_path):
    session = build_session(project, tmp_path, connect=None)
    picker = picker_for(None)

    handlers.login(session, [], picker=picker)

    shown = {choice.value: choice.hint for choice in picker.seen["choices"]}
    assert "auth: API Key" in shown["anthropic"]
    assert "auth: OAuth" in shown["google"]
    assert "auth: None (local)" in shown["ollama"]
    assert picker.seen["title"] == "AI Provider"


def test_login_to_an_api_key_provider_stores_and_connects(project, tmp_path):
    session = build_session(project, tmp_path, connect=None)

    result = handlers.login(session, ["anthropic", "--api-key", KEY], picker=picker_for(None))

    assert result.exit_code == 0
    assert session.auth.connected("anthropic") is True
    assert session.provider_name == "anthropic"


def test_provider_switch_without_a_credential_warns_but_selects(project, tmp_path):
    session = build_session(project, tmp_path)

    result = handlers.provider(session, ["openai"], picker=picker_for(None))

    assert session.provider_name == "openai"
    assert "/login openai" in (result.warning or "")


def test_model_by_name_resolves_a_partial(project, tmp_path, wired_transport):
    session = build_session(project, tmp_path)
    queue_models(wired_transport, "claude-sonnet-4-5", "claude-opus-4-1")

    result = handlers.model(session, ["sonnet"], picker=picker_for(None))

    assert result.exit_code == 0
    assert session.model_name == "claude-sonnet-4-5"


def test_an_ambiguous_model_name_is_refused(project, tmp_path, wired_transport):
    session = build_session(project, tmp_path)
    queue_models(wired_transport, "claude-sonnet-4-5", "claude-opus-4-1")

    result = handlers.model(session, ["claude"], picker=picker_for(None))

    assert result.exit_code == 1
    assert "no single model" in (result.warning or "")


def test_a_task_model_is_bound_not_switched(project, tmp_path, wired_transport):
    session = build_session(project, tmp_path)
    queue_models(wired_transport, "claude-sonnet-4-5", "claude-opus-4-1")
    before = session.model_name

    handlers.model(session, ["opus", "--task", "migration"], picker=picker_for(None))

    assert session.context.config.ai.models.migration == "claude-opus-4-1"
    assert session.model_name == before  # the active model did not move


def test_selecting_a_model_from_a_disconnected_provider_is_refused(
    project, tmp_path, wired_transport
):
    session = build_session(project, tmp_path)
    queue_models(wired_transport, "claude-sonnet-4-5")

    result = handlers.model(session, ["gpt-5"], picker=picker_for(None))

    assert result.exit_code in (1, 10)
    assert session.provider_name == "anthropic"


def test_the_model_picker_groups_the_active_provider_first(
    project, tmp_path, wired_transport
):
    session = build_session(project, tmp_path)
    queue_models(wired_transport, "claude-sonnet-4-5", "claude-opus-4-1")
    picker = picker_for(None)

    handlers.model(session, [], picker=picker)

    groups = [choice.group for choice in picker.seen["choices"]]
    assert groups[0] == "Anthropic"
    assert any(group != "Anthropic" for group in groups)


def test_ctrl_p_cycles_within_the_connected_provider(project, tmp_path, wired_transport):
    session = build_session(project, tmp_path)
    queue_models(wired_transport, "claude-sonnet-4-5", "claude-opus-4-1")
    first = session.model_name

    moved = handlers.model_cycle(session)

    assert moved == "claude-opus-4-1" != first
    assert session.provider_name == "anthropic"


def test_whoami_lists_every_provider(project, tmp_path, capsys):
    session = build_session(project, tmp_path)

    handlers.whoami(session, [])

    printed = capsys.readouterr().out
    for label in ("Anthropic", "OpenAI", "Google Gemini", "Ollama"):
        assert label in printed


def test_status_never_invents_a_quota(project, tmp_path, capsys):
    session = build_session(project, tmp_path)

    handlers.status(session, [])

    printed = capsys.readouterr().out
    assert "% remaining" not in printed
    assert "claude-sonnet-4-5" in printed


def test_context_command_reports_what_would_be_sent(project, tmp_path, capsys):
    (project.root / ".env").write_text("SECRET=sk-live-abcdef0123456789\n", encoding="utf-8")
    session = build_session(project, tmp_path)

    handlers.context_command(session, ["button"])

    printed = capsys.readouterr().out
    assert "Context that would be sent" in printed
    assert "SECRET" not in printed


def test_clear_drops_the_conversation_only(project, tmp_path):
    session = build_session(project, tmp_path)
    session.remember("user", "hi")

    handlers.clear(session, [])

    assert session.history == []
    assert session.context.has_project_context()


# ---------------------------------------------------------------------------
# palette and dialogs
# ---------------------------------------------------------------------------
def test_the_palette_offers_every_command(project, tmp_path):
    session = build_session(project, tmp_path)
    router = CommandRouter(session=session)
    router.extra.update(handlers.session_commands(router=router))
    router.__post_init__()
    picker = picker_for("/migrate")

    command = open_palette(router, picker=picker)

    assert command is not None and command.name == "migrate"
    values = {choice.value for choice in picker.seen["choices"]}
    assert {"/model", "/provider", "/login", "/health", "/migrate", "/exit"} <= values


def test_a_dialog_falls_back_to_its_stated_default():
    chosen = choose(
        "Build failed",
        (Action("analyze", "Analyze"), Action("skip", "Skip")),
        default="skip",
        picker=lambda *args, **kwargs: None,
    )

    assert chosen == "skip"


# ---------------------------------------------------------------------------
# the migration wizard
# ---------------------------------------------------------------------------
def test_the_wizard_refuses_a_target_that_is_not_newer(project, tmp_path):
    session = build_session(project, tmp_path)

    result = run_wizard(
        session, ["--to", "0.80.0"], picker=picker_for(None), asker=lambda prompt: None
    )

    assert result.exit_code == 1
    assert "not newer" in (result.warning or "")


def test_the_wizard_asks_for_a_target_when_none_is_given(project, tmp_path):
    session = build_session(project, tmp_path)
    asked: list[str] = []

    def asker(prompt: str) -> str | None:
        asked.append(prompt)
        return "0.86.0"

    # --offline keeps the engine off the network; the wizard is what is under test.
    result = run_wizard(session, ["--offline"], picker=picker_for(None), asker=asker)

    assert asked and "Target" in asked[0]
    # No tty: the action dialog defaults to "analyze", which is the engine's
    # dry-run preview - so nothing is written and the branch is not created.
    assert result.exit_code in (0, 1)
    assert not (project.root / ".rn-agent" / "migration-history.json").exists()


def test_the_wizard_cancels_when_no_version_is_offered(project, tmp_path):
    session = build_session(project, tmp_path)

    result = run_wizard(session, [], picker=picker_for(None), asker=lambda prompt: None)

    assert result.message == "cancelled"


# ---------------------------------------------------------------------------
# intent routing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fix my android build", Intent.FIX),
        ("migrate react native to 0.86.0", Intent.MIGRATE),
        ("can I upgrade to 0.86?", Intent.COMPATIBILITY),
        ("what's wrong with my project", Intent.HEALTH),
        ("how does Hermes differ from JSC", Intent.QUESTION),
    ],
)
def test_prose_routes_deterministically(text, expected):
    assert detect(text).intent is expected


def test_a_migration_request_carries_its_target():
    detection = detect("migrate react native from 0.84.2 to 0.86.0")

    assert detection.arguments == ("--to", "0.86.0")
    assert detection.strong is True


def test_the_model_cache_holds_ids_only(project, tmp_path):
    session = build_session(project, tmp_path)
    session.registry.discover(
        "anthropic", build=None, connected=False, suggested=("claude-sonnet-4-5",)
    )
    session.registry.discover(
        "anthropic", build=None, connected=False, suggested=("claude-sonnet-4-5",)
    )
    cache = tmp_path / "model-cache.json"
    if cache.is_file():
        payload = json.dumps(json.loads(cache.read_text()))
        assert KEY not in payload
        assert "token" not in payload.casefold()


def test_a_suggested_model_is_labelled_as_such(project, tmp_path):
    session = build_session(project, tmp_path, connect=None)

    models = session.registry.discover(
        "openai", build=None, connected=False, suggested=("gpt-5",)
    )

    assert models and models[0].source is ModelSource.SUGGESTED
    assert models[0].available is False
    assert isinstance(models[0], ModelInfo)
