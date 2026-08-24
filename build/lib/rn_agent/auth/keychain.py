"""Credential storage backends.

A provider key is a secret the developer owns, so it goes where the operating
system keeps secrets - never into the project, and never into a config file the
agent writes:

===============  =====================================================
macOS            ``security`` (login keychain)
Linux/BSD        ``secret-tool`` (Secret Service / gnome-keyring, KWallet)
Windows          DPAPI via PowerShell, ciphertext under ``~/.config/rn-agent``
fallback         a ``0600`` file, clearly labelled as *not* a keychain
===============  =====================================================

Every backend shells out through :class:`CommandRunner` - the one place the
agent executes anything - and every secret travels on **stdin**, never in
``argv``, so it cannot show up in another user's process list. Set
``RN_AGENT_KEYCHAIN`` to ``file`` or ``none`` to override the choice (CI).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from ..constants import ENV_KEYCHAIN, KEYCHAIN_SERVICE
from ..core.logging import get_logger
from ..core.paths import user_secrets_file
from ..errors import ProviderError
from ..runner.command_runner import CommandResult, CommandRunner
from ..utils.io import read_json, write_json
from ..utils.redaction import redact

#: API keys are opaque ASCII tokens. Refusing anything else keeps quoting,
#: newlines and shell metacharacters out of every backend below.
_ALLOWED_SECRET = re.compile(r"^[A-Za-z0-9._\-:+/=~]{8,4096}$")

_MISSING_MARKERS = ("no such", "not found", "does not exist", "could not be found")

#: Reading a credential never changes anything, so it runs even in dry-run.
_READ_TIMEOUT = 20.0
_WRITE_TIMEOUT = 30.0


def validate_secret(value: str) -> str:
    """Normalise a credential, or explain why it cannot be one."""
    cleaned = value.strip()
    if not cleaned:
        raise ProviderError(
            "empty credential",
            hint="Paste the key from your provider dashboard, or pipe it in with --stdin.",
        )
    if not _ALLOWED_SECRET.match(cleaned):
        raise ProviderError(
            "that does not look like an API key (unsupported characters or length)",
            hint="Keys are 8+ characters of letters, digits and ._-:+/=~ with no whitespace.",
        )
    return cleaned


def _looks_missing(result: CommandResult) -> bool:
    """True when a backend said "no such item" rather than "I broke"."""
    lowered = result.stderr.casefold()
    return not result.stdout.strip() and (
        not lowered or any(marker in lowered for marker in _MISSING_MARKERS)
    )


class KeychainBackend(ABC):
    """Stores one secret per provider account under a fixed service name."""

    name: ClassVar[str] = "backend"
    label: ClassVar[str] = "credential store"
    #: False only for backends that are not an OS secret store.
    secure: ClassVar[bool] = True

    def __init__(
        self,
        *,
        runner: CommandRunner,
        secrets_file: Path | None = None,
        service: str = KEYCHAIN_SERVICE,
        logger: logging.Logger | None = None,
    ) -> None:
        self.runner = runner
        self.service = service
        self.secrets_file = secrets_file or user_secrets_file()
        self.logger = logger or get_logger("auth")

    @abstractmethod
    def available(self) -> bool:
        """Whether this backend can be used on this machine right now."""

    @abstractmethod
    def get(self, account: str) -> str | None:
        """The stored secret, or ``None`` when nothing is stored."""

    @abstractmethod
    def set(self, account: str, secret: str) -> None:
        """Store (or replace) the secret for ``account``."""

    @abstractmethod
    def delete(self, account: str) -> bool:
        """Remove the secret; ``False`` when there was nothing to remove."""

    # -- helpers -----------------------------------------------------------
    def _fail(self, action: str, result: CommandResult) -> ProviderError:
        detail = redact(result.tail(5)) or f"exit code {result.returncode}"
        return ProviderError(
            f"{self.label} could not {action} the credential: {detail}",
            hint="Unlock your keychain, or set RN_AGENT_KEYCHAIN=file to use the 0600 fallback.",
        )


class MacKeychainBackend(KeychainBackend):
    """macOS login keychain via ``security``."""

    name: ClassVar[str] = "keychain-macos"
    label: ClassVar[str] = "macOS keychain"
    #: `security` exit code for "the item is not in the keychain".
    NOT_FOUND: ClassVar[int] = 44

    def available(self) -> bool:
        return self.runner.available("security")

    def get(self, account: str) -> str | None:
        result = self.runner.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                self.service,
                "-w",
            ],
            timeout=_READ_TIMEOUT,
            force=True,
            quiet=True,
        )
        if result.ok:
            return result.stdout.strip() or None
        if result.returncode == self.NOT_FOUND:
            return None
        raise self._fail("read", result)

    def set(self, account: str, secret: str) -> None:
        # `security -i` reads subcommands from stdin, which keeps the secret out
        # of argv (and therefore out of `ps`). -U updates an existing item.
        script = f"add-generic-password -a {account} -s {self.service} -U -w {secret}\n"
        result = self.runner.run(
            ["security", "-i"], timeout=_WRITE_TIMEOUT, input_text=script
        )
        if not result.ok:
            raise self._fail("store", result)

    def delete(self, account: str) -> bool:
        result = self.runner.run(
            ["security", "delete-generic-password", "-a", account, "-s", self.service],
            timeout=_READ_TIMEOUT,
            quiet=True,
        )
        if result.ok:
            return True
        if result.returncode == self.NOT_FOUND:
            return False
        raise self._fail("delete", result)


class SecretServiceBackend(KeychainBackend):
    """Freedesktop Secret Service via ``secret-tool``."""

    name: ClassVar[str] = "secret-service"
    label: ClassVar[str] = "Secret Service keyring"

    def available(self) -> bool:
        return self.runner.available("secret-tool")

    def _attributes(self, account: str) -> list[str]:
        return ["service", self.service, "account", account]

    def get(self, account: str) -> str | None:
        result = self.runner.run(
            ["secret-tool", "lookup", *self._attributes(account)],
            timeout=_READ_TIMEOUT,
            force=True,
            quiet=True,
        )
        if result.ok:
            return result.stdout.strip() or None
        if _looks_missing(result):
            return None
        raise self._fail("read", result)

    def set(self, account: str, secret: str) -> None:
        result = self.runner.run(
            [
                "secret-tool",
                "store",
                f"--label={self.service} {account}",
                *self._attributes(account),
            ],
            timeout=_WRITE_TIMEOUT,
            input_text=secret,
        )
        if not result.ok:
            raise self._fail("store", result)

    def delete(self, account: str) -> bool:
        result = self.runner.run(
            ["secret-tool", "clear", *self._attributes(account)],
            timeout=_READ_TIMEOUT,
            quiet=True,
        )
        if result.ok:
            # `clear` succeeds even when nothing matched, so confirm by reading.
            return self.get(account) is None
        if _looks_missing(result):
            return False
        raise self._fail("delete", result)


class FileBackend(KeychainBackend):
    """Labelled fallback: a ``0600`` JSON file under ``~/.config/rn-agent``.

    Used when no OS secret store is reachable (containers, CI, minimal Linux).
    ``secure`` is ``False`` so every command can say so out loud.
    """

    name: ClassVar[str] = "file"
    label: ClassVar[str] = "0600 file (no OS keychain found)"
    secure: ClassVar[bool] = False

    def available(self) -> bool:
        return True

    # -- encoding hooks (DPAPI overrides these) ----------------------------
    def _encode(self, secret: str) -> str:
        return secret

    def _decode(self, stored: str) -> str | None:
        return stored or None

    # -- storage -----------------------------------------------------------
    def _load(self) -> dict[str, str]:
        payload = read_json(self.secrets_file, default={})
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items()}

    def _save(self, entries: dict[str, str]) -> None:
        write_json(self.secrets_file, entries)
        try:
            self.secrets_file.chmod(0o600)
        except OSError as exc:  # pragma: no cover - exotic filesystems
            self.logger.warning("could not restrict %s: %s", self.secrets_file, exc)

    def get(self, account: str) -> str | None:
        stored = self._load().get(account)
        return self._decode(stored) if stored else None

    def set(self, account: str, secret: str) -> None:
        entries = self._load()
        entries[account] = self._encode(secret)
        self._save(entries)

    def delete(self, account: str) -> bool:
        entries = self._load()
        if account not in entries:
            return False
        del entries[account]
        self._save(entries)
        return True


class DpapiBackend(FileBackend):
    """Windows: DPAPI-encrypted (user-scoped) ciphertext in the fallback file.

    ``cmdkey`` can only *write* Credential Manager entries, so it is useless for
    an agent that must read the key back. DPAPI ties the ciphertext to the
    logged-in Windows user, which is the same trust boundary as the keychain.
    """

    name: ClassVar[str] = "dpapi"
    label: ClassVar[str] = "Windows DPAPI"
    secure: ClassVar[bool] = True

    POWERSHELL: ClassVar[tuple[str, ...]] = (
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    )
    _ENCRYPT: ClassVar[str] = (
        "$plain=[Console]::In.ReadToEnd().Trim();"
        "ConvertTo-SecureString -String $plain -AsPlainText -Force |"
        " ConvertFrom-SecureString"
    )
    _DECRYPT: ClassVar[str] = (
        "$blob=[Console]::In.ReadToEnd().Trim();"
        "$secure=ConvertTo-SecureString -String $blob;"
        "[Runtime.InteropServices.Marshal]::PtrToStringAuto("
        "[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))"
    )

    def available(self) -> bool:
        return self.runner.available("powershell")

    def _powershell(self, script: str, payload: str, *, action: str) -> str:
        result = self.runner.run(
            [*self.POWERSHELL, script],
            timeout=_WRITE_TIMEOUT,
            input_text=payload,
            force=action == "read",
        )
        if not result.ok or not result.stdout.strip():
            raise self._fail(action, result)
        return result.stdout.strip()

    def _encode(self, secret: str) -> str:
        return self._powershell(self._ENCRYPT, secret, action="store")

    def _decode(self, stored: str) -> str | None:
        return self._powershell(self._DECRYPT, stored, action="read") or None


class NullBackend(KeychainBackend):
    """Storage disabled (``RN_AGENT_KEYCHAIN=none``): environment variables only."""

    name: ClassVar[str] = "none"
    label: ClassVar[str] = "disabled"

    def available(self) -> bool:
        return True

    def get(self, account: str) -> str | None:
        return None

    def set(self, account: str, secret: str) -> None:
        raise ProviderError(
            f"credential storage is disabled ({ENV_KEYCHAIN}=none)",
            hint="Export the provider's API-key environment variable instead.",
        )

    def delete(self, account: str) -> bool:
        return False


BACKENDS: dict[str, type[KeychainBackend]] = {
    MacKeychainBackend.name: MacKeychainBackend,
    SecretServiceBackend.name: SecretServiceBackend,
    DpapiBackend.name: DpapiBackend,
    FileBackend.name: FileBackend,
    NullBackend.name: NullBackend,
}

#: Aliases accepted from RN_AGENT_KEYCHAIN.
BACKEND_ALIASES: dict[str, str] = {
    "keychain": MacKeychainBackend.name,
    "macos": MacKeychainBackend.name,
    "secret-tool": SecretServiceBackend.name,
    "secretservice": SecretServiceBackend.name,
    "windows": DpapiBackend.name,
    "plain": FileBackend.name,
    "off": NullBackend.name,
}

_PREFERRED: dict[str, type[KeychainBackend]] = {
    "darwin": MacKeychainBackend,
    "win32": DpapiBackend,
    "cygwin": DpapiBackend,
}


def select_backend(
    *,
    runner: CommandRunner | None = None,
    override: str | None = None,
    platform: str | None = None,
    secrets_file: Path | None = None,
    logger: logging.Logger | None = None,
) -> KeychainBackend:
    """Pick a backend: explicit override, then the platform's, then the file."""
    log = logger or get_logger("auth")
    active_runner = runner or CommandRunner(cwd=Path.home(), logger=log)
    choice = (override or os.environ.get(ENV_KEYCHAIN) or "auto").strip().casefold()

    def build(backend_class: type[KeychainBackend]) -> KeychainBackend:
        return backend_class(runner=active_runner, secrets_file=secrets_file, logger=log)

    if choice != "auto":
        key = BACKEND_ALIASES.get(choice, choice)
        backend_class = BACKENDS.get(key)
        if backend_class is None:
            raise ProviderError(
                f"unknown credential backend: {choice}",
                hint=f"Set {ENV_KEYCHAIN} to one of: {', '.join(BACKENDS)}.",
            )
        return build(backend_class)

    backend = build(_PREFERRED.get(platform or sys.platform, SecretServiceBackend))
    if backend.available():
        return backend
    log.info("%s unavailable; falling back to the 0600 file store", backend.label)
    return build(FileBackend)
