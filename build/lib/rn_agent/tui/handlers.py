"""The commands that are about the session, not the project.

``/login``, ``/provider`` and ``/model`` have no command-line twin to delegate
to, because they change *this* conversation: which account answers, which model,
which task gets which model. They are the only handlers written by hand, and the
rules they follow are the ones from the brief:

* the provider picker shows each provider's **real** authentication method, and
  an API-key provider says why it is not an account login;
* switching provider or model keeps the conversation and the scanned project;
* every one of them works without a terminal too - pass the name as an argument
  and no picker opens, which is what keeps a piped session usable.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from ..ai.models import ModelInfo, ModelSource
from ..ai.registry import provider_names, resolve_spec
from ..auth.authenticator import AuthMethod
from ..cli import ui
from ..errors import ProviderError, RNAgentError
from . import chrome
from .router import RouteResult, SlashCommand, parse_flags
from .select import Choice, select
from .session import SessionManager

if TYPE_CHECKING:
    from ..auth.manager import ProviderAuth

Picker = Callable[..., Choice | None]


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------
def login(session: SessionManager, args: list[str], *, picker: Picker = select) -> RouteResult:
    """Connect an account, by whatever mechanism the provider actually offers."""
    positional, options = parse_flags(args, flags=("stdin", "dry-run"))
    name = positional[0] if positional else _pick_provider(session, picker, purpose="login")
    if not name:
        return RouteResult(message="cancelled")

    entry = _provider_entry(session, name)
    authenticator = session.auth.for_provider(entry.provider)
    capability = authenticator.capability

    ui.blank()
    ui.header(f"Sign in · {entry.label}", f"auth: {capability.label}")
    if capability.detail:
        ui.note(capability.detail)
    if capability.unsupported_note:
        # The honest part: say why this is a key rather than a subscription login.
        ui.console().print(f"  [warn]Note[/warn] [muted]{capability.unsupported_note}[/muted]")

    secret: str | None = None
    if capability.method is AuthMethod.API_KEY or (
        capability.method is AuthMethod.OAUTH and options.get("api-key")
    ):
        secret = _read_key(entry, options)
        if secret is None:
            return RouteResult(exit_code=1, warning="no key given; nothing was stored")
    elif capability.method is AuthMethod.OAUTH:
        ui.bullet(f"Opening {entry.label} authentication in your browser…")

    outcome = authenticator.login(
        secret=secret,
        client_id=options.get("client-id"),
        client_secret=options.get("client-secret"),
        dry_run=bool(options.get("dry-run")) or session.dry_run,
    )
    for warning in outcome.warnings:
        ui.warning(warning)

    state = outcome.state
    if state.connected:
        session.switch_provider(entry.provider)
        detail = f" as {state.account}" if state.account else ""
        ui.success(f"{entry.label} connected{detail} · auth: {state.method.label}")
        ui.note("run /model to choose a model")
        return RouteResult()
    return RouteResult(exit_code=10, warning=f"{entry.label} is still not connected")


def _read_key(entry: ProviderAuth, options: dict[str, str | bool]) -> str | None:
    """A key from the flag, from stdin, or from a hidden prompt - never echoed."""
    from_flag = options.get("api-key")
    if isinstance(from_flag, str) and from_flag:
        return from_flag
    if options.get("stdin"):
        import sys

        piped = sys.stdin.readline().strip()
        return piped or None
    if entry.console_url:
        ui.note(f"create a key at {entry.console_url}")
    return ui.ask_secret(f"{entry.label} API key")


def logout(session: SessionManager, args: list[str], *, picker: Picker = select) -> RouteResult:
    """Forget a stored credential or OAuth session."""
    positional, options = parse_flags(args, flags=("all",))
    if options.get("all"):
        removed = [
            entry.provider
            for entry in session.auth.providers()
            if session.auth.for_provider(entry.provider).logout()
        ]
        return RouteResult(
            message=f"signed out of {', '.join(removed)}" if removed else "nothing was stored"
        )
    name = positional[0] if positional else session.provider_name
    if not name:
        return RouteResult(exit_code=1, warning="no provider given and none selected")
    entry = _provider_entry(session, name)
    if session.auth.for_provider(entry.provider).logout():
        return RouteResult(message=f"signed out of {entry.label}")
    return RouteResult(message=f"nothing stored for {entry.label}")


def whoami(session: SessionManager, args: list[str]) -> RouteResult:
    """Which account each provider is using, and how."""
    _ = args
    rows: list[tuple[str, str, str, str]] = []
    for entry in session.auth.providers():
        authenticator = session.auth.for_provider(entry.provider)
        capability = authenticator.capability
        state = authenticator.state()
        detail = state.label or capability.detail
        if not state.connected and capability.unsupported_note:
            detail = capability.unsupported_note
        marker = " ·active" if entry.provider == session.provider_name else ""
        rows.append(
            (
                f"{entry.label}{marker}",
                capability.label,
                _state_cell(state.status_word),
                detail or "",
            )
        )
    chrome.render_auth_table(rows)
    return RouteResult()


def _state_cell(word: str) -> str:
    if word == "connected":
        return "[ok]connected[/ok]"
    if word == "available locally":
        return "[info]local[/info]"
    return "[warn]not connected[/warn]"


# ---------------------------------------------------------------------------
# /provider
# ---------------------------------------------------------------------------
def provider(session: SessionManager, args: list[str], *, picker: Picker = select) -> RouteResult:
    """Switch provider without losing the conversation."""
    positional, options = parse_flags(args, flags=("list",))
    if options.get("list"):
        return whoami(session, [])
    name = positional[0] if positional else _pick_provider(session, picker, purpose="switch")
    if not name:
        return RouteResult(message="cancelled")

    entry = _provider_entry(session, name)
    state = session.auth.state(entry.provider)
    session.switch_provider(entry.provider)
    if not state.connected:
        return RouteResult(
            warning=(
                f"{entry.label} selected but not connected - "
                f"run /login {entry.provider} ({session.auth.capability(entry.provider).label})"
            )
        )
    ui.success(f"provider: {entry.label} · auth: {state.method.label}")
    ui.note(f"model reset to {session.model_name} · /model to choose another")
    return RouteResult()


def _pick_provider(session: SessionManager, picker: Picker, *, purpose: str) -> str | None:
    """The provider list, annotated with each one's real auth method."""
    choices: list[Choice] = []
    for entry in session.auth.providers():
        capability = session.auth.capability(entry.provider)
        state = session.auth.state(entry.provider)
        note = "" if state.connected else capability.label
        choices.append(
            Choice(
                value=entry.provider,
                label=entry.label,
                hint=f"auth: {capability.label} · {state.status_word}",
                current=entry.provider == session.provider_name,
                note=note,
            )
        )
    title = "AI Provider" if purpose == "login" else "Select Provider"
    chosen = picker(title, choices, footer="↑↓ Navigate   Enter Select   Esc Cancel")
    return chosen.value if chosen else None


