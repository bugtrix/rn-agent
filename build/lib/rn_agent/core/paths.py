"""Where the agent keeps its state.

Project state lives in ``<project>/.rn-agent`` so the brain travels with the
repository. User state (provider choice, credentials index) lives under
``~/.config/rn-agent`` so nothing secret can ever be committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..constants import (
    AGENT_DIR,
    ARCHITECTURE_FILE,
    BACKUP_DIR,
    CACHE_DIR,
    CONFIG_FILE,
    DECISIONS_FILE,
    DEPENDENCIES_FILE,
    ENV_HOME,
    IGNORE_FILE,
    KNOWLEDGE_DB,
    KNOWLEDGE_DIR,
    LOGS_DIR,
    MIGRATION_HISTORY_FILE,
    PROJECT_CONTEXT_FILE,
    PROJECT_MARKER_FILES,
    RULES_FILE,
    USER_CONFIG_DIR,
    USER_CONFIG_FILE,
    USER_CREDENTIALS_INDEX,
    USER_SECRETS_FILE,
)


@dataclass(frozen=True, slots=True)
class AgentPaths:
    """Resolved layout for one project."""

    project_root: Path
    agent_dir: Path

    @classmethod
    def for_project(cls, project_root: Path) -> AgentPaths:
        root = Path(project_root).expanduser()
        return cls(project_root=root, agent_dir=root / AGENT_DIR)

    # -- project files -----------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.agent_dir / CONFIG_FILE

    @property
    def context_file(self) -> Path:
        return self.agent_dir / PROJECT_CONTEXT_FILE

    @property
    def architecture_file(self) -> Path:
        return self.agent_dir / ARCHITECTURE_FILE

    @property
    def rules_file(self) -> Path:
        return self.agent_dir / RULES_FILE

    @property
    def dependencies_file(self) -> Path:
        return self.agent_dir / DEPENDENCIES_FILE

    @property
    def migration_history_file(self) -> Path:
        return self.agent_dir / MIGRATION_HISTORY_FILE

    @property
    def decisions_file(self) -> Path:
        return self.agent_dir / DECISIONS_FILE

    @property
    def knowledge_dir(self) -> Path:
        return self.agent_dir / KNOWLEDGE_DIR

    @property
    def knowledge_db(self) -> Path:
        return self.knowledge_dir / KNOWLEDGE_DB

    @property
    def cache_dir(self) -> Path:
        return self.agent_dir / CACHE_DIR

    @property
    def backup_dir(self) -> Path:
        return self.cache_dir / BACKUP_DIR

    @property
    def logs_dir(self) -> Path:
        return self.agent_dir / LOGS_DIR

    @property
    def ignore_file(self) -> Path:
        return self.project_root / IGNORE_FILE

    def log_file(self, command: str) -> Path:
        return self.logs_dir / f"{command}.log"

    def ensure(self) -> AgentPaths:
        for directory in (
            self.agent_dir,
            self.knowledge_dir,
            self.cache_dir,
            self.backup_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def exists(self) -> bool:
        return self.agent_dir.is_dir()

    def relative(self, path: Path) -> str:
        try:
            return str(Path(path).relative_to(self.project_root))
        except ValueError:
            return str(path)


def user_config_dir() -> Path:
    """``~/.config/rn-agent`` unless ``RN_AGENT_HOME`` overrides it."""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "rn-agent"
    return Path.home() / USER_CONFIG_DIR


def user_config_file() -> Path:
    return user_config_dir() / USER_CONFIG_FILE


def user_credentials_file() -> Path:
    """Index of which providers have a stored credential (never the secret)."""
    return user_config_dir() / USER_CREDENTIALS_INDEX


def user_secrets_file() -> Path:
    """Where the labelled file fallback keeps credentials when no keychain exists."""
    return user_config_dir() / USER_SECRETS_FILE


def looks_like_project(path: Path) -> bool:
    """A directory that carries at least a ``package.json``."""
    return (path / "package.json").is_file()


def marker_files(path: Path) -> list[str]:
    """Which recognised React Native project files exist in ``path``."""
    return [name for name in PROJECT_MARKER_FILES if (path / name).exists()]
