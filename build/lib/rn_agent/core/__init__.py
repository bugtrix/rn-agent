"""Agent core.

Deliberately light: this package's ``__init__`` only re-exports the leaf
modules (paths, config, logging). ``AgentContext``, ``AgentCommand`` and the
registry live one import away because they depend on the git/runner/filesystem
managers, which in turn need ``core.logging`` - importing them here would create
a cycle.
"""

from __future__ import annotations

from .config import (
    load_config,
    update_project_config,
    update_user_config,
    write_default_config,
)
from .logging import configure_logging, get_logger, shutdown_logging
from .paths import (
    AgentPaths,
    user_config_dir,
    user_config_file,
    user_credentials_file,
    user_secrets_file,
)

__all__ = [
    "AgentPaths",
    "configure_logging",
    "get_logger",
    "load_config",
    "shutdown_logging",
    "update_project_config",
    "update_user_config",
    "user_config_dir",
    "user_config_file",
    "user_credentials_file",
    "user_secrets_file",
    "write_default_config",
]
