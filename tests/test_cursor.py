"""Cursor: as a completion backend, and as an agent this project keeps on a leash.

Two integrations with opposite risk profiles, so they are tested for opposite
things. :class:`CursorProvider` must be **incapable** of editing the tree - it is
a brain, and rn-agent's own apply pipeline stays in charge. ``rn-agent delegate``
is the opt-in where Cursor *does* edit, so what matters there is the bracket: a
clean tree, a deny list Cursor enforces itself, an audit of what actually
changed, and no destructive git.

Every test drives a stub binary on PATH. The real CLI is never installed in CI,
and a stub is also the only way to assert "this flag was not passed".
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tests.conftest import FakeTransport

from rn_agent.agents.cursor_agent import (
    ALWAYS_DENY,
    CLI_CONFIG,
    CursorAgentRunner,
)
from rn_agent.agents.rules import ProjectRules
from rn_agent.ai.cursor import CursorProvider
from rn_agent.ai.registry import canonical_name, resolve_spec
from rn_agent.ai.types import Message
from rn_agent.auth.authenticator import AuthMethod
from rn_agent.auth.manager import AuthenticationManager, auth_for
from rn_agent.commands.delegate import DelegateCommand
from rn_agent.errors import ProviderError, RNAgentError
from rn_agent.net.http import TransportError
from rn_agent.runner.command_runner import CommandRunner
from rn_agent.tools.cursor import PINNED_VERSION, ManagedCursorCli

RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 1200,
    "result": '{"proposals": []}',
    "session_id": "sess-1",
}


@pytest.fixture
def cursor_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stub `cursor-agent` on PATH that records argv, stdin and environment.

    It writes a file **only** when ``--force`` is present, which is what lets a
    test prove the provider cannot edit the project.
    """
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    log = bin_dir / "argv.json"
    stub = bin_dir / "cursor-agent"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"log = {str(log)!r}\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == 'status':\n"
        "    print(json.dumps({'authenticated': True, 'email': 'dev@example.com'}))\n"
        "    raise SystemExit(0)\n"
        "if '--list-models' in args:\n"
        "    print(json.dumps(['composer-2.5', 'claude-sonnet-5']))\n"
        "    raise SystemExit(0)\n"
        # The prompt arrives on stdin, so the stub has to drain it to record how
        # much it got - a test asserting "no argv limit" needs that number.
        "stdin = sys.stdin.read() if not sys.stdin.isatty() else ''\n"
        "open(log, 'w').write(json.dumps({\n"
        "    'argv': args,\n"
        "    'key': os.environ.get('CURSOR_API_KEY'),\n"
        "    'stdin_chars': len(stdin),\n"
        "    'stdin': stdin,\n"
        "}))\n"
        "if '--force' in args:\n"
        "    target = os.path.join(os.environ.get('STUB_WRITE_DIR', '.'), 'agent-wrote-this.txt')\n"
        "    open(target, 'w').write('x')\n"
        # The result object is embedded as JSON text, not re-serialised: a JSON
        # `false` is not valid Python, which is exactly the trap here.
        f"sys.stdout.write({json.dumps(RESULT)!r})\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def called(log: Path) -> dict:
    return json.loads(log.read_text())


# ---------------------------------------------------------------------------
# the provider: a brain, not a pair of hands
# ---------------------------------------------------------------------------
def test_the_provider_never_passes_force(cursor_cli, project):
    """The whole safety argument: no --force means the agent cannot write."""
    provider = CursorProvider(model="composer-2.5", workspace=str(project.root))

    provider.complete([Message.user("what does this app do?")])

    argv = called(cursor_cli)["argv"]
    assert "--force" not in argv
    assert "--yolo" not in argv
    assert "--trust" in argv
    # This CLI only accepts --mode ask|plan. Omitting --mode is the coding agent.
    assert "--mode" not in argv
    assert "--print" in argv and argv[argv.index("--output-format") + 1] == "json"


def test_mode_agent_is_omitted_because_the_cli_rejects_it(cursor_cli, project):
    CursorProvider(model="m", mode="agent").complete([Message.user("hi")])

    assert "--mode" not in called(cursor_cli)["argv"]


