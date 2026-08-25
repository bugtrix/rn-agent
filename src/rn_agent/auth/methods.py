"""The three sign-in mechanisms, and which provider gets which.

The table at the bottom of this module is the single source of truth for what
``/login`` offers. It is written from what each provider *publishes* for
third-party tools, with the sources recorded in ``docs/authentication.md``, and
it is the only place to change when a provider ships something new.

Deliberately absent: any mechanism that would need a browser cookie, a scraped
token, a password, a private endpoint, or an identity borrowed from another
vendor's CLI. Where a provider has an OAuth flow it does not offer to us, the
capability records *that fact* so the terminal can explain the API-key prompt
instead of pretending it is a subscription login.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from ..core.logging import get_logger
from ..errors import ProviderError
from .authenticator import AuthCapability, Authenticator, AuthMethod, AuthOutcome, AuthState
from .keychain import validate_secret
from .oauth import OAuthClient, OAuthFlow, TokenStore, load_client_file
from .store import Credential, CredentialStore

# ---------------------------------------------------------------------------
# Google: the one provider that publishes OAuth for this use
# ---------------------------------------------------------------------------
#: Documented at https://ai.google.dev/gemini-api/docs/oauth
GOOGLE_AUTH_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
#: RFC 8628 endpoint, so a machine with no browser can still sign in.
GOOGLE_DEVICE_URL: Final = "https://oauth2.googleapis.com/device/code"
GOOGLE_SCOPES: Final[tuple[str, ...]] = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/generative-language.retriever",
)


def google_client(client_id: str, client_secret: str | None) -> OAuthClient:
    """The Gemini API OAuth client, as Google's quickstart describes it."""
    return OAuthClient(
        provider="google",
        authorize_url=GOOGLE_AUTH_URL,
        token_url=GOOGLE_TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        scopes=GOOGLE_SCOPES,
        device_url=GOOGLE_DEVICE_URL,
        # offline + consent are what actually return a refresh token, so the
        # developer signs in once rather than once per day.
        extra_authorize_params=(
            ("access_type", "offline"),
            ("prompt", "consent"),
            ("include_granted_scopes", "true"),
        ),
    )


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------
@dataclass
class ApiKeyAuthenticator(Authenticator):
    """A key the developer creates in the provider's console.

    Used where a provider offers third-party tools nothing else. The UI labels it
    ``API Key`` and, when the provider *has* an OAuth flow reserved for its own
    products, prints why - see :attr:`AuthCapability.unsupported_note`.

    Storage goes through :class:`CredentialStore` rather than straight to a
    keychain backend, and that is not indirection for its own sake: the store
    validates the secret's shape, reads it back (a silent write is not a write),
    and maintains the index that ``whoami`` and ``logout --all`` enumerate. A
    key written past it would be usable but invisible.
    """

    store: CredentialStore = field(default=None)  # type: ignore[assignment]
    env_var: str | None = None
    console_url: str | None = None
    unsupported_note: str | None = None
    label: str = "API Key"
    detail: str = ""
    logger: logging.Logger = field(default_factory=lambda: get_logger("auth"))

    @property
    def capability(self) -> AuthCapability:
        return AuthCapability(
            provider=self.provider,
            method=AuthMethod.API_KEY,
            label=self.label,
            detail=self.detail,
            docs_url=self.console_url,
            unsupported_note=self.unsupported_note,
        )

    def state(self) -> AuthState:
        credential = self._resolve()
        if credential is None:
            return AuthState(provider=self.provider, method=AuthMethod.API_KEY, connected=False)
        return AuthState(
            provider=self.provider,
            method=AuthMethod.API_KEY,
            connected=True,
            source=credential.source,
            label=credential.describe(),
            masked=credential.masked,
        )

    def login(self, **options: Any) -> AuthOutcome:
        """Store a key. ``secret`` is required unless one is already reachable."""
        secret = options.get("secret")
        dry_run = bool(options.get("dry_run"))
        if not secret:
            existing = self.state()
            if existing.connected:
                return AuthOutcome(
                    state=existing,
                    warnings=("already connected; no new key was stored",),
                )
            raise ProviderError(
                f"no API key given for {self.provider}",
                hint=self._key_hint(),
            )
        value = validate_secret(secret)
        if dry_run:
            return AuthOutcome(
                state=AuthState(
                    provider=self.provider,
                    method=AuthMethod.API_KEY,
                    connected=True,
                    source=self.store.backend.name,
                    label=self.store.backend.label,
                    masked=_mask(value),
                ),
                stored=False,
                warnings=("dry run: the key was not stored",),
            )
        self.store.store(self.provider, value)
        return AuthOutcome(state=self.state(), stored=True)

    def logout(self) -> bool:
        return bool(self.store.forget(self.provider))

    def credential(self) -> str | None:
        found = self._resolve()
        return found.value if found else None

    def _resolve(self) -> Credential | None:
        """The credential and its provenance, environment first."""
        from ..ai.registry import resolve_spec

        return self.store.resolve(resolve_spec(self.provider))

    def _from_env(self) -> str | None:
        if not self.env_var:
            return None
        return os.environ.get(self.env_var, "").strip() or None

    def _key_hint(self) -> str:
        parts = [f"Create a key at {self.console_url}"] if self.console_url else []
        if self.env_var:
            parts.append(f"or export {self.env_var}")
        return ". ".join(parts) or "Paste a key when prompted."


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------
@dataclass
class OAuthAuthenticator(Authenticator):
    """A real account sign-in, through the provider's own consent screen.

    The tokens authorise whatever the provider says they authorise - for Gemini,
    the developer's own Cloud project. This class never claims a subscription is
    covered; it reports the account it signed in as, and nothing more.
    """

    tokens: TokenStore = field(default=None)  # type: ignore[assignment]
    #: Which stored session this provider uses. Claude-on-Vertex and Gemini are
    #: two providers on one Google account, so they share ``google``: signing in
    #: once connects both, and signing out of one does not orphan the other.
    shares_session_with: str | None = None
    build_client: Any = None
    label: str = "OAuth"
    detail: str = ""
    docs_url: str | None = None
    #: Falls back to a key when the developer prefers one; the UI shows which.
    api_key_fallback: ApiKeyAuthenticator | None = None
    transport: Any = None
    opener: Any = None
    logger: logging.Logger = field(default_factory=lambda: get_logger("auth"))

    @property
    def capability(self) -> AuthCapability:
        return AuthCapability(
            provider=self.provider,
            method=AuthMethod.OAUTH,
            label=self.label,
            detail=self.detail,
            docs_url=self.docs_url,
            needs_setup=self.client_registration() is None,
        )

    @property
    def session_key(self) -> str:
        """The keychain slot this provider's session lives in."""
        return self.shares_session_with or self.provider

    # -- client registration ----------------------------------------------
    def client_registration(self) -> tuple[str, str | None] | None:
        """The OAuth client this machine will use, if one was registered.

        An installed application cannot ship a client secret, and an OAuth client
        belongs to whoever owns the billed project - so the developer registers
        theirs once with ``/login google --client-id …``.
        """
        return self.tokens.read_client(self.session_key)

    def register_client(self, *, client_id: str, client_secret: str | None) -> None:
        self.tokens.write_client(
            self.session_key,
            client_id=client_id.strip(),
            client_secret=(client_secret or "").strip() or None,
        )

    # -- lifecycle ---------------------------------------------------------
    def state(self) -> AuthState:
        stored = self.tokens.read(self.session_key)
        if stored is None:
            fallback = self.api_key_fallback.state() if self.api_key_fallback else None
            if fallback and fallback.connected:
                return fallback
            return AuthState(provider=self.provider, method=AuthMethod.OAUTH, connected=False)
        return AuthState(
            provider=self.provider,
            method=AuthMethod.OAUTH,
            connected=True,
            source="oauth",
            label=f"OAuth session ({stored.account})" if stored.account else "OAuth session",
            masked=stored.masked,
            account=stored.account,
            expires_at=_iso(stored.expires_at),
            detail="expired; will refresh on next use" if stored.expired else None,
        )

    def login(self, **options: Any) -> AuthOutcome:
        """Run the provider's consent flow in a browser."""
        if options.get("secret") and self.api_key_fallback is not None:
            # The developer explicitly offered a key; honour it and say so.
            outcome = self.api_key_fallback.login(**options)
            return AuthOutcome(
                state=outcome.state,
                stored=outcome.stored,
                warnings=(
                    *outcome.warnings,
                    f"{self.provider} supports OAuth; run `login {self.provider}` "
                    "with no key to sign in with your account instead",
                ),
            )

        client_id = options.get("client_id")
        client_secret = options.get("client_secret")
        client_file = options.get("client_file")
        if client_file:
            # Google's own quickstart hands you this file; reading it directly
            # turns three flags into one path.
            client_id, client_secret = load_client_file(str(client_file))
        if client_id:
            self.register_client(client_id=str(client_id), client_secret=client_secret)
        registration = self.client_registration()
        if registration is None:
            raise ProviderError(
                f"{self.provider} OAuth needs an OAuth client from your own project",
                hint=(
                    f"Download the client JSON and run `login {self.provider} "
                    f"--client-file <path>`, or pass --client-id/--client-secret. "
                    f"Docs: {self.docs_url}"
                ),
            )
        if options.get("dry_run"):
            return AuthOutcome(
                state=self.state(),
                warnings=("dry run: no browser was opened and nothing was stored",),
            )

        flow = self._flow(registration)
        account = options.get("account")
        warnings: tuple[str, ...] = ()
        if options.get("device") or not browser_available():
            # No browser here: RFC 8628 moves the consent screen to a device
            # that has one, without this process ever seeing a password.
            tokens = flow.device_login(account=account, announce=options.get("announce"))
        else:
            tokens, url = flow.run(account=account)
            warnings = () if browser_available() else (f"open this URL to continue: {url}",)
        self.tokens.write(self.session_key, tokens)
        self.logger.info("%s OAuth sign-in complete", self.provider)
        return AuthOutcome(state=self.state(), stored=True, warnings=warnings)

    def logout(self) -> bool:
        cleared = self.tokens.clear(self.session_key)
        if self.api_key_fallback is not None:
            cleared = self.api_key_fallback.logout() or cleared
        return cleared

    def credential(self) -> str | None:
        """A bearer token, refreshed when it is about to expire."""
        stored = self.tokens.read(self.session_key)
        if stored is None:
            return self.api_key_fallback.credential() if self.api_key_fallback else None
        if not stored.expired:
            return stored.access_token
        registration = self.client_registration()
        if registration is None:  # pragma: no cover - a session implies a client
            return None
        refreshed = self._flow(registration).refresh(stored)
        self.tokens.write(self.session_key, refreshed)
        return refreshed.access_token

    def uses_oauth(self) -> bool:
        """Whether the *active* credential is an OAuth session, not a key."""
        return self.tokens.read(self.session_key) is not None

    # -- internals ---------------------------------------------------------
    def _flow(self, registration: tuple[str, str | None]) -> OAuthFlow:
        client_id, client_secret = registration
        builder = self.build_client or google_client
        return OAuthFlow(
            client=builder(client_id, client_secret),
            transport=self.transport,
            opener=self.opener,
            logger=self.logger,
        )


