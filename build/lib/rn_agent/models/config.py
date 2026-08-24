"""Configuration model for ``.rn-agent/config.yaml``.

Credentials are deliberately absent: provider secrets live in the OS keychain
(or a labelled 0600 fallback file under ``~/.config/rn-agent``), never in the
project. The project file only records *which* provider and model to use, plus
request, safety and context policy.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskModels(BaseModel):
    """Optional per-task model overrides (§9)."""

    model_config = ConfigDict(extra="allow")

    default: str | None = None
    migration: str | None = None
    debugging: str | None = None
    review: str | None = None
    feature: str | None = None
    test: str | None = None
    upgrade: str | None = None
    docs: str | None = None

    def for_task(self, task: str | None) -> str | None:
        if task:
            value = getattr(self, task, None)
            if isinstance(value, str) and value:
                return value
            extra = (self.model_extra or {}).get(task)
            if isinstance(extra, str) and extra:
                return extra
        return self.default


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str | None = None
    model: str | None = None
    models: TaskModels = Field(default_factory=TaskModels)
    enabled: bool = True
    #: Override the provider's API host (self-hosted gateway, Ollama on another box).
    base_url: str | None = None
    max_output_tokens: int = 4096
    temperature: float = 0.0
    timeout_seconds: float = 120.0
    max_context_files: int = 40
    max_context_tokens: int = 120_000

    def model_for(self, task: str | None = None) -> str | None:
        return self.models.for_task(task) or self.model


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    require_confirmation: bool = True
    auto_fix_low_risk: bool = False
    require_clean_git: bool = False
    create_backups: bool = True
    allow_native_edits: bool = True
    max_files_per_operation: int = 200


class MigrationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    create_git_branch: bool = True
    branch_prefix: str = "rn-agent/migrate"
    run_install: bool = True
    run_pod_install: bool = True
    run_android_build: bool = True
    run_ios_build: bool = True
    run_tests: bool = True
    use_ai_for_errors: bool = True
    upgrade_helper_base: str = "https://react-native-community.github.io/upgrade-helper"
    docs_base: str = "https://reactnative.dev/docs/upgrading"


class ContextConfig(BaseModel):
    """What the agent is allowed to read into an AI prompt (§31)."""

    model_config = ConfigDict(extra="ignore")

    respect_gitignore: bool = True
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)
    allow_secret_files: bool = False
    max_file_kb: int = 96


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    level: str = "INFO"
    keep_logs: bool = True

    @field_validator("level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class AgentConfig(BaseModel):
    """Root of ``.rn-agent/config.yaml``."""

    model_config = ConfigDict(extra="ignore")

    version: int = 1
    ai: AIConfig = Field(default_factory=AIConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    migration: MigrationConfig = Field(default_factory=MigrationConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    def to_yaml_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=False)