def _provider_entry(session: SessionManager, name: str) -> ProviderAuth:
    from ..auth.manager import auth_for

    try:
        spec = resolve_spec(name)
    except RNAgentError as error:
        raise ProviderError(
            f"unknown provider: {name}",
            hint=f"Known providers: {', '.join(provider_names())}.",
        ) from error
    return auth_for(spec.name)


# ---------------------------------------------------------------------------
# /model
# ---------------------------------------------------------------------------
def model(session: SessionManager, args: list[str], *, picker: Picker = select) -> RouteResult:
    """Choose a model - for now, or for one task role."""
    positional, options = parse_flags(args, flags=("list", "refresh"))
    task = options.get("task")
    refresh = bool(options.get("refresh"))

    if options.get("list"):
        return _list_models(session, refresh=refresh)

    if positional:
        chosen = _resolve_model(session, positional[0], refresh=refresh)
        if chosen is None:
            return RouteResult(
                exit_code=1,
                warning=(
                    f"no single model matches {positional[0]!r} - "
                    "run /model with no argument to pick from the list"
                ),
            )
    else:
        chosen = _pick_model(session, picker, refresh=refresh)
        if chosen is None:
            return RouteResult(message="cancelled")

    if chosen.provider != session.provider_name:
        state = session.auth.state(chosen.provider)
        if not state.connected:
            return RouteResult(
                exit_code=10,
                warning=f"{chosen.provider} is not connected - run /login {chosen.provider}",
            )
        session.switch_provider(chosen.provider)

    if isinstance(task, str) and task:
        session.set_task_model(task, chosen.id)
        ui.success(f"{task} model: {chosen.id}")
        return RouteResult()

    session.switch_model(chosen.id)
    ui.success(f"model: {chosen.id}")
    if session.history:
        ui.note(f"{len(session.history)} turn(s) of context kept")
    return RouteResult()


def _model_choices(session: SessionManager, *, refresh: bool) -> list[Choice]:
    choices: list[Choice] = []
    for group in session.all_models(refresh=refresh):
        for info in group.models:
            choices.append(
                Choice(
                    value=info.id,
                    label=info.label or info.id,
                    hint=_model_hint(info),
                    group=group.label,
                    current=info.id == session.model_name
                    and info.provider == session.provider_name,
                    disabled=not group.connected,
                    note=group.note or "",
                    payload=info,
                )
            )
    return choices


