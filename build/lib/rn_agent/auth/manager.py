"""AuthenticationManager: one place that knows how to sign in to anything.

The provider table below is the answer to "what does ``/login`` offer, and is it
honest?". Each entry is written from what the provider publishes for third-party
applications, and the ``unsupported_note`` fields are as important as the rest:
they are what lets the terminal say *why* Anthropic asks for a key instead of
implying that a Claude subscription is being used.

Sources for every claim are in ``docs/authentication.md``. When a provider ships
an official OAuth program for third-party tools, the change is one entry here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..errors import ProviderError
from .authenticator import AuthCapability, Authenticator, AuthMethod, AuthState
from .keychain import KeychainBackend, select_backend
from .methods import (
    ApiKeyAuthenticator,
    LocalAuthenticator,
    OAuthAuthenticator,
    ToolAuthenticator,
    google_client,
)
from .oauth import TokenStore
from .store import CredentialStore

#: Why Anthropic is API-key-only here. Quoted in the UI, not paraphrased away.
ANTHROPIC_NOTE = (
    "Anthropic restricts subscription OAuth (Free/Pro/Max) to Claude Code and "
    "Claude.ai. A Console API key is the only mechanism open to third-party "
    "tools, and it is billed separately from a Claude subscription."
)

#: Why OpenAI is API-key-only here.
OPENAI_NOTE = (
    "\"Sign in with ChatGPT\" is an identity provider - it returns a profile, "
    "not model access. Model calls need a platform API key, billed separately "
    "from a ChatGPT subscription."
)

GOOGLE_DETAIL = (
    "Sign in with your Google account. The tokens authorise the Gemini API in "
    "your own Cloud project, which is what Google's OAuth quickstart describes."
)

GOOGLE_DOCS = "https://ai.google.dev/gemini-api/docs/oauth"

VERTEX_DETAIL = (
    "Sign in with your Google account to use Claude models on Google Cloud. No "
    "Anthropic key: Anthropic publishes Claude in Vertex AI, and requests are "
    "billed to your own Cloud project."
)

VERTEX_DOCS = (
    "https://docs.cloud.google.com/vertex-ai/generative-ai/docs/partner-models/claude"
)

CURSOR_DETAIL = (
    "The Cursor CLI keeps its own login: run `cursor-agent login` once and this "
    "agent uses it, without copying the credential. CURSOR_API_KEY is accepted "
    "instead, for CI."
)

CURSOR_DOCS = "https://cursor.com/docs/cli/reference/authentication"


@dataclass(frozen=True, slots=True)
class ProviderAuth:
    """How one provider signs in, declaratively."""

    provider: str
    label: str
    method: AuthMethod
    env_var: str | None = None
    console_url: str | None = None
    docs_url: str | None = None
    detail: str = ""
    unsupported_note: str | None = None
    #: OAuth providers may also accept a key; the UI shows which is in use.
    allows_api_key: bool = False
    host_env: str | None = None
    #: Reuse another provider's stored session (Vertex rides Google's).
    shares_session_with: str | None = None


#: The registry. Order is the order the provider picker shows.
PROVIDER_AUTH: tuple[ProviderAuth, ...] = (
    ProviderAuth(
        provider="anthropic",
        label="Anthropic",
        method=AuthMethod.API_KEY,
        env_var="ANTHROPIC_API_KEY",
        console_url="https://console.anthropic.com/settings/keys",
        detail="Console API key (Anthropic offers third-party tools no OAuth).",
        unsupported_note=ANTHROPIC_NOTE,
    ),
    ProviderAuth(
        provider="openai",
        label="OpenAI",
        method=AuthMethod.API_KEY,
        env_var="OPENAI_API_KEY",
        console_url="https://platform.openai.com/api-keys",
        detail="Platform API key (\"Sign in with ChatGPT\" grants identity, not model access).",
        unsupported_note=OPENAI_NOTE,
    ),
    ProviderAuth(
        provider="google",
        label="Google Gemini",
        method=AuthMethod.OAUTH,
        env_var="GEMINI_API_KEY",
        console_url="https://aistudio.google.com/apikey",
        docs_url=GOOGLE_DOCS,
        detail=GOOGLE_DETAIL,
        allows_api_key=True,
    ),
    ProviderAuth(
        provider="vertex",
        label="Claude on Vertex AI",
        method=AuthMethod.OAUTH,
        docs_url=VERTEX_DOCS,
        detail=VERTEX_DETAIL,
        # One Google account, two providers: signing in once connects both.
        shares_session_with="google",
    ),
    ProviderAuth(
        provider="cursor",
        label="Cursor CLI",
        method=AuthMethod.TOOL,
        env_var="CURSOR_API_KEY",
        console_url="https://cursor.com/dashboard?tab=integrations",
        docs_url=CURSOR_DOCS,
        detail=CURSOR_DETAIL,
        allows_api_key=True,
    ),
    ProviderAuth(
        provider="ollama",
        label="Ollama",
        method=AuthMethod.NONE,
        host_env="OLLAMA_HOST",
        detail="Runs on your machine; no account and no credential.",
    ),
)

BY_NAME: dict[str, ProviderAuth] = {entry.provider: entry for entry in PROVIDER_AUTH}


def auth_for(provider: str) -> ProviderAuth:
    entry = BY_NAME.get(provider)
    if entry is None:
        raise ProviderError(
            f"unknown provider: {provider}",
            hint=f"Known providers: {', '.join(BY_NAME)}.",
        )
    return entry


@dataclass(slots=True)
class AuthenticationManager:
    """Builds and caches one authenticator per provider."""

    backend: KeychainBackend | None = None
    secrets_file: Path | None = None
    override: str | None = None
    transport: Any = None
    opener: Any = None
    logger: logging.Logger = field(default_factory=lambda: get_logger("auth"))
    _cache: dict[str, Authenticator] = field(default_factory=dict, init=False, repr=False)
    _tokens: TokenStore | None = field(default=None, init=False, repr=False)
    _credentials: CredentialStore | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = select_backend(
                override=self.override, secrets_file=self.secrets_file, logger=self.logger
            )
        self._tokens = TokenStore(backend=self.backend, logger=self.logger)

    # -- access ------------------------------------------------------------
    @property
    def tokens(self) -> TokenStore:
        assert self._tokens is not None  # set in __post_init__
        return self._tokens

    def providers(self) -> tuple[ProviderAuth, ...]:
        return PROVIDER_AUTH

    def capability(self, provider: str) -> AuthCapability:
        return self.for_provider(provider).capability

    def for_provider(self, provider: str) -> Authenticator:
        """The authenticator for ``provider``, built once per manager."""
        cached = self._cache.get(provider)
        if cached is not None:
            return cached
        built = self._build(auth_for(provider))
        self._cache[provider] = built
        return built

    def state(self, provider: str) -> AuthState:
        return self.for_provider(provider).state()

    def states(self) -> dict[str, AuthState]:
        """Connection state for every known provider - what ``/provider`` shows."""
        return {entry.provider: self.state(entry.provider) for entry in PROVIDER_AUTH}

    def stored_sessions(self) -> tuple[str, ...]:
        """Providers holding an OAuth session, as opposed to a stored key.

        ``whoami`` needs this because the key index cannot see the token store:
        without it, signing in with a browser and then asking what is stored
        would answer "nothing". Providers that share one session (Vertex rides
        Google's) are both listed - both really are connected.
        """
        names: list[str] = []
        for entry in PROVIDER_AUTH:
            uses_oauth = getattr(self.for_provider(entry.provider), "uses_oauth", None)
            if callable(uses_oauth) and uses_oauth():
                names.append(entry.provider)
        return tuple(names)

    def credential(self, provider: str) -> str | None:
        return self.for_provider(provider).credential()

    def connected(self, provider: str) -> bool:
        return self.state(provider).connected

    @property
    def credentials(self) -> CredentialStore:
        """The key store: validation, read-back, and the provider index."""
        assert self.backend is not None  # set in __post_init__
        if self._credentials is None:
            self._credentials = CredentialStore(backend=self.backend, logger=self.logger)
        return self._credentials

    # -- building ----------------------------------------------------------
    def _build(self, entry: ProviderAuth) -> Authenticator:
        if entry.method is AuthMethod.NONE:
            return LocalAuthenticator(
                provider=entry.provider,
                host_env=entry.host_env,
                detail=entry.detail,
                logger=self.logger,
            )

        api_key = ApiKeyAuthenticator(
            provider=entry.provider,
            store=self.credentials,
            env_var=entry.env_var,
            console_url=entry.console_url,
            unsupported_note=entry.unsupported_note,
            detail=entry.detail,
            logger=self.logger,
        )
        if entry.method is AuthMethod.API_KEY:
            return api_key

        if entry.method is AuthMethod.TOOL:
            # The tool owns the session; rn-agent only ever holds an optional key.
            return ToolAuthenticator(
                provider=entry.provider,
                api_key=api_key,
                tool=entry.label,
                sign_in_command=f"{entry.provider}-agent login",
                detail=entry.detail,
                docs_url=entry.docs_url,
                logger=self.logger,
            )

        # Every OAuth provider here is a Google flow today; ``shares_session_with``
        # is what lets Vertex ride the Gemini sign-in rather than asking twice.
        return OAuthAuthenticator(
            provider=entry.provider,
            tokens=self.tokens,
            shares_session_with=entry.shares_session_with,
            build_client=google_client,
            label="OAuth (Google account)",
            detail=entry.detail,
            docs_url=entry.docs_url,
            api_key_fallback=api_key if entry.allows_api_key else None,
            transport=self.transport,
            opener=self.opener,
            logger=self.logger,
        )
