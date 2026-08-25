"""The AI setup commands end to end: login, logout, whoami, provider, model.

These run the real Typer app. The credential backend is the 0600 file store
(forced for every test by ``_isolated_user_state``) and the provider transport is
a fake, so nothing here reaches a keychain, a network or the developer's config.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from rn_agent.cli.app import app

runner = CliRunner()

KEY = "sk-test-openai-0123456789abcd"
MODELS_OK = {"data": [{"id": "gpt-5"}, {"id": "gpt-5-mini"}]}


def invoke(*args: str, stdin: str | None = None):
    return runner.invoke(app, list(args), input=stdin)


def user_config(tmp_path: Path) -> dict:
    """Whatever the CLI wrote to the (redirected) user config."""
    path = tmp_path / "user-home" / "config.yaml"
    return yaml.safe_load(path.read_text()) if path.is_file() else {}


def secrets_text(tmp_path: Path) -> str:
    path = tmp_path / "user-home" / "credentials.enc.json"
    return path.read_text() if path.is_file() else ""


# --- provider --------------------------------------------------------------
def test_provider_list_shows_every_backend_and_its_env_var():
    result = invoke("provider", "--list")

    assert result.exit_code == 0
    for expected in ("anthropic", "openai", "ollama", "ANTHROPIC_API_KEY", "not needed"):
        assert expected in result.output


def test_provider_selects_and_clears_the_preference(tmp_path):
    result = invoke("provider", "anthropic")

    assert result.exit_code == 0
    assert user_config(tmp_path)["ai"]["provider"] == "anthropic"
    # No key yet, so it must say so rather than look ready.
    assert "rn-agent login anthropic" in result.output

    assert invoke("provider", "--clear").exit_code == 0
    assert user_config(tmp_path)["ai"]["provider"] is None


def test_provider_accepts_the_alias_developers_type(tmp_path):
    assert invoke("provider", "claude").exit_code == 0
    assert user_config(tmp_path)["ai"]["provider"] == "anthropic"


def test_provider_project_scope_writes_the_project_config(project):
    result = invoke("--path", str(project.root), "provider", "openai", "--project")

    assert result.exit_code == 0
    config = yaml.safe_load((project.root / ".rn-agent" / "config.yaml").read_text())
    assert config["ai"]["provider"] == "openai"


def test_unknown_provider_lists_the_valid_ones():
    result = invoke("provider", "copilot")

    assert result.exit_code == 10
    assert "anthropic" in result.output


# --- model -----------------------------------------------------------------
def test_model_sets_the_default_and_a_task_override(tmp_path):
    invoke("provider", "anthropic")

    assert invoke("model", "claude-sonnet-4-5").exit_code == 0
    assert invoke("model", "claude-opus-4-1", "--task", "migration").exit_code == 0

    ai = user_config(tmp_path)["ai"]
    assert ai["model"] == "claude-sonnet-4-5"
    assert ai["models"]["migration"] == "claude-opus-4-1"

    shown = invoke("model")
    assert "claude-sonnet-4-5" in shown.output
    assert "migration" in shown.output


def test_model_clear_removes_only_what_was_asked(tmp_path):
    invoke("provider", "anthropic")
    invoke("model", "claude-sonnet-4-5")
    invoke("model", "claude-opus-4-1", "--task", "migration")

    assert invoke("model", "--clear", "--task", "migration").exit_code == 0

    ai = user_config(tmp_path)["ai"]
    assert ai["models"]["migration"] is None
    assert ai["model"] == "claude-sonnet-4-5"


def test_model_rejects_an_unknown_task():
    result = invoke("model", "gpt-5", "--task", "migrateion")

    assert result.exit_code == 10
    assert "Known tasks" in result.output


def test_model_list_is_labelled_as_suggestions():
    invoke("provider", "anthropic")

    result = invoke("model", "--list")

    assert result.exit_code == 0
    assert "bundled suggestions" in result.output
    assert "claude-sonnet-4-5" in result.output


def test_model_list_remote_asks_the_account(wired_transport):
    invoke("provider", "openai")
    invoke("login", "openai", "--stdin", "--no-verify", stdin=KEY)
    wired_transport.queue(body=MODELS_OK)

    result = invoke("--json", "model", "--list", "--remote")

    payload = json.loads(result.output)
    assert payload["source"] == "OpenAI API"
    assert payload["models"] == ["gpt-5", "gpt-5-mini"]
    assert wired_transport.last["url"] == "https://api.openai.com/v1/models"
    assert wired_transport.last["headers"]["authorization"] == f"Bearer {KEY}"


# --- login -----------------------------------------------------------------
def test_login_verifies_then_stores_and_records_the_choice(tmp_path, wired_transport):
    wired_transport.queue(body=MODELS_OK)

    result = invoke("login", "openai", "--stdin", "--model", "gpt-5-mini", stdin=KEY)

    assert result.exit_code == 0, result.output
    assert "credential stored" in result.output
    assert wired_transport.last["url"] == "https://api.openai.com/v1/models"
    assert user_config(tmp_path)["ai"] == {"provider": "openai", "model": "gpt-5-mini"}
    assert KEY in secrets_text(tmp_path)  # the 0600 fallback, not the project


def test_login_with_a_rejected_key_stores_nothing(tmp_path, wired_transport):
    wired_transport.queue(status=401, body={"error": {"message": "invalid api key"}})

    result = invoke("login", "openai", "--stdin", stdin=KEY)

    assert result.exit_code == 10
    assert "invalid api key" in result.output
    assert KEY not in secrets_text(tmp_path)
    assert user_config(tmp_path) == {}


def test_login_offline_skips_verification(tmp_path, transport):
    result = invoke("login", "openai", "--stdin", "--no-verify", stdin=KEY)

    assert result.exit_code == 0
    assert "not verified" in result.output
    assert transport.calls == []
    assert KEY in secrets_text(tmp_path)


def test_login_dry_run_changes_nothing(tmp_path, wired_transport):
    wired_transport.queue(body=MODELS_OK)

    result = invoke("--dry-run", "login", "openai", "--stdin", stdin=KEY)

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert secrets_text(tmp_path) == ""
    assert user_config(tmp_path) == {}


def test_login_to_ollama_needs_no_key(tmp_path, wired_transport):
    wired_transport.queue(body={"models": [{"name": "llama3.1:latest"}]})

    result = invoke("login", "ollama")

    assert result.exit_code == 0, result.output
    assert user_config(tmp_path)["ai"]["provider"] == "ollama"
    assert secrets_text(tmp_path) == ""


def test_login_remembers_a_custom_api_host(tmp_path, wired_transport):
    wired_transport.queue(body={"models": [{"name": "llama3.1"}]})

    result = invoke("login", "ollama", "--base-url", "http://gpu.box:11434")

    assert result.exit_code == 0, result.output
    assert "http://gpu.box:11434" in result.output
    assert user_config(tmp_path)["ai"]["base_url"] == "http://gpu.box:11434"
    assert wired_transport.last["url"] == "http://gpu.box:11434/api/tags"

def device_transport(monkeypatch, transport):
    """A device-grant login, with no browser and no network."""
    transport.queue(
        body={
            "device_code": "dev",
            "user_code": "WXYZ-1234",
            "verification_url": "https://www.google.com/device",
            "interval": 0,
            "expires_in": 900,
        }
    )
    transport.queue(body={"access_token": "ya29.tok", "refresh_token": "1//r", "expires_in": 3600})
    monkeypatch.setattr("rn_agent.auth.oauth.default_transport", lambda: transport)
    return transport


def test_login_vertex_records_the_project_that_pays(tmp_path, monkeypatch, transport):
    """Vertex bills a Cloud project, so the flag has to survive the login."""
    device_transport(monkeypatch, transport)
    client = tmp_path / "client_secret.json"
    client.write_text('{"installed": {"client_id": "cid.apps", "client_secret": "shh"}}')

    result = invoke(
        "login",
        "vertex",
        "--client-file",
        str(client),
        "--device",
        "--cloud-project",
        "billed-project",
        "--region",
        "us-east5",
        "--no-verify",
    )

    assert result.exit_code == 0, result.output
    written = user_config(tmp_path)["ai"]
    assert written["provider"] == "vertex"
    assert written["project"] == "billed-project"
    assert written["region"] == "us-east5"
    # It says what will actually happen: a code to type, not a browser it
    # cannot open.
    assert "WXYZ-1234" in result.output
    assert "No browser here" in result.output


def test_login_vertex_says_it_is_a_google_sign_in_not_an_anthropic_key():
    """Without an OAuth client it must refuse - and name the file to download."""
    result = invoke("login", "vertex", "--cloud-project", "p", "--no-verify")

    assert result.exit_code == 10
    assert "Google" in result.output
    assert "--client-file" in result.output
    # No Anthropic key is involved on this path, so none may be suggested.
    assert "ANTHROPIC_API_KEY" not in result.output


def test_login_vertex_shares_the_google_session(tmp_path, monkeypatch, transport):
    """One Google account: signing in for Vertex also connects Gemini."""
    device_transport(monkeypatch, transport)
    client = tmp_path / "client_secret.json"
    client.write_text('{"installed": {"client_id": "cid.apps"}}')

    invoke("login", "vertex", "--client-file", str(client), "--device", "--no-verify")
    result = invoke("--json", "whoami")

    payload = json.loads(result.output)
    assert "google" in payload["stored_providers"]



def test_login_without_a_key_and_without_a_tty_explains_the_options(tmp_path):
    result = invoke("login", "anthropic")

    assert result.exit_code == 10
    assert "--stdin" in result.output
    assert "ANTHROPIC_API_KEY" in result.output


def test_login_json_output_is_machine_readable(wired_transport):
    wired_transport.queue(body=MODELS_OK)

    result = invoke("--json", "login", "openai", "--stdin", stdin=KEY)

    payload = json.loads(result.output)
    assert payload["provider"] == "openai"
    assert payload["stored"] is True
    assert payload["verified"] is True
    assert payload["credential"] == "…abcd"
    assert KEY not in result.output


def test_a_login_inside_a_project_never_writes_a_secret_into_it(project, wired_transport):
    wired_transport.queue(body=MODELS_OK)

    result = invoke("--path", str(project.root), "login", "openai", "--stdin", stdin=KEY)

    assert result.exit_code == 0, result.output
    for path in project.root.rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(errors="ignore"), path


# --- whoami ----------------------------------------------------------------
def test_whoami_is_non_zero_until_a_provider_is_configured(tmp_path):
    result = invoke("whoami")

    assert result.exit_code == 10
    assert "not configured" in result.output
    assert "rn-agent login" in result.output


def test_whoami_reports_the_stored_credential(tmp_path):
    invoke("login", "openai", "--stdin", "--no-verify", stdin=KEY)

    result = invoke("whoami")

    assert result.exit_code == 0
    assert "openai" in result.output
    assert "…abcd" in result.output
    assert KEY not in result.output


def test_whoami_reports_an_environment_key_as_such(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-0123456789")
    invoke("provider", "openai")

    result = invoke("whoami")

    assert result.exit_code == 0
    assert "OPENAI_API_KEY (environment)" in result.output


def test_whoami_check_asks_the_provider(wired_transport):
    invoke("login", "openai", "--stdin", "--no-verify", stdin=KEY)
    wired_transport.queue(body=MODELS_OK)

    result = invoke("--json", "whoami", "--check")

    payload = json.loads(result.output)
    assert payload["verified"] is True
    assert "2 model(s)" in payload["detail"]


def test_whoami_check_surfaces_a_rejected_key(wired_transport):
    invoke("login", "openai", "--stdin", "--no-verify", stdin=KEY)
    wired_transport.queue(status=401, body={"error": {"message": "invalid api key"}})

    result = invoke("whoami", "--check")

    assert result.exit_code == 10
    assert "invalid api key" in result.output


# --- logout ----------------------------------------------------------------
def test_logout_forgets_the_key_and_is_idempotent(tmp_path):
    invoke("login", "openai", "--stdin", "--no-verify", stdin=KEY)

    first = invoke("logout", "openai")
    assert first.exit_code == 0
    assert "forgot openai" in first.output
    assert KEY not in secrets_text(tmp_path)

    second = invoke("logout", "openai")
    assert second.exit_code == 0
    assert "nothing was stored" in second.output


def test_logout_all_clears_every_provider(tmp_path):
    invoke("login", "openai", "--stdin", "--no-verify", stdin=KEY)
    invoke("login", "anthropic", "--stdin", "--no-verify", stdin="sk-ant-test-0123456789abcdef")

    result = invoke("--json", "logout", "--all")

    payload = json.loads(result.output)
    assert set(payload["removed"]) == {"openai", "anthropic"}
    assert secrets_text(tmp_path) in ("", "{}\n")


def test_logout_warns_that_an_environment_key_still_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-0123456789")
    invoke("login", "openai", "--stdin", "--no-verify", stdin=KEY)

    result = invoke("logout", "openai")

    assert "OPENAI_API_KEY is still set" in result.output


def test_logout_dry_run_keeps_the_credential(tmp_path):
    invoke("login", "openai", "--stdin", "--no-verify", stdin=KEY)

    result = invoke("--dry-run", "logout", "openai")

    assert result.exit_code == 0
    assert "would forget" in result.output
    assert KEY in secrets_text(tmp_path)


# --- info ------------------------------------------------------------------
def test_info_shows_the_ai_setup_after_a_login(project):
    invoke("--path", str(project.root), "login", "openai", "--stdin", "--no-verify", stdin=KEY)

    result = invoke("--path", str(project.root), "info")

    assert result.exit_code == 0
    assert "openai" in result.output
    assert "rn-agent whoami" in result.output


# --- a provider that needs no credential but accepts one -------------------
def test_an_explicit_key_is_stored_even_when_the_provider_needs_none(tmp_path):
    """Cursor's CLI holds its own session, so `login cursor` never prompts for a
    key - but `--stdin` is how CI supplies CURSOR_API_KEY, and dropping it would
    make the documented CI path silently do nothing."""
    result = invoke("login", "cursor", "--stdin", "--no-verify", stdin="cur_key_0123456789")

    assert result.exit_code == 0, result.output
    assert "cur_key_0123456789" in secrets_text(tmp_path)
    payload = json.loads(invoke("--json", "whoami").output)
    assert payload["provider"] == "cursor"
    assert "cursor" in payload["stored_providers"]
    # Masked in the report, never echoed.
    assert "cur_key_0123456789" not in result.output


def test_no_key_is_ever_prompted_for_a_tool_provider(tmp_path):
    """With no --api-key and no --stdin there is nothing to ask for: the tool is
    already signed in, and a prompt would imply otherwise."""
    result = invoke("login", "cursor", "--no-verify")

    assert result.exit_code == 0, result.output
    assert "API key" not in result.output
    assert secrets_text(tmp_path) == ""
    assert "cursor-agent login" in result.output
