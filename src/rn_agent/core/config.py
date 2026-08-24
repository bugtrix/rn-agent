"""Configuration loading and creation.

Precedence, lowest to highest:

1. built-in defaults
2. user config (``~/.config/rn-agent/config.yaml``) - provider/model preference
3. project config (``<project>/.rn-agent/config.yaml``)
4. CLI flags (applied by the caller)

The project file is safe to commit: it never contains credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError
from ..models.config import AgentConfig
from ..utils.io import atomic_write_text, read_text
from .paths import AgentPaths, user_config_file

CONFIG_HEADER = """\
# rn-agent project configuration.
# Safe to commit: credentials are stored in your OS keychain, never here.
# Docs: https://github.com/rn-agent/rn-agent#configuration
"""

USER_CONFIG_HEADER = """\
# rn-agent user preferences (which provider and model you like).
# Credentials are NOT here: they live in your OS keychain.
"""

DEFAULT_CONFIG_YAML = """\
# rn-agent project configuration.
# Safe to commit: credentials live in your OS keychain, never in this file.

version: 1

ai:
  # Set by `rn-agent login` / `rn-agent provider` / `rn-agent model`.
  provider: null
  model: null
  # Optional per-task overrides; `default` is used when a task has none.
  models:
    default: null
    migration: null
    debugging: null
    review: null
    feature: null
  enabled: true
  # Point at a self-hosted gateway or another machine's Ollama; null = provider default.
  base_url: null
  max_output_tokens: 4096
  temperature: 0.0
  timeout_seconds: 120.0
  max_context_files: 40
  max_context_tokens: 120000

safety:
  # Ask before writing anything that is not low risk.
  require_confirmation: true
  auto_fix_low_risk: false
  # Refuse to modify files while the git tree is dirty.
  require_clean_git: false
  create_backups: true
  allow_native_edits: true
  max_files_per_operation: 200

migration:
  create_git_branch: true
  branch_prefix: rn-agent/migrate
  run_install: true
  run_pod_install: true
  run_android_build: true
  run_ios_build: true
  run_tests: true
  use_ai_for_errors: true

context:
  # Files the agent may read into an AI prompt.
  respect_gitignore: true
  include_globs: []
  exclude_globs: []
  allow_secret_files: false
  max_file_kb: 96

logging:
  level: INFO
  keep_logs: true
"""


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    text = read_text(path)
    if text is None:
        return {}
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"could not parse {path}: {exc}",
            hint="Fix the YAML syntax, or delete the file to regenerate defaults.",
        ) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return loaded


def _deep_merge(
    base: dict[str, Any], overlay: dict[str, Any], *, null_clears: bool = False
) -> dict[str, Any]:
    """Overlay wins, recursively.

    When *layering* files, a ``null`` in the higher layer means "not set here",
    never "erase what the lower layer said" - otherwise every unset key in a
    generated project config would wipe the user's preferences. When *patching*
    one file, ``null`` is an explicit instruction (``--clear``), so
    ``null_clears`` flips that rule.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if value is None and key in merged and not null_clears:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value, null_clears=null_clears)
        else:
            merged[key] = value
    return merged


def load_config(paths: AgentPaths, *, user_file: Path | None = None) -> AgentConfig:
    """Merge user and project configuration into one model."""
    user_layer = _load_yaml_mapping(user_file or user_config_file())
    project_layer = _load_yaml_mapping(paths.config_file)
    merged = _deep_merge(user_layer, project_layer)
    try:
        return AgentConfig.model_validate(merged)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(
            f"invalid configuration in {paths.config_file}: {exc}",
            hint="Delete .rn-agent/config.yaml and re-run `rn-agent scan` to regenerate it.",
        ) from exc


def write_default_config(paths: AgentPaths, *, overwrite: bool = False) -> Path:
    """Create ``.rn-agent/config.yaml`` without ever clobbering user edits."""
    target = paths.config_file
    if target.exists() and not overwrite:
        return target
    paths.ensure()
    atomic_write_text(target, DEFAULT_CONFIG_YAML)
    return target


def update_project_config(paths: AgentPaths, patch: dict[str, Any]) -> Path:
    """Merge ``patch`` into ``.rn-agent/config.yaml``, keeping every other key.

    A patch, not a dump: writing a full model would freeze today's defaults into
    the file and mask future ones.
    """
    paths.ensure()
    merged = _deep_merge(_load_yaml_mapping(paths.config_file), patch, null_clears=True)
    body = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return atomic_write_text(paths.config_file, f"{CONFIG_HEADER}\n{body}")


def update_user_config(patch: dict[str, Any], *, user_file: Path | None = None) -> Path:
    """Merge ``patch`` into ``~/.config/rn-agent/config.yaml``.

    Holds preferences only (provider, model), so a project can override them
    without anyone re-authenticating.
    """
    target = user_file or user_config_file()
    merged = _deep_merge(_load_yaml_mapping(target), patch, null_clears=True)
    body = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return atomic_write_text(target, f"{USER_CONFIG_HEADER}\n{body}")
