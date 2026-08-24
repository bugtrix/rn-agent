"""Credential resolution.

Two sources, in a fixed order, and the agent always reports which one it used:

1. **The provider's environment variable** (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``).
   Explicit, ephemeral, and what CI already has.
2. **The keychain**, written by ``rn-agent login``.

Same "provenance over guessing" rule as version resolution: ``rn-agent whoami``
prints the source, because "I exported a key" and "a key is in my keychain" are
different facts and conflating them is how people debug the wrong machine.

The index (``~/.config/rn-agent/credentials.json``) records *which* providers
have a stored key and in which backend. It never holds a secret, because
keychains cannot be enumerated portably and guessing is not an option.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..ai.registry import ProviderSpec, resolve_spec
from ..core.logging import get_logger
from ..core.paths import user_credentials_file
from ..errors import ProviderError
from ..utils.io import read_json, write_json
from .keychain import KeychainBackend, validate_secret

INDEX_VERSION = 1


@dataclass(frozen=True, slots=True)
class Credential:
    """A usable secret plus where it came from."""

    provider: str
    value: str
    source: str
    env_var: str | None = None

    @property
    def from_env(self) -> bool:
        return self.source == "env"

    @property
    def masked(self) -> str:
        return f"…{self.value[-4:]}" if len(self.value) > 8 else "set"

    def describe(self) -> str:
        return f"{self.env_var} (environment)" if self.from_env else self.source


@dataclass(frozen=True, slots=True)
class StoredCredential:
    """One index entry: a provider has a key somewhere, since some time."""

    provider: str
    backend: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "backend": self.backend, "updated_at": self.updated_at}


@dataclass(slots=True)
class CredentialStore:
    """Reads and writes provider credentials through one backend."""

    backend: KeychainBackend
    index_file: Path = field(default_factory=user_credentials_file)
    logger: logging.Logger = field(default_factory=lambda: get_logger("auth"))

    # -- reading -----------------------------------------------------------
    def resolve(self, spec: ProviderSpec) -> Credential | None:
        """The credential to use for ``spec``, or ``None`` when there is none."""
        if spec.env_var:
            from_env = os.environ.get(spec.env_var, "").strip()
            if from_env:
                return Credential(spec.name, from_env, "env", spec.env_var)
        stored = self.backend.get(spec.name)
        if stored:
            return Credential(spec.name, stored, self.backend.name)
        return None

    def require(self, spec: ProviderSpec) -> Credential | None:
        """Like :meth:`resolve`, but explains itself when a key is mandatory."""
        credential = self.resolve(spec)
        if credential is None and spec.requires_credential:
            raise ProviderError(
                f"no credential for {spec.name}",
                hint=spec.provider_class.credential_hint(),
            )
        return credential

    # -- writing -----------------------------------------------------------
    def store(self, provider: str, secret: str) -> Credential:
        """Validate, store, then read back - a silent write is not a write."""
        spec = resolve_spec(provider)
        value = validate_secret(secret)
        self.backend.set(spec.name, value)
        readback = self.backend.get(spec.name)
        if readback != value:
            raise ProviderError(
                f"{self.backend.label} accepted the credential but returned a different value",
                hint="Set RN_AGENT_KEYCHAIN=file to use the 0600 fallback instead.",
            )
        self._index_put(spec.name)
        self.logger.info("stored %s credential in %s", spec.name, self.backend.name)
        return Credential(spec.name, value, self.backend.name)

    def forget(self, provider: str) -> bool:
        """Remove a stored credential. ``False`` when nothing was stored."""
        spec = resolve_spec(provider)
        removed = self.backend.delete(spec.name)
        self._index_drop(spec.name)
        if removed:
            self.logger.info("removed %s credential from %s", spec.name, self.backend.name)
        return removed

    # -- index -------------------------------------------------------------
    def stored(self) -> tuple[StoredCredential, ...]:
        """What the index believes is stored, newest first."""
        entries = self._read_index()
        return tuple(
            sorted(
                (
                    StoredCredential(
                        provider=name,
                        backend=str(entry.get("backend", "")),
                        updated_at=str(entry.get("updated_at", "")),
                    )
                    for name, entry in entries.items()
                ),
                key=lambda item: item.updated_at,
                reverse=True,
            )
        )

    def has_stored(self, provider: str) -> bool:
        return provider in self._read_index()

    def _read_index(self) -> dict[str, dict[str, Any]]:
        payload = read_json(self.index_file, default={})
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, dict):
            return {}
        return {
            str(name): entry for name, entry in providers.items() if isinstance(entry, dict)
        }

    def _write_index(self, providers: dict[str, dict[str, Any]]) -> None:
        write_json(self.index_file, {"version": INDEX_VERSION, "providers": providers})

    def _index_put(self, provider: str) -> None:
        providers = self._read_index()
        providers[provider] = {
            "backend": self.backend.name,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self._write_index(providers)

    def _index_drop(self, provider: str) -> None:
        providers = self._read_index()
        if providers.pop(provider, None) is not None:
            self._write_index(providers)
