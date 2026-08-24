"""Credential storage and the login/whoami/logout policy.

The keychain backends are driven through a scripted runner that emulates
``security``, ``secret-tool`` and PowerShell, so the argv, the stdin and the
"item not found" exit codes are all asserted without touching a real keychain.
The file backend is exercised for real, because it is the fallback developers
without a keyring actually get.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from rn_agent.ai.registry import resolve_spec
from rn_agent.auth import session
from rn_agent.auth.keychain import (
    DpapiBackend,
    FileBackend,
    MacKeychainBackend,
    NullBackend,
    SecretServiceBackend,
    select_backend,
    validate_secret,
)
from rn_agent.auth.store import CredentialStore
from rn_agent.errors import ProviderError
from rn_agent.models.config import AIConfig
from rn_agent.runner.command_runner import CommandResult, CommandRunner

KEY = "sk-ant-test-0123456789abcdef"
Handler = Callable[[list[str], str | None], tuple[int, str, str]]


# --- a runner that pretends to be the OS ------------------------------------
@dataclass
class ScriptedRunner:
    """Stands in for :class:`CommandRunner`, recording argv and stdin."""

    handler: Handler
    tools: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def which(self, executable: str) -> str | None:
        return f"/usr/bin/{executable}" if executable in self.tools else None

    def available(self, executable: str) -> bool:
        return executable in self.tools

    def run(self, argv: Any, *, input_text: str | None = None, **kwargs: Any) -> CommandResult:
        parts = [str(part) for part in argv]
        self.calls.append({"argv": parts, "input": input_text, **kwargs})
        code, out, err = self.handler(parts, input_text)
        return CommandResult(
            argv=tuple(parts),
            returncode=code,
            stdout=out,
            stderr=err,
            duration_ms=0,
            cwd=".",
        )

    @property
    def argv_text(self) -> str:
        return " ".join(" ".join(call["argv"]) for call in self.calls)


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def mac_handler(items: dict[str, str]) -> Handler:
    def handle(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        if argv[:2] == ["security", "-i"]:
            words = (stdin or "").split()
            items[_flag(words, "-a")] = _flag(words, "-w")
            return 0, "security> \n", ""
        if "find-generic-password" in argv:
            account = _flag(argv, "-a")
            if account in items:
                return 0, f"{items[account]}\n", ""
            return 44, "", "security: SecKeychainSearchCopyNext: The specified item could not be found."
        if "delete-generic-password" in argv:
            return (0, "", "") if items.pop(_flag(argv, "-a"), None) else (44, "", "not found")
        raise AssertionError(f"unexpected command: {argv}")

    return handle


def secret_tool_handler(items: dict[str, str]) -> Handler:
    def handle(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        account = _flag(argv, "account")
        if argv[1] == "store":
            items[account] = (stdin or "").strip()
            return 0, "", ""
        if argv[1] == "lookup":
            return (0, items[account], "") if account in items else (1, "", "No such secret item")
        if argv[1] == "clear":
            items.pop(account, None)
            return 0, "", ""
        raise AssertionError(f"unexpected command: {argv}")

    return handle


def dpapi_handler() -> Handler:
    """Fake DPAPI: a reversible transform stands in for user-scoped encryption.

    It must genuinely hide the plaintext, otherwise the "no key in the file"
    assertion below would pass for the wrong reason.
    """

    prefix = "01000000d08c9d"

    def handle(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        payload = (stdin or "").strip()
        if "ConvertFrom-SecureString" in argv[-1]:
            return 0, f"{prefix}{payload[::-1]}\n", ""
        return 0, f"{payload.removeprefix(prefix)[::-1]}\n", ""

    return handle


def file_store(tmp_path: Path) -> CredentialStore:
    runner = CommandRunner(cwd=tmp_path)
    backend = FileBackend(runner=runner, secrets_file=tmp_path / "secrets.json")
    return CredentialStore(backend=backend, index_file=tmp_path / "index.json")


# --- secret validation -----------------------------------------------------
@pytest.mark.parametrize("bad", ["", "   ", "short", "has space in it", "line\nbreak", 'quote"key'])
def test_credentials_that_are_not_api_keys_are_refused(bad):
    with pytest.raises(ProviderError):
        validate_secret(bad)


def test_a_real_looking_key_is_accepted_and_trimmed():
    assert validate_secret(f"  {KEY}\n") == KEY


# --- file backend ----------------------------------------------------------
def test_file_backend_roundtrip_is_private_to_the_user(tmp_path):
    store = file_store(tmp_path)

    store.store("openai", "sk-test-openai-0123456789")

    secrets = tmp_path / "secrets.json"
    assert secrets.is_file()
    assert oct(secrets.stat().st_mode)[-3:] == "600"
    assert store.backend.get("openai") == "sk-test-openai-0123456789"
    assert store.backend.get("anthropic") is None


def test_storing_records_an_index_without_the_secret(tmp_path):
    store = file_store(tmp_path)
    store.store("openai", KEY)

    index = json.loads((tmp_path / "index.json").read_text())
    assert index["providers"]["openai"]["backend"] == "file"
    assert KEY not in json.dumps(index)
    assert [entry.provider for entry in store.stored()] == ["openai"]
    assert store.has_stored("openai")


def test_forgetting_removes_the_secret_and_the_index_entry(tmp_path):
    store = file_store(tmp_path)
    store.store("openai", KEY)

    assert store.forget("openai") is True
    assert store.backend.get("openai") is None
    assert store.stored() == ()
    # Idempotent: forgetting twice is not an error, it is just "nothing there".
    assert store.forget("openai") is False


def test_a_backend_that_loses_the_secret_fails_loudly(tmp_path):
    class ForgetfulBackend(FileBackend):
        def set(self, account: str, secret: str) -> None:
            return None

    backend = ForgetfulBackend(runner=CommandRunner(cwd=tmp_path), secrets_file=tmp_path / "s.json")
    store = CredentialStore(backend=backend, index_file=tmp_path / "i.json")

    with pytest.raises(ProviderError, match="different value"):
        store.store("openai", KEY)


# --- environment beats the keychain, and says so ---------------------------
def test_the_environment_variable_wins_and_is_reported(tmp_path, monkeypatch):
    store = file_store(tmp_path)
    store.store("openai", "sk-stored-key-0123456789")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-0123456789")

    credential = store.resolve(resolve_spec("openai"))

    assert credential is not None
    assert credential.value == "sk-env-key-0123456789"
    assert credential.from_env
    assert credential.describe() == "OPENAI_API_KEY (environment)"
    assert credential.masked == "…6789"


def test_a_missing_credential_is_none_unless_it_is_required(tmp_path):
    store = file_store(tmp_path)

    assert store.resolve(resolve_spec("anthropic")) is None
    with pytest.raises(ProviderError, match="no credential for anthropic"):
        store.require(resolve_spec("anthropic"))
    # Ollama runs locally, so "no key" is the normal case, not a failure.
    assert store.require(resolve_spec("ollama")) is None


# --- macOS keychain --------------------------------------------------------
def test_macos_backend_keeps_the_secret_out_of_argv(tmp_path):
    items: dict[str, str] = {}
    runner = ScriptedRunner(mac_handler(items), tools={"security"})
    backend = MacKeychainBackend(runner=runner, secrets_file=tmp_path / "unused.json")

    backend.set("anthropic", KEY)

    assert items == {"anthropic": KEY}
    assert KEY not in runner.argv_text, "the key must travel on stdin, never in argv"
    assert KEY in (runner.calls[0]["input"] or "")
    assert backend.get("anthropic") == KEY
    assert backend.available()


def test_macos_backend_reads_item_not_found_as_no_credential(tmp_path):
    runner = ScriptedRunner(mac_handler({}), tools={"security"})
    backend = MacKeychainBackend(runner=runner, secrets_file=tmp_path / "unused.json")

    assert backend.get("anthropic") is None
    assert backend.delete("anthropic") is False
    # An expected miss must not be logged as a failure.
    assert runner.calls[0]["quiet"] is True


def test_macos_backend_reports_a_locked_keychain(tmp_path):
    def locked(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        return 36, "", "security: SecKeychainUnlock: User interaction is not allowed."

    backend = MacKeychainBackend(
        runner=ScriptedRunner(locked, tools={"security"}), secrets_file=tmp_path / "unused.json"
    )

    with pytest.raises(ProviderError) as failure:
        backend.get("anthropic")
    assert "could not read" in failure.value.message
    assert "RN_AGENT_KEYCHAIN=file" in (failure.value.hint or "")


# --- Secret Service --------------------------------------------------------
def test_secret_service_backend_uses_stdin_and_attributes(tmp_path):
    items: dict[str, str] = {}
    runner = ScriptedRunner(secret_tool_handler(items), tools={"secret-tool"})
    backend = SecretServiceBackend(runner=runner, secrets_file=tmp_path / "unused.json")

    backend.set("openai", KEY)

    store_call = runner.calls[0]
    assert store_call["argv"][:2] == ["secret-tool", "store"]
    assert "--label=rn-agent openai" in store_call["argv"]
    assert store_call["input"] == KEY
    assert KEY not in runner.argv_text
    assert backend.get("openai") == KEY
    assert backend.delete("openai") is True
    assert backend.get("openai") is None


# --- Windows DPAPI ---------------------------------------------------------
def test_dpapi_backend_stores_ciphertext_not_the_key(tmp_path):
    secrets = tmp_path / "secrets.json"
    runner = ScriptedRunner(dpapi_handler(), tools={"powershell"})
    backend = DpapiBackend(runner=runner, secrets_file=secrets)

    backend.set("openai", KEY)

    stored = json.loads(secrets.read_text())["openai"]
    assert stored != KEY
    assert stored.startswith("01000000d08c9d")
    assert KEY not in secrets.read_text()
    assert KEY not in runner.argv_text
    assert backend.get("openai") == KEY


# --- backend selection -----------------------------------------------------
@pytest.mark.parametrize(
    ("platform", "tools", "expected"),
    [
        ("darwin", {"security"}, "keychain-macos"),
        ("darwin", set(), "file"),
        ("linux", {"secret-tool"}, "secret-service"),
        ("linux", set(), "file"),
        ("win32", {"powershell"}, "dpapi"),
    ],
)
def test_backend_selection_follows_the_platform(tmp_path, platform, tools, expected):
    runner = ScriptedRunner(mac_handler({}), tools=tools)

    backend = select_backend(
        runner=runner, override="auto", platform=platform, secrets_file=tmp_path / "s.json"
    )

    assert backend.name == expected


def test_the_environment_can_force_or_disable_storage(tmp_path):
    runner = ScriptedRunner(mac_handler({}), tools={"security"})
    forced = select_backend(runner=runner, override="file", secrets_file=tmp_path / "s.json")
    assert isinstance(forced, FileBackend)
    assert forced.secure is False

    disabled = select_backend(runner=runner, override="none", secrets_file=tmp_path / "s.json")
    assert isinstance(disabled, NullBackend)
    assert disabled.get("openai") is None
    with pytest.raises(ProviderError, match="storage is disabled"):
        disabled.set("openai", KEY)

    with pytest.raises(ProviderError, match="unknown credential backend"):
        select_backend(runner=runner, override="pigeon-post", secrets_file=tmp_path / "s.json")


# --- session policy --------------------------------------------------------
def test_status_without_a_provider_is_not_ready(tmp_path):
    result = session.status(AIConfig(), file_store(tmp_path))

    assert result.provider is None
    assert result.ready is False
    assert result.verified is None
    assert result.backend_location.endswith("secrets.json")


def test_status_reports_the_stored_credential(tmp_path):
    store = file_store(tmp_path)
    store.store("openai", KEY)

    result = session.status(AIConfig(provider="openai", model="gpt-5-mini"), store)

    assert result.ready is True
    assert result.model == "gpt-5-mini"
    assert result.credential_source == "file"
    assert result.stored == ("openai",)
    assert result.as_dict()["credential"] == "…cdef"


def test_status_is_ready_for_ollama_without_any_key(tmp_path):
    result = session.status(AIConfig(provider="ollama"), file_store(tmp_path))

    assert result.requires_credential is False
    assert result.ready is True


def test_status_check_records_a_failed_verification(tmp_path, transport):
    store = file_store(tmp_path)
    store.store("openai", KEY)
    transport.queue(status=401, body={"error": {"message": "invalid api key"}})

    result = session.status(
        AIConfig(provider="openai"), store, check=True, transport=transport
    )

    assert result.verified is False
    assert "invalid api key" in (result.detail or "")


def test_login_verifies_before_it_stores(tmp_path, transport):
    """A key the provider rejects must never reach the keychain."""
    store = file_store(tmp_path)
    transport.queue(status=401, body={"error": {"message": "invalid api key"}})

    with pytest.raises(ProviderError, match="invalid api key"):
        session.login(
            provider="openai",
            config=AIConfig(),
            store=store,
            secret="sk-bad-key-0123456789",
            transport=transport,
        )

    assert store.backend.get("openai") is None
    assert store.stored() == ()


def test_login_stores_after_a_successful_check(tmp_path, transport):
    store = file_store(tmp_path)
    transport.queue(body={"data": [{"id": "gpt-5"}, {"id": "gpt-5-mini"}]})

    result = session.login(
        provider="openai",
        config=AIConfig(),
        store=store,
        secret=KEY,
        model="gpt-5-mini",
        transport=transport,
    )

    assert result.stored is True
    assert result.status.verified is True
    assert result.status.model == "gpt-5-mini"
    assert store.backend.get("openai") == KEY
    assert result.warnings == ()


def test_login_warns_when_the_model_is_not_in_the_catalogue(tmp_path, transport):
    store = file_store(tmp_path)
    transport.queue(body={"data": [{"id": "gpt-4.1"}]})

    result = session.login(
        provider="openai",
        config=AIConfig(),
        store=store,
        secret=KEY,
        model="gpt-5",
        transport=transport,
    )

    assert any("not in this account's catalogue" in warning for warning in result.warnings)
    assert result.stored is True


def test_login_in_dry_run_verifies_but_stores_nothing(tmp_path, transport):
    store = file_store(tmp_path)
    transport.queue(body={"data": [{"id": "gpt-5"}]})

    result = session.login(
        provider="openai",
        config=AIConfig(),
        store=store,
        secret=KEY,
        verify=True,
        dry_run=True,
        transport=transport,
    )

    assert result.stored is False
    assert store.backend.get("openai") is None
    assert any("dry run" in warning for warning in result.warnings)


def test_login_without_a_key_uses_the_environment_and_says_so(tmp_path, monkeypatch, transport):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-0123456789")
    store = file_store(tmp_path)

    result = session.login(
        provider="openai", config=AIConfig(), store=store, verify=False, transport=transport
    )

    assert result.stored is False
    assert any("OPENAI_API_KEY" in warning for warning in result.warnings)
    assert store.backend.get("openai") is None


def test_login_without_any_credential_explains_the_options(tmp_path):
    with pytest.raises(ProviderError) as failure:
        session.login(provider="anthropic", config=AIConfig(), store=file_store(tmp_path))

    assert "no API key given" in failure.value.message
    assert "ANTHROPIC_API_KEY" in (failure.value.hint or "")


def test_login_to_ollama_needs_no_credential_at_all(tmp_path, transport):
    transport.queue(body={"models": [{"name": "llama3.1:latest"}]})

    result = session.login(
        provider="ollama", config=AIConfig(), store=file_store(tmp_path), transport=transport
    )

    assert result.status.ready is True
    assert result.stored is False
    assert result.identity is not None
    assert result.identity.models == ("llama3.1:latest",)


def test_login_reports_the_host_it_actually_verified_against(tmp_path, transport):
    """The panel must show the --base-url just used, not the stale default."""
    transport.queue(body={"models": [{"name": "llama3.1"}]})

    result = session.login(
        provider="ollama",
        config=AIConfig(),
        store=file_store(tmp_path),
        base_url="http://gpu.box:11434",
        transport=transport,
    )

    assert result.status.base_url == "http://gpu.box:11434"
    assert transport.last["url"] == "http://gpu.box:11434/api/tags"


# --- where secrets are allowed to live -------------------------------------
def test_credential_files_can_never_land_inside_a_project(tmp_path):
    """§7: nothing secret may sit in a directory a developer might commit."""
    from rn_agent.core.paths import (
        AgentPaths,
        user_config_dir,
        user_credentials_file,
        user_secrets_file,
    )

    project = AgentPaths.for_project(tmp_path / "app")

    for path in (user_credentials_file(), user_secrets_file()):
        assert path.parent == user_config_dir()
        assert project.agent_dir not in path.parents
        assert project.project_root not in path.parents
