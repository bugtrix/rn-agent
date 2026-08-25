"""OAuth 2.0 with PKCE, over a loopback redirect.

This is the flow Google documents for installed applications, and the only shape
of OAuth this agent implements: the browser goes to the provider's own consent
screen, the provider redirects to ``http://127.0.0.1:<port>/callback``, and the
code is exchanged for tokens over TLS from this process.

Properties that matter, and why:

* **PKCE (S256), always.** An installed application cannot keep a client secret,
  so the code verifier is what binds the exchange to this process.
* **A random ``state``, checked on return.** A callback that does not carry back
  the state we generated is rejected, so another local page cannot feed us a
  code.
* **One request, then the socket closes.** The loopback server answers exactly
  one callback and shuts down; it is not a background listener.
* **Nothing is printed.** The code lands in the redirect, the tokens go straight
  to the keychain, and neither is ever logged - the log line says "exchanged an
  authorisation code", nothing more.

The token *store* lives here too, because refresh tokens are credentials: they
go in the same OS keychain as API keys, under a distinct account name, and they
never touch the project directory.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..errors import ProviderError
from ..net.http import DEFAULT_TIMEOUT, HttpResponse, JsonTransport, default_transport
from ..utils.io import read_text

#: The loopback host every installed-app OAuth flow redirects to.
LOOPBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
#: How long to wait for the developer to finish in the browser.
DEFAULT_LOGIN_TIMEOUT = 300.0
#: Refresh a little early: a token that expires mid-request is a failed command.
EXPIRY_SKEW_SECONDS = 120.0

SUCCESS_PAGE = b"""<!doctype html>
<meta charset="utf-8"><title>rn-agent</title>
<body style="font:15px -apple-system,Segoe UI,sans-serif;padding:3rem;max-width:32rem">
<h2>Signed in</h2>
<p>rn-agent has your authorisation. You can close this tab and return to the
terminal.</p>
</body>
"""

FAILURE_PAGE = b"""<!doctype html>
<meta charset="utf-8"><title>rn-agent</title>
<body style="font:15px -apple-system,Segoe UI,sans-serif;padding:3rem;max-width:32rem">
<h2>Sign-in failed</h2>
<p>Return to the terminal for the reason.</p>
</body>
"""


#: RFC 8628. Providers that publish a device endpoint accept this grant type.
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """What the provider hands back to start a device sign-in."""

    device_code: str
    #: The short code the developer types in the browser. Safe to display.
    user_code: str
    verification_url: str
    interval: float = 5.0
    expires_in: float = 900.0


@dataclass(frozen=True, slots=True)
class OAuthClient:
    """The provider endpoints and the client this app was registered as.

    ``client_id``/``client_secret`` are supplied by the developer, not shipped:
    an OAuth client belongs to whoever owns the project being billed, and
    embedding one in a public CLI would make every user share an identity.
    """

    provider: str
    authorize_url: str
    token_url: str
    client_id: str
    scopes: tuple[str, ...]
    #: "Installed app" clients get a secret that is not actually secret; some
    #: providers still require it in the exchange.
    client_secret: str | None = None
    #: RFC 8628 device endpoint, when the provider offers that grant. Used on a
    #: machine with no browser - a container, a remote shell, CI.
    device_url: str | None = None
    #: Extra authorisation parameters (Google wants ``access_type=offline``).
    extra_authorize_params: tuple[tuple[str, str], ...] = ()

    @property
    def supports_device_flow(self) -> bool:
        return bool(self.device_url)

    @property
    def scope_string(self) -> str:
        return " ".join(self.scopes)


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """What the provider gave back. Treated as a credential throughout."""

    access_token: str
    refresh_token: str | None = None
    #: Unix seconds. ``None`` when the provider did not say.
    expires_at: float | None = None
    scopes: tuple[str, ...] = ()
    account: str | None = None
    token_type: str = "Bearer"

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at - EXPIRY_SKEW_SECONDS

    @property
    def masked(self) -> str:
        tail = self.access_token[-4:]
        return f"…{tail}" if len(self.access_token) > 8 else "set"

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "account": self.account,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OAuthTokens | None:
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            return None
        expires = payload.get("expires_at")
        return cls(
            access_token=access,
            refresh_token=payload.get("refresh_token") or None,
            expires_at=float(expires) if isinstance(expires, int | float) else None,
            scopes=tuple(str(scope) for scope in payload.get("scopes") or ()),
            account=payload.get("account") or None,
            token_type=str(payload.get("token_type") or "Bearer"),
        )

    @classmethod
    def from_response(
        cls,
        body: dict[str, Any],
        *,
        previous: OAuthTokens | None = None,
        account: str | None = None,
    ) -> OAuthTokens:
        """Parse a token endpoint response.

        A refresh response usually omits ``refresh_token``; keeping the previous
        one is what makes a long-lived login actually long-lived.
        """
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise ProviderError(
                "the provider's token response contained no access token",
                hint="Try signing in again; if it persists, check the OAuth client settings.",
            )
        expires_in = body.get("expires_in")
        expires_at = (
            time.time() + float(expires_in)
            if isinstance(expires_in, int | float)
            else (previous.expires_at if previous else None)
        )
        scope_text = body.get("scope")
        scopes = (
            tuple(scope_text.split())
            if isinstance(scope_text, str) and scope_text
            else (previous.scopes if previous else ())
        )
        return cls(
            access_token=access,
            refresh_token=body.get("refresh_token") or (previous.refresh_token if previous else None),
            expires_at=expires_at,
            scopes=scopes,
            account=account or (previous.account if previous else None),
            token_type=str(body.get("token_type") or "Bearer"),
        )


# ---------------------------------------------------------------------------
# the loopback callback
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Callback:
    """What came back on the redirect."""

    code: str | None = None
    state: str | None = None
    error: str | None = None


def free_port() -> int:
    """An ephemeral port the provider can redirect to."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return int(probe.getsockname()[1])


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers exactly one redirect, then lets the server die."""

    result: _Callback
    finished: threading.Event

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        self.result.code = _first(params.get("code"))
        self.result.state = _first(params.get("state"))
        self.result.error = _first(params.get("error"))
        ok = bool(self.result.code) and not self.result.error
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(SUCCESS_PAGE if ok else FAILURE_PAGE)
        self.finished.set()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence http.server: a redirect URL carries an authorisation code."""
        return


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None