def test_mode_ask_is_passed_when_requested(cursor_cli, project):
    CursorProvider(model="m", mode="ask").complete([Message.user("hi")])

    argv = called(cursor_cli)["argv"]
    assert argv[argv.index("--mode") + 1] == "ask"


def test_the_prompt_tells_cursor_it_is_not_ask_mode(cursor_cli, project):
    from rn_agent.ai.cursor import HOST_CONTRACT

    CursorProvider(model="m").complete([Message.user("rename HomeScreen")])

    prompt = called(cursor_cli)["stdin"]
    assert HOST_CONTRACT in prompt
    assert "not Cursor Ask mode" in prompt


def test_the_provider_writes_nothing_into_the_project(cursor_cli, project, monkeypatch):
    """Proof, not assertion: the stub *would* write if --force were passed."""
    monkeypatch.setenv("STUB_WRITE_DIR", str(project.root))
    before = sorted(p.name for p in project.root.iterdir())

    CursorProvider(workspace=str(project.root)).complete([Message.user("hi")])

    assert sorted(p.name for p in project.root.iterdir()) == before
    assert not (project.root / "agent-wrote-this.txt").exists()


def test_the_documented_result_object_becomes_a_completion(cursor_cli, project):
    completion = CursorProvider(model="composer-2.5").complete(
        [Message.system("be brief"), Message.user("go")], task="review"
    )

    assert completion.text == '{"proposals": []}'
    assert completion.provider == "cursor"
    assert completion.model == "composer-2.5"
    assert completion.task == "review"
    # The CLI reports duration, not tokens. Zero is honest; a guess would corrupt
    # the usage accounting `/status` prints.
    assert completion.usage.total_tokens == 0


def test_the_conversation_is_flattened_with_roles(cursor_cli, project):
    CursorProvider(model="m").complete(
        [Message.user("first"), Message.assistant("second"), Message.user("third")],
        system="obey rules",
    )

    prompt = called(cursor_cli)["stdin"]
    assert "obey rules" in prompt
    assert prompt.index("obey rules") < prompt.index("[Developer]\nfirst")
    assert "[Developer]\nfirst" in prompt
    assert "[Assistant]\nsecond" in prompt


def test_the_key_travels_in_the_environment_not_argv(cursor_cli, project):
    CursorProvider(model="m", credential="key-abc123").complete([Message.user("hi")])

    call = called(cursor_cli)
    assert call["key"] == "key-abc123"
    # `ps` shows arguments to every user on the machine.
    assert not any("key-abc123" in str(part) for part in call["argv"])


def test_a_very_long_prompt_goes_through_on_stdin(cursor_cli, project):
    """The regression: rn-agent's context budget can exceed any argv limit.

    155k characters used to be refused by an invented 120k cap. The prompt is
    written to the CLI's stdin, so there is no ceiling to hit - and the argv the
    CLI receives must not contain the prompt at all.
    """
    long_prompt = "x" * 155_260

    completion = CursorProvider(model="m").complete([Message.user(long_prompt)])

    assert completion.text  # it answered rather than refusing
    call = called(cursor_cli)
    assert call["stdin_chars"] >= 155_260
    # Nothing that large may reach argv, whatever the platform's ARG_MAX is.
    assert all(len(part) < 1000 for part in call["argv"])


def test_a_missing_cli_says_how_to_install_it(project, monkeypatch):
    monkeypatch.setenv("PATH", str(project.root))  # nothing on it
    monkeypatch.delenv("RN_AGENT_CURSOR_BIN", raising=False)
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)
    provider = CursorProvider(model="m")

    with pytest.raises(ProviderError) as failure:
        provider.complete([Message.user("hi")])

    assert "not installed" in str(failure.value)
    assert "rn-agent login cursor" in (failure.value.hint or "")


def test_the_provider_finds_a_managed_cli_off_path(tmp_path, monkeypatch, project):
    """Login installs under rn-agent's directory, which is frequently not on PATH."""
    from rn_agent.core.paths import user_config_dir
    from rn_agent.tools.cursor import EXECUTABLE_NAME

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("RN_AGENT_CURSOR_BIN", raising=False)
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)
    binary = user_config_dir() / "tools" / EXECUTABLE_NAME / "2026.08.11-test" / EXECUTABLE_NAME
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    assert Path(CursorProvider(model="m").executable()) == binary