def _model_hint(info: ModelInfo) -> str:
    if info.source is ModelSource.SUGGESTED:
        return f"{info.provider} · suggested"
    if info.source is ModelSource.CONFIG:
        return f"{info.provider} · from config"
    return info.provider


def _pick_model(session: SessionManager, picker: Picker, *, refresh: bool) -> ModelInfo | None:
    choices = _model_choices(session, refresh=refresh)
    if not choices:
        return None
    chosen = picker(
        "Select Model",
        choices,
        footer="↑↓ Navigate   Enter Select   Ctrl+R Refresh   Esc Cancel",
        on_refresh=lambda: _model_choices(session, refresh=True),
    )
    if chosen is None:
        return None
    payload = chosen.payload
    return payload if isinstance(payload, ModelInfo) else None


def _resolve_model(session: SessionManager, query: str, *, refresh: bool) -> ModelInfo | None:
    """Resolve ``/model <name>`` across every provider, refusing ambiguity."""
    candidates: list[ModelInfo] = []
    for group in session.all_models(refresh=refresh):
        candidates.extend(group.models)
    return session.registry.resolve(query, candidates)


def _list_models(session: SessionManager, *, refresh: bool) -> RouteResult:
    groups = session.all_models(refresh=refresh)
    rows: list[list[str]] = []
    for group in groups:
        for info in group.models:
            marker = "·" if info.id == session.model_name else ""
            rows.append(
                [
                    marker,
                    info.id,
                    group.label,
                    info.source.value,
                    "connected" if group.connected else (group.note or "not connected"),
                ]
            )
    ui.table(["", "Model", "Provider", "Source", "State"], rows, title="Models")
    return RouteResult()


# ---------------------------------------------------------------------------
# /status, /context, /clear, /help, /exit
# ---------------------------------------------------------------------------
def status(session: SessionManager, args: list[str]) -> RouteResult:
    """Everything the status bar compresses, spelled out."""
    _ = args
    snapshot = session.snapshot()
    capability = (
        session.auth.capability(snapshot.provider) if snapshot.provider else None
    )
    ui.section("Session")
    ui.key_values(
        [
            ("project", snapshot.project_name),
            ("root", snapshot.project_root),
            ("react native", snapshot.rn_version or "unknown · /scan"),
            ("scanned", "yes" if snapshot.scanned else "no · /scan"),
            ("provider", snapshot.provider_label or "none · /login"),
            ("auth", capability.label if capability else "-"),
            ("connected", "yes" if snapshot.connected else "no"),
            ("account", snapshot.account or "-"),
            ("model", snapshot.model or "none · /model"),
            ("git", _git_word(snapshot.git_branch, snapshot.git_dirty)),
            ("turns", snapshot.turns),
            ("dry run", "yes" if snapshot.dry_run else "no"),
        ]
    )
    task_models = {
        task: value
        for task, value in session.context.config.ai.models.model_dump(exclude_none=True).items()
        if isinstance(value, str) and value
    }
    if task_models:
        ui.table(
            ["Task", "Model"],
            [[task, value] for task, value in sorted(task_models.items())],
            title="Task models",
        )
    if capability and capability.unsupported_note:
        ui.note(capability.unsupported_note)
    _render_usage(session)
    return RouteResult()


def _render_usage(session: SessionManager) -> None:
    """Token accounting the agent itself recorded - never an invented quota.

    No provider in the registry exposes remaining-quota through a supported
    interface, so this shows what this project has spent and says nothing about
    what is left.
    """
    try:
        usage = session.context.store.ai_usage_summary()
    except RNAgentError:  # pragma: no cover - store unavailable
        return
    if not usage.get("calls"):
        return
    ui.key_values(
        [
            ("ai calls", usage["calls"]),
            ("input tokens", f"{usage['input_tokens']:,}"),
            ("output tokens", f"{usage['output_tokens']:,}"),
        ]
    )
    ui.note("recorded locally by this agent; providers do not publish remaining quota")


def _git_word(branch: str | None, dirty: bool | None) -> str:
    if dirty is None:
        return "not a repository"
    state = "dirty" if dirty else "clean"
    return f"{branch or 'detached'} ({state})"