@dataclass(slots=True)
class LoopbackListener:
    """A one-shot HTTP server for an installed-app redirect."""

    port: int = field(default_factory=free_port)
    _server: http.server.HTTPServer | None = field(default=None, init=False, repr=False)
    _result: _Callback = field(default_factory=_Callback, init=False, repr=False)
    _finished: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    @property
    def redirect_uri(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}{CALLBACK_PATH}"

    def __enter__(self) -> LoopbackListener:
        handler = type(
            "BoundHandler",
            (_Handler,),
            {"result": self._result, "finished": self._finished},
        )
        self._server = http.server.HTTPServer((LOOPBACK_HOST, self.port), handler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def wait(self, *, timeout: float = DEFAULT_LOGIN_TIMEOUT) -> _Callback:
        """Block until the browser comes back, or say that it did not."""
        if not self._finished.wait(timeout):
            raise ProviderError(
                f"sign-in timed out after {timeout:g}s",
                hint="Run the command again; the browser tab must complete the consent screen.",
            )
        return self._result


# ---------------------------------------------------------------------------
# the flow
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class OAuthFlow:
    """Runs an authorisation-code + PKCE exchange for one provider."""

    client: OAuthClient
    transport: JsonTransport | None = None
    logger: logging.Logger = field(default_factory=lambda: get_logger("auth"))
    #: Injectable so a test can assert the URL without opening a browser.
    opener: Any = None
    timeout: float = DEFAULT_LOGIN_TIMEOUT

    def __post_init__(self) -> None:
        if self.transport is None:
            self.transport = default_transport()
        if self.opener is None:
            self.opener = webbrowser.open

    # -- public ------------------------------------------------------------
    def authorize_url(self, *, redirect_uri: str, verifier: str, state: str) -> str:
        """The consent URL, with PKCE and state applied."""
        params = {
            "client_id": self.client.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.client.scope_string,
            "state": state,
            "code_challenge": challenge_for(verifier),
            "code_challenge_method": "S256",
            **dict(self.client.extra_authorize_params),
        }
        separator = "&" if "?" in self.client.authorize_url else "?"
        return f"{self.client.authorize_url}{separator}{urllib.parse.urlencode(params)}"

    def run(self, *, account: str | None = None) -> tuple[OAuthTokens, str]:
        """Open the browser, wait for the redirect, exchange the code.

        Returns ``(tokens, url)`` - the URL so the caller can print it for a
        machine with no browser (an SSH session, a container).
        """
        verifier = new_verifier()
        state = secrets.token_urlsafe(24)
        with LoopbackListener() as listener:
            url = self.authorize_url(
                redirect_uri=listener.redirect_uri, verifier=verifier, state=state
            )
            self._open(url)
            callback = listener.wait(timeout=self.timeout)

        if callback.error:
            raise ProviderError(
                f"{self.client.provider} refused the sign-in: {callback.error}",
                hint="Check that the OAuth client allows this redirect URI and these scopes.",
            )
        if not callback.code:
            raise ProviderError(f"{self.client.provider} returned no authorisation code")
        if callback.state != state:
            # Someone else's redirect reached our port. Refuse it.
            raise ProviderError(
                "the sign-in response did not match this request",
                hint="Start the login again and complete it in the tab that opens.",
            )

        tokens = self.exchange(code=callback.code, verifier=verifier, redirect_uri=listener.redirect_uri, account=account)
        return tokens, url

    def exchange(
        self, *, code: str, verifier: str, redirect_uri: str, account: str | None = None
    ) -> OAuthTokens:
        """Trade the authorisation code for tokens."""
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": self.client.client_id,
            "redirect_uri": redirect_uri,
        }
        if self.client.client_secret:
            payload["client_secret"] = self.client.client_secret
        body = self._post(payload, what="exchanged an authorisation code")
        return OAuthTokens.from_response(body, account=account)

    # -- device grant ------------------------------------------------------
    def device_login(
        self,
        *,
        account: str | None = None,
        announce: Callable[[DeviceCode], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> OAuthTokens:
        """Sign in on a machine with no browser (RFC 8628).

        The developer opens the verification URL somewhere they *do* have a
        browser, types the short code, and this process polls until the
        provider says yes. Nothing is scraped and no password is handled - it is
        the same consent screen, reached from another device.
        """
        code = self.request_device_code()
        if announce is not None:
            announce(code)
        return self.poll_device_code(code, account=account, sleep=sleep)

    def request_device_code(self) -> DeviceCode:
        if not self.client.device_url:
            raise ProviderError(
                f"{self.client.provider} does not offer a device sign-in",
                hint="Run the login on a machine with a browser.",
            )
        body = self._post_to(
            self.client.device_url,
            {"client_id": self.client.client_id, "scope": self.client.scope_string},
            what="requested a device code",
        )
        device_code = body.get("device_code")
        user_code = body.get("user_code")
        verification = body.get("verification_url") or body.get("verification_uri")
        if not (isinstance(device_code, str) and isinstance(user_code, str) and verification):
            raise ProviderError(f"{self.client.provider} returned an unusable device code")
        return DeviceCode(
            device_code=device_code,
            user_code=user_code,
            verification_url=str(verification),
            interval=float(body.get("interval") or 5),
            expires_in=float(body.get("expires_in") or 900),
        )

    def poll_device_code(
        self,
        code: DeviceCode,
        *,
        account: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> OAuthTokens:
        """Wait for the developer to approve, honouring the provider's pacing."""
        payload = {
            "client_id": self.client.client_id,
            "device_code": code.device_code,
            "grant_type": DEVICE_GRANT,
        }
        if self.client.client_secret:
            payload["client_secret"] = self.client.client_secret

        deadline = time.time() + min(code.expires_in, self.timeout)
        interval = code.interval
        while time.time() < deadline:
            sleep(interval)
            response = self._raw_post(self.client.token_url, payload)
            if response.ok:
                self.logger.info("device sign-in approved for %s", self.client.provider)
                return OAuthTokens.from_response(response.body, account=account)
            error = str(response.body.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                # The provider is asking for more space between polls.
                interval += 5
                continue
            raise ProviderError(
                f"{self.client.provider} refused the device sign-in: {error or response.status}",
                hint=_token_error_hint(response.body),
            )
        raise ProviderError(
            "the device sign-in expired before it was approved",
            hint=f"Run the login again and enter the code at {code.verification_url}.",
        )

    def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        """Use the refresh token. Raises when there is none, or it is rejected."""
        if not tokens.refresh_token:
            raise ProviderError(
                f"the {self.client.provider} session expired and there is no refresh token",
                hint=f"Run `/login {self.client.provider}` to sign in again.",
            )
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": self.client.client_id,
        }
        if self.client.client_secret:
            payload["client_secret"] = self.client.client_secret
        body = self._post(payload, what="refreshed an access token")
        return OAuthTokens.from_response(body, previous=tokens)

    # -- internals ---------------------------------------------------------
    def _open(self, url: str) -> None:
        try:
            self.opener(url)
        except Exception as exc:  # pragma: no cover - platform dependent
            self.logger.debug("could not open a browser: %s", exc)

    def _raw_post(self, url: str, payload: dict[str, str]) -> HttpResponse:
        """One POST, returned as-is. The device grant reads error bodies."""
        assert self.transport is not None  # set in __post_init__
        return self.transport.request(
            "POST",
            url,
            headers={"accept": "application/json"},
            payload=payload,
            timeout=min(self.timeout, DEFAULT_TIMEOUT),
        )

    def _post_to(self, url: str, payload: dict[str, str], *, what: str) -> dict[str, Any]:
        response = self._raw_post(url, payload)
        if not response.ok:
            raise ProviderError(
                f"{self.client.provider} rejected the request (HTTP {response.status})",
                hint=_token_error_hint(response.body),
            )
        self.logger.info("%s for %s", what, self.client.provider)
        return response.body

    def _post(self, payload: dict[str, str], *, what: str) -> dict[str, Any]:
        return self._post_to(self.client.token_url, payload, what=what)


def new_verifier() -> str:
    """A PKCE code verifier: 43-128 unreserved characters."""
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    """The S256 challenge for ``verifier``, base64url without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _token_error_hint(body: dict[str, Any]) -> str | None:
    """The provider's own explanation, when it gave one."""
    for key in ("error_description", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# token storage
# ---------------------------------------------------------------------------
#: Suffix that keeps OAuth material in its own keychain slot, so an API key and
#: a session for the same provider cannot overwrite each other.
TOKEN_SUFFIX = "-oauth"
CLIENT_SUFFIX = "-oauth-client"


@dataclass(slots=True)
class TokenStore:
    """OAuth tokens and client registrations, in the OS keychain.

    Refresh tokens are long-lived credentials, so they get exactly the same
    treatment as API keys: the keychain, never the project, never a log line.
    """

    backend: Any  # KeychainBackend, typed loosely to avoid an import cycle
    logger: logging.Logger = field(default_factory=lambda: get_logger("auth"))

    # -- encoding ----------------------------------------------------------
    # A keychain backend stores one opaque token per account, and some of them
    # pass it through a subcommand script (macOS `security -i`). A JSON blob
    # carries spaces, quotes and braces, so it is base64url-encoded first: the
    # stored value then satisfies the same "opaque ASCII" contract as an API
    # key, and no backend has to learn about quoting.
    @staticmethod
    def _encode(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(stored: str) -> dict[str, Any] | None:
        padded = stored + "=" * (-len(stored) % 4)
        try:
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    # -- tokens ------------------------------------------------------------
    def read(self, provider: str) -> OAuthTokens | None:
        stored = self.backend.get(f"{provider}{TOKEN_SUFFIX}")
        if not stored:
            return None
        payload = self._decode(stored)
        if payload is None:
            self.logger.warning("stored %s session is unreadable; ignoring it", provider)
            return None
        return OAuthTokens.from_dict(payload)

    def write(self, provider: str, tokens: OAuthTokens) -> None:
        self.backend.set(f"{provider}{TOKEN_SUFFIX}", self._encode(tokens.as_dict()))
        self.logger.info("stored %s session in %s", provider, self.backend.name)

    def clear(self, provider: str) -> bool:
        return bool(self.backend.delete(f"{provider}{TOKEN_SUFFIX}"))

    # -- client registration ----------------------------------------------
    def read_client(self, provider: str) -> tuple[str, str | None] | None:
        """``(client_id, client_secret)`` the developer registered, if any."""
        stored = self.backend.get(f"{provider}{CLIENT_SUFFIX}")
        if not stored:
            return None
        payload = self._decode(stored)
        if payload is None:
            return None
        client_id = payload.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            return None
        secret = payload.get("client_secret")
        return client_id, secret if isinstance(secret, str) and secret else None

    def write_client(self, provider: str, *, client_id: str, client_secret: str | None) -> None:
        self.backend.set(
            f"{provider}{CLIENT_SUFFIX}",
            self._encode({"client_id": client_id, "client_secret": client_secret}),
        )

    def clear_client(self, provider: str) -> bool:
        return bool(self.backend.delete(f"{provider}{CLIENT_SUFFIX}"))


def load_client_file(path: str | Path) -> tuple[str, str | None]:
    """Read a client id/secret out of Google's ``client_secret.json``.

    This is the file Google's own OAuth quickstart tells you to download, so
    accepting it directly turns a three-flag command into ``--client-file
    ~/Downloads/client_secret_….json``. Both shapes Google emits are handled
    (``installed`` for a Desktop app, ``web`` for the other kind).
    """
    target = Path(path).expanduser()
    raw = read_text(target)
    if raw is None:
        raise ProviderError(
            f"cannot read {target}",
            hint="Download the OAuth client JSON from the Google Cloud console.",
        )
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProviderError(f"{target} does not contain an OAuth client")

    block = payload.get("installed") or payload.get("web") or payload
    client_id = block.get("client_id") if isinstance(block, dict) else None
    if not isinstance(client_id, str) or not client_id:
        raise ProviderError(
            f"{target} has no client_id",
            hint="Create an OAuth client of type \"Desktop app\" and download its JSON.",
        )
    secret = block.get("client_secret") if isinstance(block, dict) else None
    return client_id, secret if isinstance(secret, str) and secret else None
