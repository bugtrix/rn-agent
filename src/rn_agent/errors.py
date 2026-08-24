"""Exception hierarchy.

Commands convert these into a rendered error plus a non-zero exit code; they
never leak a traceback to the developer's terminal.
"""

from __future__ import annotations


class RNAgentError(Exception):
    """Base class for every expected failure."""

    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class NotAReactNativeProject(RNAgentError):
    """The working directory is not (inside) a React Native project."""

    exit_code = 2


class ProjectNotScanned(RNAgentError):
    """A command needs the shared project context but no scan has been run."""

    exit_code = 3


class ConfigError(RNAgentError):
    """`.rn-agent/config.yaml` is missing, malformed or invalid."""

    exit_code = 4


class UnsafePathError(RNAgentError):
    """A write was attempted outside the project root."""

    exit_code = 5


class GitError(RNAgentError):
    """A git precondition failed or a git command errored."""

    exit_code = 6


class CommandExecutionError(RNAgentError):
    """An external tool exited non-zero (or was not found)."""

    exit_code = 7


class ConfirmationDeclined(RNAgentError):
    """The developer answered "no" at a safety gate."""

    exit_code = 8


class KnowledgeStoreError(RNAgentError):
    """Local SQLite knowledge storage failed."""

    exit_code = 9


class ProviderError(RNAgentError):
    """AI provider configuration/authentication problem (phase 2)."""

    exit_code = 10


class TransportError(RNAgentError):
    """A request never reached its host (DNS, TLS, refused, timeout).

    Raised by ``net/http.py`` for every consumer - the npm registry, the
    upstream React Native diffs, and AI providers. Providers translate it into
    a :class:`ProviderError` so an AI failure keeps reporting exit code 10.
    """

    exit_code = 11


class ModelOutputError(RNAgentError):
    """The model answered, but the answer could not be used.

    A separate code from :class:`ProviderError` on purpose: the credential and
    the endpoint are fine, the *response* is not, and the fix is different
    (retry, a stronger model, a narrower request).
    """

    exit_code = 12
