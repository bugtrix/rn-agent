"""Authentication: the mechanism, and the honesty about it.

These tests defend the promise the login UX makes. A provider that offers no
OAuth to third-party tools must *say* so and ask for a key; a provider that does
offer OAuth must run a real PKCE flow; and no path may store a token in the
project or hand a mislabelled credential to a provider.
"""

from __future__ import annotations

import tempfile
import threading
import urllib.request
from pathlib import Path

import pytest

from rn_agent.auth.authenticator import AuthMethod
from rn_agent.auth.manager import ANTHROPIC_NOTE, AuthenticationManager, auth_for
from rn_agent.auth.methods import (
    GOOGLE_SCOPES,
    ApiKeyAuthenticator,
    LocalAuthenticator,
    OAuthAuthenticator,
    google_client,
)
from rn_agent.auth.oauth import (
    OAuthClient,
    OAuthFlow,
    OAuthTokens,
    TokenStore,
    challenge_for,
    load_client_file,
    new_verifier,
)
from rn_agent.auth.store import CredentialStore
from rn_agent.errors import ProviderError
from rn_agent.net.http import HttpResponse

KEY = "sk-ant-test-0123456789abcdef"


class Backend:
    """A keychain stand-in that rejects anything a real backend would mangle."""

    name = "file"
    label = "test backend"
    secure = True

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self.items.get(account)

    def set(self, account: str, secret: str) -> None:
        # macOS passes secrets through a `security -i` script, so a value with
        # whitespace or quotes would break the write. Enforce the contract.
        assert " " not in secret and '"' not in secret and "\n" not in secret
        self.items[account] = secret

    def delete(self, account: str) -> bool:
        return self.items.pop(account, None) is not None


class TokenTransport:
    """A token endpoint that records what it was sent."""

    def __init__(self, body: dict | None = None, status: int = 200) -> None:
        self.body = body or {
            "access_token": "ya29.access",
            "refresh_token": "1//refresh",
            "expires_in": 3600,
            "scope": " ".join(GOOGLE_SCOPES),
            "token_type": "Bearer",
        }
        self.status = status
        self.payloads: list[dict] = []

    def request(self, method, url, *, headers, payload=None, timeout=120.0):
        self.payloads.append(payload or {})
        return HttpResponse(status=self.status, body=self.body, text="")


def browser_that_completes(state_holder: dict) -> object:
    """A fake browser that plays the provider's part: redirect back with a code."""

    def opener(url: str) -> None:
        import urllib.parse as parse

        state_holder["url"] = url
        query = parse.parse_qs(parse.urlparse(url).query)
        redirect = query["redirect_uri"][0]
        state = query["state"][0]
        threading.Thread(
            target=lambda: urllib.request.urlopen(f"{redirect}?code=code-123&state={state}").read()
        ).start()

    return opener


# ---------------------------------------------------------------------------
# the capability table is the UX contract
# ---------------------------------------------------------------------------
def test_anthropic_is_api_key_and_says_why():
    entry = auth_for("anthropic")

    assert entry.method is AuthMethod.API_KEY
    # The note is what stops the UI implying a Pro/Max subscription is in use.
    assert "Claude Code" in ANTHROPIC_NOTE
    assert entry.unsupported_note == ANTHROPIC_NOTE
    assert "console.anthropic.com" in (entry.console_url or "")


def test_openai_is_api_key_and_says_why():
    entry = auth_for("openai")

    assert entry.method is AuthMethod.API_KEY
    assert "identity" in (entry.unsupported_note or "")


def test_google_is_oauth_with_the_documented_scopes():
    entry = auth_for("google")

    assert entry.method is AuthMethod.OAUTH
    assert entry.allows_api_key is True  # a key is still accepted, and labelled
    client = google_client("cid", "secret")
    assert set(client.scopes) == set(GOOGLE_SCOPES)
    assert client.authorize_url.startswith("https://accounts.google.com/")
    assert dict(client.extra_authorize_params)["access_type"] == "offline"


