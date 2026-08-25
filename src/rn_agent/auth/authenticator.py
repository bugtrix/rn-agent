"""How each provider lets you sign in - and what it does not let you do.

This is the module that keeps ``/login`` honest. Every provider declares the
authentication mechanism it *officially* supports for a third-party tool like
this one, and the UI shows that mechanism by name. There is no code path that
dresses an API key up as a subscription login, and none that reaches for a
mechanism a provider has said is off limits.

The state of the world this abstraction encodes (verified, with sources in
``docs/authentication.md``):

* **Google** publishes an OAuth 2.0 flow for the Gemini API, so ``/login google``
  really is "sign in with your Google account" - the tokens authorise *your*
  Cloud project.
* **Anthropic** restricts subscription OAuth to Claude Code and Claude.ai; the
  only mechanism open to a third-party tool is a Console API key. The UI says
  ``Auth: API Key`` and explains why, rather than implying a Pro/Max login.
* **OpenAI**'s "Sign in with ChatGPT" is an identity provider - it returns a
  profile, not model access - so model calls need an API key.
* **Ollama** runs on your machine and needs no credential at all.

When a provider ships an official OAuth program, adding it is one subclass and a
capability change here. Nothing in the terminal UI has to move.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.logging import get_logger
from ..errors import ProviderError


class AuthMethod(StrEnum):
    """How a credential is obtained."""

    #: The provider's own OAuth 2.0 flow, in a browser, on the user's account.
    OAUTH = "oauth"
    #: A key the user creates in the provider's console and pastes once.
    API_KEY = "api_key"
    #: No credential exists to obtain (a local runtime).
    NONE = "none"
    #: Another tool on this machine holds the session, and rn-agent uses it
    #: without copying it. Cursor is the case: `cursor-agent login` stores its
    #: own credential, which this agent reads through the tool, never around it.
    #: A key is still accepted, because CI has no browser to log in with.
    TOOL = "tool"

    @property
    def label(self) -> str:
        return {
            AuthMethod.OAUTH: "OAuth",
            AuthMethod.API_KEY: "API Key",
            AuthMethod.NONE: "None (local)",
            AuthMethod.TOOL: "Tool session",
        }[self]


@dataclass(frozen=True, slots=True)
class AuthCapability:
    """What a provider offers, and what it withholds.

    ``unsupported_note`` is the important field: when a provider *has* an OAuth
    flow that third-party tools may not use, the terminal says so out loud. A
    developer asking "why am I pasting a key when I pay for Pro?" deserves the
    real answer.
    """

    provider: str
    method: AuthMethod
    #: Shown in the provider picker, e.g. "OAuth (Google account)".
    label: str
    #: One line of why this is the mechanism, shown under the selection.
    detail: str = ""
    docs_url: str | None = None
    #: Set when an OAuth flow exists but is not available to this application.
    unsupported_note: str | None = None
    #: True when the flow needs values the user must supply (an OAuth client).
    needs_setup: bool = False

    @property
    def is_account_login(self) -> bool:
        return self.method is AuthMethod.OAUTH

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "method": self.method.value,
            "label": self.label,
            "detail": self.detail,
            "docs_url": self.docs_url,
            "unsupported_note": self.unsupported_note,
            "needs_setup": self.needs_setup,
        }


@dataclass(frozen=True, slots=True)
class AuthState:
    """Whether this provider is usable right now, and on whose authority."""

    provider: str
    method: AuthMethod
    connected: bool
    #: ``env``, a keychain backend name, ``oauth``, or ``local``.
    source: str | None = None
    #: Human phrasing of the same fact ("ANTHROPIC_API_KEY (environment)").
    label: str | None = None
    #: Enough of the secret to recognise it, never enough to use it.
    masked: str | None = None
    #: The signed-in account, when the mechanism reveals one.
    account: str | None = None
    expires_at: str | None = None
    detail: str | None = None

    @property
    def status_word(self) -> str:
        if self.method is AuthMethod.NONE:
            return "available locally"
        return "connected" if self.connected else "not connected"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "method": self.method.value,
            "connected": self.connected,
            "source": self.source,
            "label": self.label,
            "credential": self.masked,
            "account": self.account,
            "expires_at": self.expires_at,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    """The result of one login attempt."""

    state: AuthState
    stored: bool = False
    warnings: tuple[str, ...] = ()
    #: Models the provider reported during verification, when it did.
    models: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.state.as_dict(),
            "stored": self.stored,
            "warnings": list(self.warnings),
            "models": list(self.models),
        }


@dataclass
class Authenticator(ABC):
    """One provider's sign-in mechanism."""

    provider: str
    logger: logging.Logger = field(default_factory=lambda: get_logger("auth"))

    # -- declaration -------------------------------------------------------
    @property
    @abstractmethod
    def capability(self) -> AuthCapability:
        """What this provider officially supports. Shown verbatim in the UI."""

    @property
    def method(self) -> AuthMethod:
        return self.capability.method

    # -- lifecycle ---------------------------------------------------------
    @abstractmethod
    def state(self) -> AuthState:
        """Whether a request could be made right now. Never performs one."""

    @abstractmethod
    def login(self, **options: Any) -> AuthOutcome:
        """Obtain a credential. May open a browser or read one value."""

    @abstractmethod
    def logout(self) -> bool:
        """Forget whatever this machine stored. ``False`` if nothing was."""

    @abstractmethod
    def credential(self) -> str | None:
        """The value to authenticate a request with, or ``None``."""

    # -- shared helpers ----------------------------------------------------
    def require_credential(self) -> str:
        """The credential, or an error that says how to get one."""
        value = self.credential()
        if not value:
            capability = self.capability
            raise ProviderError(
                f"{self.provider} is not connected",
                hint=self.connect_hint(capability),
            )
        return value

    def connect_hint(self, capability: AuthCapability | None = None) -> str:
        capability = capability or self.capability
        if capability.method is AuthMethod.OAUTH:
            base = f"Run `/login {self.provider}` to sign in with your account."
        elif capability.method is AuthMethod.API_KEY:
            base = f"Run `/login {self.provider}` and paste a key."
        else:
            base = f"{self.provider} needs no credential."
        return f"{base} {capability.docs_url}" if capability.docs_url else base
