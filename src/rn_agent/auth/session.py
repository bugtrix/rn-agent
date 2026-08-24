"""What ``login`` / ``logout`` / ``whoami`` actually do.

Policy lives here so the CLI stays a router: which credential wins, when a key
is verified against the real API, when it is stored, and what the developer is
told. Nothing in this module writes to a project - configuration files are the
caller's decision (user-level by default, project-level with ``--project``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..ai.http import JsonTransport
from ..ai.provider import AIProvider, ProviderIdentity
from ..ai.registry import ProviderSpec, build_provider, resolve_spec
from ..core.logging import get_logger
from ..errors import ProviderError
from ..models.config import AIConfig
from .keychain import select_backend
from .store import Credential, CredentialStore


@dataclass(frozen=True, slots=True)
class AuthStatus:
    """The answer to "is AI set up, and with whose key?"."""

    provider: str | None
    model: str | None
    task_models: dict[str, str]
    enabled: bool
    base_url: str | None
    requires_credential: bool
    #: Raw source: ``env`` or the backend name. Machine-readable on purpose.
    credential_source: str | None
    #: Same fact, phrased for a human ("OPENAI_API_KEY (environment)").
    credential_label: str | None
    credential_masked: str | None
    env_var: str | None
    backend: str
    backend_secure: bool
    stored: tuple[str, ...]
    #: Set only when the credential is *not* in an OS keychain, so every command
    #: can point at the file it fell back to.
    backend_location: str | None = None
    verified: bool | None = None
    detail: str | None = None

    @property
    def has_credential(self) -> bool:
        return self.credential_source is not None

    @property
    def from_env(self) -> bool:
        return self.credential_source == "env"

    @property
    def ready(self) -> bool:
        """True when a command could make a request right now."""
        if not self.provider or not self.enabled:
            return False
        return self.has_credential or not self.requires_credential

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "task_models": dict(self.task_models),
            "enabled": self.enabled,
            "base_url": self.base_url,
            "credential_source": self.credential_source,
            "credential_label": self.credential_label,
            "credential": self.credential_masked,
            "env_var": self.env_var,
            "backend": self.backend,
            "backend_secure": self.backend_secure,
            "backend_location": self.backend_location,
            "stored_providers": list(self.stored),
            "ready": self.ready,
            "verified": self.verified,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Outcome of one ``rn-agent login``."""

    status: AuthStatus
    identity: ProviderIdentity | None
    stored: bool
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.status.as_dict(),
            "stored": self.stored,
            "identity": self.identity.as_dict() if self.identity else None,
            "warnings": list(self.warnings),
        }


def build_store(
    *,
    override: str | None = None,
    secrets_file: Path | None = None,
    index_file: Path | None = None,
    logger: logging.Logger | None = None,
) -> CredentialStore:
    """A store on whichever backend this machine offers."""
    log = logger or get_logger("auth")
    backend = select_backend(override=override, secrets_file=secrets_file, logger=log)
    if index_file is None:
        return CredentialStore(backend=backend, logger=log)
    return CredentialStore(backend=backend, index_file=index_file, logger=log)


def _task_models(config: AIConfig) -> dict[str, str]:
    dumped = config.models.model_dump(exclude_none=True)
    return {task: model for task, model in dumped.items() if isinstance(model, str) and model}