def test_ollama_needs_no_credential():
    entry = auth_for("ollama")

    assert entry.method is AuthMethod.NONE
    authenticator = LocalAuthenticator(provider="ollama", host_env="OLLAMA_HOST")
    assert authenticator.credential() is None
    assert authenticator.state().connected is True
    assert authenticator.state().status_word == "available locally"


def test_an_unknown_provider_lists_the_known_ones():
    with pytest.raises(ProviderError) as failure:
        auth_for("copilot")

    assert "anthropic" in (failure.value.hint or "")


def test_every_provider_declares_a_capability():
    manager = AuthenticationManager(backend=Backend())

    for entry in manager.providers():
        capability = manager.capability(entry.provider)
        assert capability.label
        assert capability.method is entry.method
        # An API-key provider must justify itself; OAuth needs no excuse.
        if capability.method is AuthMethod.API_KEY:
            assert capability.unsupported_note


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------
def key_store(backend: Backend, tmp_path: Path | None = None) -> CredentialStore:
    """The real credential store on a fake backend.

    Going through the store - not the backend - is what keeps the provider index
    (which ``logout --all`` enumerates) in step with what was written.
    """
    index = (tmp_path or Path(tempfile.mkdtemp())) / "credentials.json"
    return CredentialStore(backend=backend, index_file=index)


def test_a_key_is_stored_read_back_and_masked():
    backend = Backend()
    authenticator = ApiKeyAuthenticator(
        provider="anthropic", store=key_store(backend), env_var="ANTHROPIC_API_KEY"
    )

    outcome = authenticator.login(secret=KEY)

    assert outcome.stored is True
    assert authenticator.credential() == KEY
    assert outcome.state.masked and KEY not in outcome.state.masked
    assert authenticator.state().connected is True


def test_the_environment_wins_and_is_reported_as_such(monkeypatch):
    backend = Backend()
    authenticator = ApiKeyAuthenticator(
        provider="anthropic", store=key_store(backend), env_var="ANTHROPIC_API_KEY"
    )
    authenticator.login(secret=KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-9876543210abcdef")

    state = authenticator.state()

    assert state.source == "env"
    assert "ANTHROPIC_API_KEY" in (state.label or "")
    assert authenticator.credential() == "sk-ant-env-9876543210abcdef"


def test_a_dry_run_login_stores_nothing():
    backend = Backend()
    authenticator = ApiKeyAuthenticator(provider="anthropic", store=key_store(backend))

    outcome = authenticator.login(secret=KEY, dry_run=True)

    assert outcome.stored is False
    assert backend.items == {}


def test_login_without_a_key_explains_where_to_get_one():
    authenticator = ApiKeyAuthenticator(
        provider="anthropic",
        store=key_store(Backend()),
        env_var="ANTHROPIC_API_KEY",
        console_url="https://console.anthropic.com/settings/keys",
    )

    with pytest.raises(ProviderError) as failure:
        authenticator.login()

    assert "console.anthropic.com" in (failure.value.hint or "")
    assert "ANTHROPIC_API_KEY" in (failure.value.hint or "")


def test_logout_forgets_the_key():
    backend = Backend()
    authenticator = ApiKeyAuthenticator(provider="anthropic", store=key_store(backend))
    authenticator.login(secret=KEY)

    assert authenticator.logout() is True
    assert authenticator.credential() is None
    assert authenticator.logout() is False


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------
def test_pkce_challenge_is_s256():
    import base64
    import hashlib

    verifier = new_verifier()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )

    assert challenge_for(verifier) == expected
    assert 43 <= len(verifier) <= 128


def test_the_oauth_flow_completes_over_loopback():
    transport = TokenTransport()
    holder: dict[str, str] = {}
    flow = OAuthFlow(
        client=google_client("client-id", "client-secret"),
        transport=transport,
        opener=browser_that_completes(holder),
        timeout=20,
    )

    tokens, url = flow.run(account="dev@example.com")

    assert "code_challenge_method=S256" in holder["url"]
    assert "127.0.0.1" in holder["url"]
    assert url == holder["url"]
    assert tokens.access_token == "ya29.access"
    assert tokens.refresh_token == "1//refresh"
    assert tokens.account == "dev@example.com"
    assert not tokens.expired
    sent = transport.payloads[0]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "code-123"
    assert "code_verifier" in sent


