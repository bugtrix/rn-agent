"""Credentials: the developer's own provider keys, in the OS keychain.

Nothing in this package writes a secret into a project directory, and no
credential is ever stored before the provider has accepted it.
"""

from __future__ import annotations

from .keychain import (
    BACKENDS,
    DpapiBackend,
    FileBackend,
    KeychainBackend,
    MacKeychainBackend,
    NullBackend,
    SecretServiceBackend,
    select_backend,
    validate_secret,
)
from .session import AuthStatus, LoginResult, build_store, login, logout, status
from .store import Credential, CredentialStore, StoredCredential

__all__ = [
    "BACKENDS",
    "AuthStatus",
    "Credential",
    "CredentialStore",
    "DpapiBackend",
    "FileBackend",
    "KeychainBackend",
    "LoginResult",
    "MacKeychainBackend",
    "NullBackend",
    "SecretServiceBackend",
    "StoredCredential",
    "build_store",
    "login",
    "logout",
    "select_backend",
    "status",
    "validate_secret",
]