def context_command(session: SessionManager, args: list[str]) -> RouteResult:
    """Show what a request would send - the audit trail, before spending tokens."""
    from ..agents.context_builder import ContextBuilder

    positional, options = parse_flags(args, flags=("changed",))
    query = " ".join(positional) or None
    selected = ContextBuilder(session.context).select(
        query=query, changed=bool(options.get("changed"))
    )
    ui.section("Context that would be sent")
    ui.key_values(
        [
            ("files", len(selected)),
            ("approx tokens", f"{selected.approx_tokens:,}"),
            ("refused (secrets)", len(selected.refused)),
            ("not sent (budget)", len(selected.skipped)),
        ]
    )
    for file in selected.files[:20]:
        suffix = " (truncated)" if file.truncated else ""
        ui.note(f"{file.path}  {file.lines} lines{suffix}")
    if len(selected.files) > 20:
        ui.note(f"… and {len(selected.files) - 20} more")
    for path in selected.refused:
        ui.console().print(f"  [warn]refused[/warn] [muted]{path} (secret-bearing)[/muted]")
    return RouteResult()


def clear(session: SessionManager, args: list[str]) -> RouteResult:
    _ = args
    count = session.clear_history()
    ui.console().clear()
    chrome.render_banner(session.snapshot())
    return RouteResult(message=f"cleared {count} turn(s)" if count else None)


def help_command(session: SessionManager, args: list[str], *, router: Any = None) -> RouteResult:
    _ = args, session
    if router is not None:
        chrome.render_help(router.help_rows())
    return RouteResult()


def exit_command(session: SessionManager, args: list[str]) -> RouteResult:
    _ = args, session
    return RouteResult(quit=True)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def session_commands(*, picker: Picker = select, router: Any = None) -> dict[str, SlashCommand]:
    """The session-scoped commands, bound to a picker and the router."""

    def bind(handler: Callable[..., RouteResult], **extra: Any) -> Callable[[SessionManager, list[str]], RouteResult]:
        def call(session: SessionManager, args: list[str]) -> RouteResult:
            return handler(session, args, **extra)

        return call

    def _wizard_migrate(
        session: SessionManager, args: list[str], *, picker: Picker
    ) -> RouteResult:
        """``/migrate`` asks before it acts; the engine itself is unchanged."""
        from .wizard import migrate as run_wizard

        return run_wizard(session, args, picker=picker)

    entries = [
        SlashCommand(
            name="help",
            summary="List every command",
            handler=bind(help_command, router=router),
            group="Session",
            aliases=("?",),
        ),
        SlashCommand(
            name="login",
            summary="Connect an AI account (OAuth where the provider offers it)",
            handler=bind(login, picker=picker),
            group="Session",
            usage="/login [provider] [--api-key K] [--client-id ID]",
        ),
        SlashCommand(
            name="logout",
            summary="Forget a stored credential or OAuth session",
            handler=bind(logout, picker=picker),
            group="Session",
            usage="/logout [provider] [--all]",
        ),
        SlashCommand(
            name="whoami",
            summary="Show each provider's auth method and state",
            handler=whoami,
            group="Session",
        ),
        SlashCommand(
            name="provider",
            summary="Switch provider, keeping the conversation",
            handler=bind(provider, picker=picker),
            group="Session",
            usage="/provider [name] [--list]",
        ),
        SlashCommand(
            name="model",
            summary="Switch model, or bind one to a task",
            handler=bind(model, picker=picker),
            group="Session",
            usage="/model [name] [--task migration] [--list] [--refresh]",
        ),
        SlashCommand(
            name="status",
            summary="Project, account, model, git and token usage",
            handler=status,
            group="Session",
        ),
        SlashCommand(
            name="context",
            summary="Show what a request would send, before sending it",
            handler=context_command,
            group="Session",
            usage="/context [query] [--changed]",
        ),
        SlashCommand(
            name="migrate",
            summary="Migrate React Native, step by step, with a wizard",
            handler=bind(_wizard_migrate, picker=picker),
            group="Maintain",
            usage="/migrate [--to VERSION] [--skip-native] [--build] [--offline]",
        ),
        SlashCommand(
            name="clear",
            summary="Clear the conversation, keep the project context",
            handler=clear,
            group="Session",
        ),
        SlashCommand(
            name="exit",
            summary="Leave the terminal",
            handler=exit_command,
            group="Session",
            aliases=("quit", "q"),
        ),
    ]
    return {command.name: command for command in entries}


def model_cycle(session: SessionManager) -> str | None:
    """Next model in the active provider's list - the Ctrl+P shortcut.

    Cycling stays inside the connected provider on purpose: a keystroke should
    never move billing to another account.
    """
    models: Sequence[ModelInfo] = session.available_models()
    usable = [info for info in models if info.available]
    if len(usable) < 2:
        return None
    ids = [info.id for info in usable]
    current = session.model_name
    index = ids.index(current) + 1 if current in ids else 0
    return session.switch_model(ids[index % len(ids)])