def test_a_callback_with_the_wrong_state_is_refused():
    def evil_opener(url: str) -> None:
        import urllib.parse as parse

        redirect = parse.parse_qs(parse.urlparse(url).query)["redirect_uri"][0]
        threading.Thread(
            target=lambda: urllib.request.urlopen(f"{redirect}?code=x&state=not-ours").read()
        ).start()

    flow = OAuthFlow(
        client=google_client("client-id", None),
        transport=TokenTransport(),
        opener=evil_opener,
        timeout=20,
    )

    with pytest.raises(ProviderError, match="did not match"):
        flow.run()


def test_a_rejected_token_request_reports_the_provider_reason():
    transport = TokenTransport(
        body={"error": "invalid_grant", "error_description": "expired code"}, status=400
    )
    flow = OAuthFlow(
        client=google_client("client-id", None),
        transport=transport,
        opener=browser_that_completes({}),
        timeout=20,
    )

    with pytest.raises(ProviderError) as failure:
        flow.run()

    assert "expired code" in (failure.value.hint or "")


def test_refresh_keeps_the_refresh_token():
    transport = TokenTransport(
        body={"access_token": "ya29.new", "expires_in": 3600, "token_type": "Bearer"}
    )
    flow = OAuthFlow(client=google_client("cid", None), transport=transport)
    old = OAuthTokens(access_token="old", refresh_token="1//keep", expires_at=0.0)

    refreshed = flow.refresh(old)

    assert refreshed.access_token == "ya29.new"
    assert refreshed.refresh_token == "1//keep"
    assert transport.payloads[0]["grant_type"] == "refresh_token"


def test_refresh_without_a_refresh_token_says_to_sign_in_again():
    flow = OAuthFlow(client=google_client("cid", None), transport=TokenTransport())

    with pytest.raises(ProviderError) as failure:
        flow.refresh(OAuthTokens(access_token="old", expires_at=0.0))

    assert "/login google" in (failure.value.hint or "")


def test_tokens_round_trip_through_the_keychain_as_opaque_text():
    backend = Backend()
    store = TokenStore(backend=backend)
    tokens = OAuthTokens(
        access_token="ya29.a b c",  # spaces in the *token*, not in what is stored
        refresh_token="1//r",
        expires_at=123.0,
        scopes=GOOGLE_SCOPES,
        account="dev@example.com",
    )

    store.write("google", tokens)
    restored = store.read("google")

    assert restored is not None
    assert restored.access_token == tokens.access_token
    assert restored.account == "dev@example.com"
    assert store.clear("google") is True
    assert store.read("google") is None


def test_an_unreadable_session_is_ignored_not_fatal():
    backend = Backend()
    backend.items["google-oauth"] = "not-base64-json"

    assert TokenStore(backend=backend).read("google") is None


def test_oauth_login_requires_a_client_registration():
    authenticator = OAuthAuthenticator(
        provider="google",
        tokens=TokenStore(backend=Backend()),
        build_client=google_client,
        docs_url="https://ai.google.dev/gemini-api/docs/oauth",
    )

    with pytest.raises(ProviderError) as failure:
        authenticator.login()

    assert "OAuth client" in failure.value.message
    assert "ai.google.dev" in (failure.value.hint or "")
    assert authenticator.capability.needs_setup is True


def test_oauth_login_stores_a_session_and_reports_the_account():
    backend = Backend()
    store = TokenStore(backend=backend)
    holder: dict[str, str] = {}
    authenticator = OAuthAuthenticator(
        provider="google",
        tokens=store,
        build_client=google_client,
        transport=TokenTransport(),
        opener=browser_that_completes(holder),
    )

    outcome = authenticator.login(
        client_id="cid.apps.googleusercontent.com",
        client_secret="secret",
        account="dev@example.com",
    )

    assert outcome.stored is True
    assert outcome.state.method is AuthMethod.OAUTH
    assert outcome.state.connected is True
    assert authenticator.credential() == "ya29.access"
    assert authenticator.uses_oauth() is True