# ---------------------------------------------------------------------------
# local
# ---------------------------------------------------------------------------
@dataclass
class LocalAuthenticator(Authenticator):
    """A runtime on this machine. There is no credential to obtain."""

    host_env: str | None = None
    detail: str = "Runs on your machine; no account and no credential."
    logger: logging.Logger = field(default_factory=lambda: get_logger("auth"))

    @property
    def capability(self) -> AuthCapability:
        return AuthCapability(
            provider=self.provider,
            method=AuthMethod.NONE,
            label="None (local)",
            detail=self.detail,
        )

    def state(self) -> AuthState:
        host = os.environ.get(self.host_env, "").strip() if self.host_env else ""
        return AuthState(
            provider=self.provider,
            method=AuthMethod.NONE,
            connected=True,
            source="local",
            label=f"{self.host_env}={host}" if host else "local runtime",
        )

    def login(self, **options: Any) -> AuthOutcome:
        _ = options
        return AuthOutcome(
            state=self.state(),
            warnings=(f"{self.provider} runs locally and needs no sign-in",),
        )

    def logout(self) -> bool:
        return False

    def credential(self) -> str | None:
        return None


# ---------------------------------------------------------------------------
# a tool that holds its own session
# ---------------------------------------------------------------------------
@dataclass
class ToolAuthenticator(Authenticator):
    """A CLI on this machine that is already signed in on its own account.

    Cursor is the case this exists for. ``cursor-agent login`` stores a session
    in that tool's own config, and rn-agent uses the tool rather than reading
    its credential - no scraping, no copying, no second place for a secret to
    leak from. A key is still accepted (CI has no browser), and when one is
    given it takes precedence, because that is what the developer asked for.

    Interactive ``login`` *runs* that tool's sign-in command so the developer
    sees Cursor's own browser page, not a prompt to copy a command. ``--no-verify``,
    a dry run, a pipe, or an explicit key skip the spawn.

    ``state()`` is deliberately optimistic about the tool's own session: proving
    it would mean spawning the binary on every status render. ``--check`` is the
    authoritative answer, and it says which mechanism was live.
    """

    api_key: ApiKeyAuthenticator | None = None
    tool: str = "the tool"
    sign_in_command: str = ""
    detail: str = ""
    docs_url: str | None = None
    launcher: Callable[..., object] | None = None
    logger: logging.Logger = field(default_factory=lambda: get_logger("auth"))

    @property
    def capability(self) -> AuthCapability:
        return AuthCapability(
            provider=self.provider,
            method=AuthMethod.TOOL,
            label=f"{self.tool} session",
            detail=self.detail,
            docs_url=self.docs_url,
        )

    def state(self) -> AuthState:
        stored = self.api_key.state() if self.api_key else None
        if stored is not None and stored.connected:
            # An explicit key wins, and is reported as the key it is.
            return stored
        return AuthState(
            provider=self.provider,
            method=AuthMethod.TOOL,
            connected=True,
            source="tool",
            label=f"{self.tool} session",
        )

    def login(self, **options: Any) -> AuthOutcome:
        secret = options.get("secret")
        if secret and self.api_key is not None:
            return self.api_key.login(**options)
        if options.get("dry_run"):
            return AuthOutcome(
                state=self.state(),
                warnings=("dry run: would open Cursor's sign-in page in your browser",),
            )
        skip = options.get("skip_launch")
        if skip is None:
            # A pipe, a test, or a missing TTY must not hang on a browser.
            skip = not (sys.stdin.isatty() and sys.stdout.isatty())
        if skip:
            return AuthOutcome(
                state=self.state(),
                warnings=(
                    f"{self.tool} keeps its own login - run `{self.sign_in_command}` to sign in"
                    if self.sign_in_command
                    else f"{self.tool} keeps its own login",
                ),
            )
        launch = self.launcher
        if launch is None:
            from ..tools.cursor import run_sign_in

            launch = run_sign_in
        result = launch(install=bool(options.get("install_cli")))
        binary = str(result) if result else None
        self.logger.info("%s CLI sign-in complete", self.provider)
        return AuthOutcome(state=self.state(), stored=False, binary=binary)

    def logout(self) -> bool:
        """Forget the key rn-agent stored. The tool's own session is not ours."""
        return self.api_key.logout() if self.api_key else False

    def credential(self) -> str | None:
        return self.api_key.credential() if self.api_key else None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _mask(secret: str) -> str:
    return f"…{secret[-4:]}" if len(secret) > 8 else "set"


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def browser_available() -> bool:
    """Whether this machine plausibly has a browser to open.

    A container, a CI runner or an SSH session has none, and waiting on a
    loopback redirect there is a hang. The device grant is the answer, so this
    decides which flow to run rather than merely what to print.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if os.environ.get("RN_AGENT_NO_BROWSER"):
        return False
    return bool(os.environ.get("DISPLAY")) or os.name == "nt" or sys.platform == "darwin"