def test_the_provider_finds_cursor_in_local_bin(tmp_path, monkeypatch, project):
    """OMP-style lookup: ~/.local/bin, even when that directory is not on PATH."""
    bindir = tmp_path / "local-bin"
    bindir.mkdir()
    binary = bindir / "cursor-agent"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("RN_AGENT_CURSOR_BIN", raising=False)
    monkeypatch.setattr("rn_agent.tools.cursor.extra_bin_dirs", lambda: (bindir,))

    assert Path(CursorProvider(model="m").executable()) == binary.resolve()


def test_an_explicit_binary_is_used_for_verify(tmp_path, monkeypatch, project):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("RN_AGENT_CURSOR_BIN", raising=False)
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)
    chosen = tmp_path / "given-agent"
    chosen.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    chosen.chmod(0o755)

    assert Path(CursorProvider(model="m", binary=str(chosen)).executable()) == chosen


def test_login_prefers_a_composer_model():
    from rn_agent.ai.cursor import preferred_model

    assert preferred_model(("gpt-5", "composer-2.5", "claude-sonnet-5")) == "composer-2.5"
    assert preferred_model(()) is None
    assert preferred_model(("auto",)) == "auto"


def test_a_failing_cli_surfaces_its_own_message(tmp_path, monkeypatch, project):
    bin_dir = tmp_path / "bad-bin"
    bin_dir.mkdir()
    stub = bin_dir / "cursor-agent"
    stub.write_text("#!/bin/sh\necho 'not logged in' >&2\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(ProviderError) as failure:
        CursorProvider(model="m").complete([Message.user("hi")])

    assert "not logged in" in str(failure.value)


def test_an_invalid_mode_is_not_reported_as_an_http_outage(tmp_path, monkeypatch, project):
    bin_dir = tmp_path / "mode-bin"
    bin_dir.mkdir()
    stub = bin_dir / "cursor-agent"
    stub.write_text(
        "#!/bin/sh\n"
        "echo \"cursor: error: option '--mode <mode>' argument 'agent' is invalid. "
        'Allowed choices are plan, ask." >&2\n'
        "exit 2\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(ProviderError) as failure:
        CursorProvider(model="m", mode="ask").complete([Message.user("hi")])

    assert "rejected --mode" in str(failure.value)
    assert "failing on its side" not in (failure.value.hint or "")
    assert "HTTP 500" not in str(failure.value)


def test_workspace_trust_is_not_reported_as_a_cursor_outage(tmp_path, monkeypatch, project):
    bin_dir = tmp_path / "trust-bin"
    bin_dir.mkdir()
    stub = bin_dir / "cursor-agent"
    stub.write_text(
        "#!/bin/sh\necho 'Workspace Trust Required. Do you trust the contents of this directory?' >&2\nexit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(ProviderError) as failure:
        CursorProvider(model="m").complete([Message.user("hi")])

    assert "workspace trust" in str(failure.value).casefold()
    assert "failing on its side" not in (failure.value.hint or "")


def test_an_agent_error_result_is_a_failure_not_an_answer(tmp_path, monkeypatch, project):
    bin_dir = tmp_path / "err-bin"
    bin_dir.mkdir()
    stub = bin_dir / "cursor-agent"
    stub.write_text(
        '#!/bin/sh\necho \'{"type":"result","is_error":true,"result":"tool limit reached"}\'\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(ProviderError) as failure:
        CursorProvider(model="m").complete([Message.user("hi")])

    assert "tool limit reached" in str(failure.value)


def test_progress_lines_before_the_result_are_tolerated(tmp_path, monkeypatch, project):
    bin_dir = tmp_path / "ndjson-bin"
    bin_dir.mkdir()
    stub = bin_dir / "cursor-agent"
    stub.write_text(
        "#!/bin/sh\n"
        'echo \'{"type":"system","subtype":"init","model":"composer-2.5"}\'\n'
        'echo \'{"type":"result","result":"the answer"}\'\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    completion = CursorProvider(model="m").complete([Message.user("hi")])

    assert completion.text == "the answer"


def test_models_come_from_the_cli_not_a_bundled_list(cursor_cli, project):
    provider = CursorProvider()

    assert provider.list_models() == ("composer-2.5", "claude-sonnet-5")
    assert CursorProvider.suggested_models == ("composer-2.5",)
    assert CursorProvider.default_model == ""


def test_listing_lines_keep_the_id_and_drop_the_pretty_name(tmp_path, monkeypatch):
    from rn_agent.ai.cursor import _model_names

    names = _model_names(
        "Available models:\n"
        "claude-opus-5-thinking-high - Claude Opus 5 1M Thinking\n"
        "* composer-2.5 - Composer\n"
        "auto\n"
    )

    assert names == (
        "claude-opus-5-thinking-high",
        "composer-2.5",
        "auto",
    )


def test_a_display_name_is_not_passed_as_the_cli_model(cursor_cli, project):
    CursorProvider(model="claude-opus-5-thinking-high - Claude Opus 5 1M Thinking").complete(
        [Message.user("hi")]
    )

    argv = called(cursor_cli)["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-thinking-high"


def test_a_rejected_model_does_not_dump_the_catalogue(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bad-model"
    bin_dir.mkdir()
    stub = bin_dir / "cursor-agent"
    stub.write_text(
        "#!/bin/sh\n"
        "echo 'Cannot use this model: claude-opus-5-thinking-high - Claude Opus 5 1M Thinking. "
        "Available models: auto, composer-2.5, gemini-3-flash' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(ProviderError) as failure:
        CursorProvider(model="claude-opus-5-thinking-high").complete([Message.user("hi")])

    message = failure.value.message
    hint = failure.value.hint or ""
    assert "rejected this model" in message
    assert "Available models" not in message
    assert "HTTP" not in message
    assert "/model" in hint


def test_verify_checks_the_session_without_calling_a_model(cursor_cli, project):
    identity = CursorProvider().verify()

    assert identity.ok is True
    assert "dev@example.com" in identity.detail
    # `status` and `--list-models` only; no completion was requested.
    assert not cursor_cli.exists()


# ---------------------------------------------------------------------------
# registry and auth
# ---------------------------------------------------------------------------
def test_the_names_developers_type_reach_cursor():
    for alias in ("cursor", "cursor-agent", "cursor-cli", "composer"):
        assert canonical_name(alias) == "cursor"


def test_cursor_needs_no_credential_because_the_cli_holds_one():
    spec = resolve_spec("cursor")

    assert spec.requires_credential is False
    assert spec.env_var == "CURSOR_API_KEY"


def test_the_capability_says_the_tool_owns_the_session():
    entry = auth_for("cursor")
    manager = AuthenticationManager()

    assert entry.method is AuthMethod.TOOL
    capability = manager.capability("cursor")
    assert "session" in capability.label
    assert "cursor-agent login" in capability.detail


def test_a_tool_that_is_not_installed_is_not_connected(monkeypatch):
    """Claiming a session for a CLI that is absent would be a plain lie - and it
    would make `/model` try to discover models from a binary that is not there."""
    monkeypatch.setattr("rn_agent.tools.cursor.resolve_binary", lambda **_: None)

    assert AuthenticationManager().state("cursor").connected is False


def test_a_tool_that_is_present_is_connected(monkeypatch, tmp_path):
    """Presence is what "connected" means here; `--check` proves the session."""
    binary = tmp_path / "cursor-agent"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("rn_agent.tools.cursor.resolve_binary", lambda **_: binary)

    state = AuthenticationManager().state("cursor")

    assert state.connected is True
    assert state.source == "tool"


def test_logging_out_of_cursor_never_touches_cursors_own_session():
    """rn-agent may forget its key; the tool's credential is not rn-agent's."""
    manager = AuthenticationManager()

    # No key was ever stored, so there is nothing of ours to forget.
    assert manager.for_provider("cursor").logout() is False


def test_sign_in_runs_the_cursor_cli_login(cursor_cli):
    """The browser page is Cursor's. We only spawn the command that opens it."""
    from rn_agent.tools.cursor import run_sign_in

    run_sign_in(install=False)

    assert called(cursor_cli)["argv"] == ["login"]


def test_sign_in_without_the_cli_explains_how_to_install(tmp_path, monkeypatch):
    from rn_agent.tools.cursor import ManagedCursorCli, run_sign_in

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("RN_AGENT_CURSOR_BIN", raising=False)
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)

    with pytest.raises(RNAgentError, match="not installed"):
        run_sign_in(install=False, cli=ManagedCursorCli(root=tmp_path / "managed"))


# ---------------------------------------------------------------------------
# the managed runtime: install it ourselves, touch nothing of the developer's
# ---------------------------------------------------------------------------
def fake_package(target: Path, *, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    """A tarball shaped exactly like Cursor's: one top-level dir, one binary."""
    import io
    import tarfile as tar

    target.parent.mkdir(parents=True, exist_ok=True)
    with tar.open(target, "w:gz") as handle:
        payload = body.encode()
        info = tar.TarInfo("dist-package/cursor-agent")
        info.size = len(payload)
        info.mode = 0o644  # deliberately not executable; install must fix it
        handle.addfile(info, io.BytesIO(payload))
        extra = tar.TarInfo("dist-package/pty.node")
        extra.size = 0
        handle.addfile(extra, io.BytesIO(b""))
    return target


@dataclass
class FakeDownloader:
    """Serves a prepared file instead of reaching Cursor's CDN."""

    package: Path
    urls: list[str] = field(default_factory=list)

    def download(self, url, target, *, timeout=120.0, on_progress=None):  # noqa: ANN001
        self.urls.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.package.read_bytes())
        if on_progress is not None:
            on_progress(target.stat().st_size, target.stat().st_size)
        return target.stat().st_size


def managed(tmp_path: Path, **kwargs) -> ManagedCursorCli:
    return ManagedCursorCli(root=tmp_path / "managed", **kwargs)


def test_install_unpacks_into_rn_agents_own_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)
    downloader = FakeDownloader(package=fake_package(tmp_path / "pkg.tar.gz"))
    cli = managed(tmp_path, downloader=downloader)

    binary = cli.install("2026.08.11-test")

    assert binary == tmp_path / "managed" / "2026.08.11-test" / "cursor-agent"
    assert binary.is_file()
    # The top-level `dist-package/` is stripped, as Cursor's own installer does.
    assert (binary.parent / "pty.node").is_file()
    assert not (binary.parent / "dist-package").exists()
    # The tar entry was not executable; an unrunnable binary is not an install.
    assert os.access(binary, os.X_OK)
    assert "2026.08.11-test" in downloader.urls[0]


def test_install_never_writes_outside_its_own_root(tmp_path, monkeypatch):
    """No `~/.local/bin`, no shell profile, no PATH: that is the whole promise."""
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cli = managed(tmp_path, downloader=FakeDownloader(package=fake_package(tmp_path / "p.tgz")))

    cli.install("2026.08.11-test")

    assert not (home / ".local").exists()
    assert not (home / ".zshrc").exists()
    assert not (home / ".bashrc").exists()


def test_a_second_install_of_the_same_version_downloads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)
    downloader = FakeDownloader(package=fake_package(tmp_path / "p.tgz"))
    cli = managed(tmp_path, downloader=downloader)

    first = cli.install("2026.08.11-test")
    second = cli.install("2026.08.11-test")

    assert first == second
    assert len(downloader.urls) == 1


def test_a_failed_download_leaves_no_half_install(tmp_path, monkeypatch):
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)

    class Broken:
        def download(self, url, target, *, timeout=120.0, on_progress=None):  # noqa: ANN001
            raise TransportError("connection reset", hint="try again")

    cli = managed(tmp_path, downloader=Broken())

    with pytest.raises(TransportError):
        cli.install("2026.08.11-test")

    # Nothing that a later run could mistake for a finished install.
    assert cli.installed_versions() == ()
    assert cli.own_binary() is None


def test_a_package_without_the_binary_is_refused(tmp_path, monkeypatch):
    import io
    import tarfile as tar

    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)
    wrong = tmp_path / "wrong.tar.gz"
    with tar.open(wrong, "w:gz") as handle:
        info = tar.TarInfo("dist-package/README.md")
        info.size = 2
        handle.addfile(info, io.BytesIO(b"hi"))
    cli = managed(tmp_path, downloader=FakeDownloader(package=wrong))

    with pytest.raises(RNAgentError, match="cursor-agent"):
        cli.install("2026.08.11-test")

    assert cli.installed_versions() == ()


def test_an_install_that_escapes_the_archive_is_refused(tmp_path, monkeypatch):
    """The tarball comes off the network, so it is treated as untrusted input."""
    import io
    import tarfile as tar

    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)
    evil = tmp_path / "evil.tar.gz"
    with tar.open(evil, "w:gz") as handle:
        info = tar.TarInfo("../escaped.txt")
        info.size = 3
        handle.addfile(info, io.BytesIO(b"bad"))
    cli = managed(tmp_path, downloader=FakeDownloader(package=evil))

    with pytest.raises(RNAgentError, match="outside the destination"):
        cli.install("2026.08.11-test")

    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "managed" / "escaped.txt").exists()


def test_the_version_comes_from_cursors_own_installer(tmp_path):
    """"Latest" is the vendor's pointer, not a number this agent invents."""
    script = 'DOWNLOAD_URL="https://downloads.cursor.com/lab/2027.01.09-abcdef1/${OS}/..."'
    transport = FakeTransport().queue(body={}, text=script)
    cli = managed(tmp_path, transport=transport)

    assert cli.resolve_version() == "2027.01.09-abcdef1"


def test_an_unreachable_installer_falls_back_to_the_pinned_version(tmp_path):
    class Offline:
        def request(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise TransportError("no route to host", hint=None)

    assert managed(tmp_path, transport=Offline()).resolve_version() == PINNED_VERSION


def test_an_explicit_version_overrides_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("RN_AGENT_CURSOR_VERSION", "2026.01.01-pinned")

    assert managed(tmp_path).resolve_version() == "2026.01.01-pinned"


def test_require_refuses_to_download_when_told_not_to(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)

    with pytest.raises(RNAgentError, match="not installed"):
        managed(tmp_path).require(install=False)


def test_describe_says_who_installed_the_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)
    downloader = FakeDownloader(package=fake_package(tmp_path / "p.tgz"))
    cli = managed(tmp_path, downloader=downloader)

    assert cli.describe()["source"] == "not installed"

    cli.install("2026.08.11-test")
    described = cli.describe()

    assert described["source"] == "managed by rn-agent"
    assert described["managed_versions"] == ["2026.08.11-test"]


def test_a_binary_on_path_is_preferred_over_downloading(tmp_path, monkeypatch):
    """75 MB is not worth spending twice."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    theirs = bin_dir / "cursor-agent"
    theirs.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    theirs.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    class Refuses:
        def download(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("must not download when one is already here")

    assert managed(tmp_path, downloader=Refuses()).require() == theirs


# ---------------------------------------------------------------------------
# delegation: the deny list is the guard rail
# ---------------------------------------------------------------------------
def runner_for(project, **kwargs) -> CursorAgentRunner:
    rules = kwargs.pop("rules", None) or ProjectRules()
    return CursorAgentRunner(
        root=project.root,
        runner=CommandRunner(cwd=project.root),
        rules=rules,
        **kwargs,
    )


def test_lockfiles_and_secrets_are_denied_whatever_the_rules_say(project):
    runner = runner_for(project, allow_native=True, allow_dependencies=True)

    denied = runner.deny_list()

    for entry in ALWAYS_DENY:
        assert entry in denied
    assert "Write(**/.env*)" in denied
    assert "Read(**/.env*)" in denied


def test_the_projects_rules_become_cursor_permissions(project):
    runner = runner_for(project)

    denied = runner.deny_list()

    assert "Write(android/**)" in denied
    assert "Write(ios/**)" in denied
    assert "Write(package.json)" in denied


def test_the_allow_flags_lift_exactly_what_they_name(project):
    denied = runner_for(project, allow_native=True, allow_dependencies=True).deny_list()

    assert "Write(android/**)" not in denied
    assert "Write(package.json)" not in denied
    # And nothing else moved.
    assert "Write(**/yarn.lock)" in denied


def test_allow_native_paths_lifts_the_cursor_native_deny(project):
    runner = runner_for(
        project,
        rules=ProjectRules(allow_native_paths=("android/app/src/main/AndroidManifest.xml",)),
    )

    denied = runner.deny_list()

    assert "Write(android/**)" not in denied
    assert "Write(package.json)" in denied


def test_audit_honours_allow_native_paths(project):
    (project.root / "android").mkdir(exist_ok=True)
    (project.root / "android" / "build.gradle").write_text("// edited\n", encoding="utf-8")
    manifest = project.root / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("<manifest />\n", encoding="utf-8")
    runner = runner_for(
        project,
        rules=ProjectRules(allow_native_paths=("android/app/src/main/AndroidManifest.xml",)),
    )

    violations = runner.audit(
        ["android/app/src/main/AndroidManifest.xml", "android/build.gradle"]
    )

    assert {item.path for item in violations} == {"android/build.gradle"}


def test_the_deny_list_merges_into_the_developers_own_config(project):
    path = project.root / CLI_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"permissions": {"allow": ["Shell(git)"], "deny": ["Shell(curl)"]}}),
        encoding="utf-8",
    )
    runner = runner_for(project)

    written, previous = runner.write_permissions()
    payload = json.loads(written.read_text())

    # Their allow list is theirs.
    assert payload["permissions"]["allow"] == ["Shell(git)"]
    assert "Shell(curl)" in payload["permissions"]["deny"]
    assert "Write(package.json)" in payload["permissions"]["deny"]

    runner.restore_permissions(written, previous)
    assert json.loads(written.read_text())["permissions"]["deny"] == ["Shell(curl)"]


def test_a_config_we_created_is_removed_again(project):
    runner = runner_for(project)

    written, previous = runner.write_permissions()
    assert previous is None
    assert written.is_file()

    runner.restore_permissions(written, previous)
    assert not written.exists()


def test_an_unreadable_config_is_refused_not_overwritten(project):
    path = project.root / CLI_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    runner = runner_for(project)

    with pytest.raises(RNAgentError) as failure:
        runner.write_permissions()

    assert CLI_CONFIG in str(failure.value)
    assert path.read_text() == "{not json"  # untouched


def test_delegation_does_pass_force_because_that_is_the_point(cursor_cli, project):
    runner = runner_for(project)

    argv = runner.argv("do the thing")

    assert "--force" in argv
    assert argv[argv.index("--workspace") + 1] == str(project.root)


def test_the_task_carries_the_projects_rules(project):
    runner = runner_for(project)

    prompt = runner.prompt("extract the header")

    assert prompt.startswith("extract the header")
    assert "Do not add, remove or change any dependency" in prompt
    # Cursor reads the repo itself, so it is told not to touch git.
    assert "do not run git" in prompt


def test_the_audit_reads_the_tree_rather_than_trusting_a_claim(project):
    (project.root / "android").mkdir(exist_ok=True)
    (project.root / "android" / "build.gradle").write_text("// edited\n", encoding="utf-8")
    runner = runner_for(project)

    violations = runner.audit(["android/build.gradle", "yarn.lock"])

    rules = {violation.rule for violation in violations}
    assert "forbid_native_edits_without_confirmation" in rules
    assert "lockfile" in rules


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------
def run_delegate(project, **kwargs):
    context = project.scanned(command="delegate", assume_yes=True, **kwargs.pop("context", {}))
    command = DelegateCommand(context, **kwargs)
    command.quiet = True
    return command, command.run()


def test_delegate_needs_a_task(project):
    _, outcome = run_delegate(project)

    assert isinstance(outcome.error, RNAgentError)
    assert "needs a task" in outcome.error.message


def test_a_dirty_tree_is_refused_because_undo_depends_on_it(cursor_cli, project):
    project.git_init(dirty=True)

    _, outcome = run_delegate(project, task="tidy the header")

    assert isinstance(outcome.error, RNAgentError)
    assert "uncommitted changes" in outcome.error.message
    assert "--allow-dirty" in (outcome.error.hint or "")
    # Nothing ran.
    assert not cursor_cli.exists()


def test_a_dry_run_never_starts_the_agent(cursor_cli, project):
    project.git_init()

    command, outcome = run_delegate(
        project, task="tidy the header", context={"dry_run": True}
    )

    assert outcome.exit_code == 0
    assert not cursor_cli.exists()
    assert command.outcome is None
    # The preview is the deny list the agent would have been given.
    assert "Write(package.json)" in outcome.summary["denied"]


def test_a_violating_change_fails_the_command(cursor_cli, project, monkeypatch):
    """Cursor edited a native file the rules protect: that is a failure."""
    project.git_init()
    monkeypatch.setenv("STUB_WRITE_DIR", str(project.root))

    def native_edit(self, changed):  # noqa: ANN001 - test double
        return self.rules.violations(
            [__import__("rn_agent.models.proposal", fromlist=["FileEdit"]).FileEdit(
                path="android/build.gradle", content="// edited"
            )],
            allow_dependencies=self.allow_dependencies,
            allow_native=self.allow_native,
        )

    monkeypatch.setattr(CursorAgentRunner, "audit", native_edit)

    command, outcome = run_delegate(project, task="bump the gradle plugin", checks=())

    assert outcome.exit_code == 1
    assert command.outcome is not None
    assert command.outcome.violations
    assert outcome.summary["violations"]


def test_the_permission_file_is_restored_even_when_the_agent_fails(
    tmp_path, monkeypatch, project
):
    """A failed run must not leave rn-agent's deny list behind as config."""
    project.git_init()
    bin_dir = tmp_path / "fail-bin"
    bin_dir.mkdir()
    stub = bin_dir / "cursor-agent"
    stub.write_text("#!/bin/sh\necho 'agent exploded' >&2\nexit 2\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    _, outcome = run_delegate(project, task="do the thing")

    assert isinstance(outcome.error, RNAgentError)
    assert not (project.root / CLI_CONFIG).exists()


def test_no_trace_of_our_permission_file_is_left_behind(project):
    """A directory the developer never made must not appear in `git status`."""
    runner = runner_for(project)

    written, previous = runner.write_permissions()
    assert written.parent.is_dir()

    runner.restore_permissions(written, previous)

    assert not written.exists()
    assert not written.parent.exists()


def test_a_cursor_directory_we_did_not_create_survives(project):
    """Only an empty directory of ours is removed; theirs is left alone."""
    keep = project.root / ".cursor" / "rules.md"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("# their rules\n", encoding="utf-8")
    runner = runner_for(project)

    written, previous = runner.write_permissions()
    runner.restore_permissions(written, previous)

    assert keep.is_file()
    assert not written.exists()


def test_rn_agents_own_state_never_blocks_a_run(cursor_cli, project, monkeypatch):
    """`.rn-agent/` is not the developer's work, and `git restore` never touches it."""
    project.git_init()
    paths = project.paths()
    paths.ensure()
    (paths.agent_dir / "scratch.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STUB_WRITE_DIR", str(project.root))

    command, outcome = run_delegate(project, task="tidy the header", checks=())

    # It ran: the untracked `.rn-agent/` did not look like uncommitted work.
    assert outcome.exit_code == 0
    assert command.outcome is not None and command.outcome.ran


def test_real_uncommitted_work_still_blocks_the_run(cursor_cli, project):
    project.git_init()
    (project.root / "src" / "App.tsx").write_text("// mine, unsaved\n", encoding="utf-8")

    _, outcome = run_delegate(project, task="tidy the header")

    assert isinstance(outcome.error, RNAgentError)
    assert "uncommitted changes" in outcome.error.message


def test_whoami_names_which_cursor_binary_would_run(tmp_path, monkeypatch):
    """Three possible binaries, three possible versions: say which one it is."""
    from rn_agent.auth.keychain import select_backend
    from rn_agent.auth.session import status as auth_status
    from rn_agent.auth.store import CredentialStore
    from rn_agent.models.config import AIConfig

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    theirs = bin_dir / "cursor-agent"
    theirs.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    theirs.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    report = auth_status(AIConfig(provider="cursor"), CredentialStore(backend=select_backend()))

    assert report.tool_binary == str(theirs)
    assert report.tool_source == "your PATH"
    assert report.as_dict()["tool_binary"] == str(theirs)


def test_whoami_says_when_no_cursor_cli_is_installed(tmp_path, monkeypatch):
    from rn_agent.auth.keychain import select_backend
    from rn_agent.auth.session import status as auth_status
    from rn_agent.auth.store import CredentialStore
    from rn_agent.models.config import AIConfig

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("rn_agent.tools.cursor._vendor_install", lambda: None)

    report = auth_status(AIConfig(provider="cursor"), CredentialStore(backend=select_backend()))

    assert report.tool_binary is None
    assert report.tool_source == "not installed"