def test_an_expired_session_is_refreshed_on_use():
    backend = Backend()
    store = TokenStore(backend=backend)
    store.write_client("google", client_id="cid", client_secret=None)
    store.write(
        "google",
        OAuthTokens(access_token="stale", refresh_token="1//r", expires_at=0.0),
    )
    transport = TokenTransport(
        body={"access_token": "ya29.fresh", "expires_in": 3600, "token_type": "Bearer"}
    )
    authenticator = OAuthAuthenticator(
        provider="google", tokens=store, build_client=google_client, transport=transport
    )

    assert authenticator.credential() == "ya29.fresh"
    # The refreshed token is persisted, so the next command does not refresh again.
    assert store.read("google").access_token == "ya29.fresh"


def test_an_explicit_key_on_an_oauth_provider_is_honoured_and_flagged():
    backend = Backend()
    api_key = ApiKeyAuthenticator(
        provider="google", store=key_store(backend), env_var="GEMINI_API_KEY"
    )
    authenticator = OAuthAuthenticator(
        provider="google",
        tokens=TokenStore(backend=backend),
        build_client=google_client,
        api_key_fallback=api_key,
    )

    outcome = authenticator.login(secret="AIzaSyTestKey0123456789")

    assert outcome.stored is True
    assert any("OAuth" in warning for warning in outcome.warnings)
    assert authenticator.uses_oauth() is False  # a key, and the UI will say so
    assert authenticator.credential() == "AIzaSyTestKey0123456789"


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------
def test_the_manager_builds_one_authenticator_per_provider():
    manager = AuthenticationManager(backend=Backend())

    first = manager.for_provider("anthropic")

    assert manager.for_provider("anthropic") is first
    assert manager.for_provider("google").method is AuthMethod.OAUTH
    assert manager.for_provider("ollama").method is AuthMethod.NONE


def test_states_covers_every_provider_without_a_request():
    manager = AuthenticationManager(backend=Backend())

    states = manager.states()

    assert set(states) == {"anthropic", "openai", "google", "vertex", "cursor", "ollama"}
    assert states["ollama"].connected is True
    assert states["anthropic"].connected is False


def test_no_credential_ever_reaches_the_project(tmp_path, monkeypatch):
    """§18: secrets live in the keychain, never in the project or a log."""
    backend = Backend()
    manager = AuthenticationManager(backend=backend)
    manager.for_provider("anthropic").login(secret=KEY)

    for path in tmp_path.rglob("*"):
        assert KEY not in path.read_text(errors="ignore") if path.is_file() else True
    assert KEY in backend.items["anthropic"]  # the keychain, and only there


# ---------------------------------------------------------------------------
# the device grant (RFC 8628): a machine with no browser
# ---------------------------------------------------------------------------
class DeviceTransport:
    """A device endpoint, then a token endpoint that first says "wait"."""

    def __init__(self, *, pending_rounds: int = 1) -> None:
        self.pending_rounds = pending_rounds
        self.urls: list[str] = []
        self.payloads: list[dict] = []

    def request(self, method, url, *, headers, payload=None, timeout=120.0):
        self.urls.append(url)
        self.payloads.append(payload or {})
        if "device/code" in url:
            return HttpResponse(
                status=200,
                body={
                    "device_code": "dev-code",
                    "user_code": "WXYZ-1234",
                    "verification_url": "https://www.google.com/device",
                    "interval": 5,
                    "expires_in": 1800,
                },
                text="",
            )
        if self.pending_rounds > 0:
            self.pending_rounds -= 1
            return HttpResponse(status=428, body={"error": "authorization_pending"}, text="")
        return HttpResponse(
            status=200,
            body={"access_token": "ya29.device", "refresh_token": "1//d", "expires_in": 3600},
            text="",
        )


def device_flow(transport: DeviceTransport) -> OAuthFlow:
    return OAuthFlow(client=google_client("cid", "secret"), transport=transport)