def status(
    config: AIConfig,
    store: CredentialStore,
    *,
    provider_name: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    check: bool = False,
    transport: JsonTransport | None = None,
) -> AuthStatus:
    """Describe the current setup; only touches the network when ``check``."""
    name = provider_name or config.provider
    stored = tuple(entry.provider for entry in store.stored())
    location = None if store.backend.secure else str(store.backend.secrets_file)
    if not name:
        return AuthStatus(
            provider=None,
            model=model or config.model,
            task_models=_task_models(config),
            enabled=config.enabled,
            base_url=base_url or config.base_url,
            requires_credential=True,
            credential_source=None,
            credential_label=None,
            credential_masked=None,
            env_var=None,
            backend=store.backend.name,
            backend_secure=store.backend.secure,
            stored=stored,
            backend_location=location,
        )

    spec = resolve_spec(name)
    credential = store.resolve(spec)
    verified: bool | None = None
    detail: str | None = None
    if check:
        try:
            provider = _provider_for(
                spec, config, credential, model=model, base_url=base_url, transport=transport
            )
            identity = provider.verify()
            verified, detail = identity.ok, identity.detail
        except ProviderError as error:
            verified, detail = False, error.message

    return AuthStatus(
        provider=spec.name,
        model=model or config.model_for(None) or spec.default_model,
        task_models=_task_models(config),
        enabled=config.enabled,
        base_url=base_url or config.base_url or spec.provider_class.resolve_base_url(None),
        requires_credential=spec.requires_credential,
        credential_source=credential.source if credential else None,
        credential_label=credential.describe() if credential else None,
        credential_masked=credential.masked if credential else None,
        env_var=spec.env_var,
        backend=store.backend.name,
        backend_secure=store.backend.secure,
        stored=stored,
        backend_location=location,
        verified=verified,
        detail=detail,
    )


def login(
    *,
    provider: str,
    config: AIConfig,
    store: CredentialStore,
    secret: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    verify: bool = True,
    dry_run: bool = False,
    transport: JsonTransport | None = None,
) -> LoginResult:
    """Verify a credential against the real API, then store it.

    In that order: a key that the provider rejects never reaches the keychain.
    """
    spec = resolve_spec(provider)
    warnings: list[str] = []
    credential = _credential_for_login(spec, store, secret)
    chosen_model = model or config.model_for(None) or spec.default_model

    identity: ProviderIdentity | None = None
    if verify:
        instance = _provider_for(
            spec, config, credential, model=chosen_model, base_url=base_url, transport=transport
        )
        identity = instance.verify()
        if identity.models and chosen_model not in identity.models:
            warnings.append(
                f"{chosen_model} is not in this account's catalogue; "
                "run `rn-agent model --list` to see what is."
            )

    stored = False
    if secret and credential is not None and not dry_run:
        store.store(spec.name, credential.value)
        stored = True
    elif secret and dry_run:
        warnings.append("dry run: the credential was verified but not stored")
    elif credential is not None and credential.from_env and not secret:
        warnings.append(f"using {credential.env_var} from the environment; nothing was stored")

    resolved = status(
        config,
        store,
        provider_name=spec.name,
        model=chosen_model,
        base_url=base_url,
        transport=transport,
    )
    if identity is not None:
        resolved = replace(resolved, verified=identity.ok, detail=identity.detail)
    return LoginResult(status=resolved, identity=identity, stored=stored, warnings=tuple(warnings))


def logout(*, provider: str, store: CredentialStore) -> bool:
    """Forget a stored credential. Environment variables are not ours to unset."""
    return store.forget(provider)


def _credential_for_login(
    spec: ProviderSpec, store: CredentialStore, secret: str | None
) -> Credential | None:
    if secret:
        return Credential(spec.name, secret.strip(), store.backend.name)
    existing = store.resolve(spec)
    if existing is not None:
        return existing
    if spec.requires_credential:
        raise ProviderError(
            f"no API key given for {spec.name}",
            hint=(
                f"Pass --api-key, pipe the key with --stdin, or export {spec.env_var}."
                if spec.env_var
                else "Pass --api-key or pipe the key with --stdin."
            ),
        )
    return None


def _provider_for(
    spec: ProviderSpec,
    config: AIConfig,
    credential: Credential | None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    transport: JsonTransport | None = None,
) -> AIProvider:
    return build_provider(
        config,
        credential=credential.value if credential else None,
        provider_name=spec.name,
        model=model,
        base_url=base_url,
        transport=transport,
    )