def test_device_login_shows_a_code_and_polls_until_approved():
    transport = DeviceTransport(pending_rounds=2)
    shown: list[str] = []
    slept: list[float] = []

    tokens = device_flow(transport).device_login(
        announce=lambda code: shown.append(code.user_code),
        sleep=slept.append,
    )

    # The developer is told the code once, not once per poll.
    assert shown == ["WXYZ-1234"]
    assert tokens.access_token == "ya29.device"
    # It waits before every poll, at the interval the provider set: the first
    # wait is what gives the developer time to open the URL and type the code.
    assert slept == [5.0, 5.0, 5.0]
    assert transport.payloads[-1]["grant_type"] == (
        "urn:ietf:params:oauth:grant-type:device_code"
    )


def test_device_login_refuses_a_provider_without_a_device_endpoint():
    client = google_client("cid", "secret")
    without = OAuthClient(
        provider=client.provider,
        authorize_url=client.authorize_url,
        token_url=client.token_url,
        client_id="cid",
        scopes=client.scopes,
    )

    with pytest.raises(ProviderError) as failure:
        OAuthFlow(client=without, transport=DeviceTransport()).device_login()

    assert "device sign-in" in str(failure.value)
    assert "browser" in (failure.value.hint or "")


def test_a_headless_machine_gets_the_device_grant_without_asking():
    """No browser is the case the device grant exists for - so don't hang."""
    transport = DeviceTransport(pending_rounds=0)
    store = TokenStore(backend=Backend())
    store.write_client("google", client_id="cid", client_secret="secret")
    authenticator = OAuthAuthenticator(
        provider="google", tokens=store, build_client=google_client, transport=transport
    )

    outcome = authenticator.login(device=True)

    assert outcome.stored is True
    assert store.read("google") is not None
    assert any("device/code" in url for url in transport.urls)


# ---------------------------------------------------------------------------
# client_secret.json: the file Google's own quickstart hands you
# ---------------------------------------------------------------------------
def test_a_downloaded_client_file_replaces_two_flags(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(
        '{"installed": {"client_id": "file-cid.apps.googleusercontent.com",'
        ' "client_secret": "file-secret"}}'
    )

    assert load_client_file(path) == ("file-cid.apps.googleusercontent.com", "file-secret")


def test_a_client_file_without_a_client_id_is_refused(tmp_path):
    path = tmp_path / "wrong.json"
    path.write_text('{"installed": {"project_id": "p"}}')

    with pytest.raises(ProviderError) as failure:
        load_client_file(path)

    assert "client_id" in str(failure.value) or "client_id" in (failure.value.hint or "")


def test_login_reads_the_client_file_instead_of_asking_for_ids(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text('{"web": {"client_id": "web-cid", "client_secret": "web-secret"}}')
    store = TokenStore(backend=Backend())
    authenticator = OAuthAuthenticator(
        provider="google",
        tokens=store,
        build_client=google_client,
        transport=DeviceTransport(pending_rounds=0),
    )

    authenticator.login(client_file=str(path), device=True)

    assert store.read_client("google") == ("web-cid", "web-secret")


# ---------------------------------------------------------------------------
# one Google account, two providers
# ---------------------------------------------------------------------------
def test_vertex_rides_the_google_session_rather_than_asking_twice():
    """Signing into Gemini connects Claude-on-Vertex: same account, same token."""
    backend = Backend()
    manager = AuthenticationManager(backend=backend)
    store = TokenStore(backend=backend)
    store.write(
        "google",
        OAuthTokens(access_token="ya29.shared", refresh_token="1//r"),
    )

    assert manager.for_provider("vertex").state().connected is True
    assert manager.credential("vertex") == "ya29.shared"
    # And it is the *same* slot, not a copy: signing out once signs out both.
    manager.for_provider("vertex").logout()
    assert manager.for_provider("google").state().connected is False


def test_vertex_declares_the_google_session_it_shares():
    entry = auth_for("vertex")

    assert entry.method is AuthMethod.OAUTH
    assert entry.shares_session_with == "google"
    # No Vertex-specific key exists, so none may be advertised.
    assert entry.allows_api_key is False
    assert entry.env_var is None
