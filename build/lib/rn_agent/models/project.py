"""The shared project brain.

Everything the agent learns about a project during ``rn-agent scan`` is stored
here and serialised to ``.rn-agent/project-context.json``. Every other command
reads this model instead of re-deriving facts, which is what makes the tool one
agent rather than a bag of scripts.

Design rule: a field is ``None`` when the fact could not be established.
Nothing in this model is ever guessed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONTEXT_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Base(BaseModel):
    """Tolerant base: unknown keys in an older context file are ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class DependencyKind(StrEnum):
    PROD = "prod"
    DEV = "dev"
    PEER = "peer"
    OPTIONAL = "optional"


class DependencyInfo(Base):
    name: str
    declared: str | None = None
    installed: str | None = None
    kind: DependencyKind = DependencyKind.PROD
    native: bool = False
    platforms: list[str] = Field(default_factory=list)
    peer_dependencies: dict[str, str] = Field(default_factory=dict)

    @property
    def effective_version(self) -> str | None:
        return self.installed or self.declared


class PackageManagerInfo(Base):
    name: str = "unknown"
    lockfile: str | None = None
    lockfiles_found: list[str] = Field(default_factory=list)
    declared: str | None = None
    version: str | None = None
    workspaces: list[str] = Field(default_factory=list)

    @property
    def install_command(self) -> str:
        return {
            "npm": "npm install",
            "yarn": "yarn install",
            "pnpm": "pnpm install",
            "bun": "bun install",
        }.get(self.name, "npm install")


class ReactNativeInfo(Base):
    version: str | None = None
    declared_range: str | None = None
    installed_version: str | None = None
    #: where `version` came from: node_modules | <lockfile> | package.json
    version_source: str | None = None
    react_version: str | None = None
    react_declared_range: str | None = None
    react_requirement: str | None = None
    types_react_version: str | None = None
    types_react_requirement: str | None = None
    node_requirement: str | None = None
    hermes_enabled: bool | None = None
    new_architecture: bool | None = None
    typescript: bool = False
    typescript_version: str | None = None
    expo: bool = False
    expo_version: str | None = None
    expo_managed: bool = False
    metro_config: str | None = None
    babel_config: str | None = None
    rn_config: str | None = None
    app_json: str | None = None
    tsconfig: str | None = None
    template_source: str | None = None


class AndroidInfo(Base):
    present: bool = False
    gradle_version: str | None = None
    agp_version: str | None = None
    kotlin_version: str | None = None
    ndk_version: str | None = None
    build_tools_version: str | None = None
    compile_sdk: int | None = None
    target_sdk: int | None = None
    min_sdk: int | None = None
    java_source_compatibility: str | None = None
    java_target_compatibility: str | None = None
    namespace: str | None = None
    application_id: str | None = None
    new_architecture: bool | None = None
    hermes_enabled: bool | None = None
    permissions: list[str] = Field(default_factory=list)
    manifest_path: str | None = None
    main_application: str | None = None
    main_activity: str | None = None
    kotlin_sources: int = 0
    java_sources: int = 0
    gradle_properties: dict[str, str] = Field(default_factory=dict)
    flavors: list[str] = Field(default_factory=list)
    signing_configs: list[str] = Field(default_factory=list)


class IOSInfo(Base):
    present: bool = False
    deployment_target: str | None = None
    deployment_target_source: str | None = None
    podfile_platform: str | None = None
    podfile_present: bool = False
    podfile_lock_present: bool = False
    pods_installed: bool = False
    cocoapods_version: str | None = None
    pods_react_native_version: str | None = None
    use_frameworks: str | None = None
    workspace: str | None = None
    xcodeproj: str | None = None
    project_name: str | None = None
    bundle_identifier: str | None = None
    display_name: str | None = None
    privacy_manifest: bool = False
    #: Where the usage descriptions were read from, so a check can name the file
    #: a developer has to edit rather than guessing at the conventional path.
    info_plist: str | None = None
    usage_descriptions: list[str] = Field(default_factory=list)
    entitlements: list[str] = Field(default_factory=list)
    app_delegate: str | None = None
    app_delegate_language: str | None = None
    swift_sources: int = 0
    objc_sources: int = 0
    new_architecture: bool | None = None


class GitInfo(Base):
    repository: bool = False
    root: str | None = None
    branch: str | None = None
    detached: bool = False
    dirty: bool = False
    untracked: int = 0
    modified: int = 0
    staged: int = 0
    last_commit: str | None = None
    last_commit_subject: str | None = None
    remotes: list[str] = Field(default_factory=list)
    ignores_agent_dir: bool = False


class ArchitectureInfo(Base):
    """Inferred, never imposed - the agent must follow what already exists."""

    language: str = "javascript"
    state_management: list[str] = Field(default_factory=list)
    navigation: list[str] = Field(default_factory=list)
    api_layer: list[str] = Field(default_factory=list)
    data_fetching: list[str] = Field(default_factory=list)
    styling: list[str] = Field(default_factory=list)
    forms: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    testing: list[str] = Field(default_factory=list)
    i18n: list[str] = Field(default_factory=list)
    analytics: list[str] = Field(default_factory=list)
    source_root: str | None = None
    feature_layout: str | None = None
    directories: dict[str, str] = Field(default_factory=dict)
    conventions: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, str]:
        def first(values: list[str]) -> str:
            return values[0] if values else "none detected"

        return {
            "language": self.language,
            "state_management": first(self.state_management),
            "navigation": first(self.navigation),
            "api_layer": first(self.api_layer),
            "styling": first(self.styling),
            "testing": first(self.testing),
        }


class SourceStats(Base):
    files: int = 0
    typescript_files: int = 0
    javascript_files: int = 0
    test_files: int = 0
    component_files: int = 0
    screen_files: int = 0
    hook_files: int = 0
    total_lines: int = 0
    largest_files: list[dict[str, Any]] = Field(default_factory=list)
    top_level_dirs: list[str] = Field(default_factory=list)


class ToolingInfo(Base):
    node: str | None = None
    npm: str | None = None
    yarn: str | None = None
    pnpm: str | None = None
    bun: str | None = None
    git: str | None = None
    java: str | None = None
    cocoapods: str | None = None
    xcodebuild: str | None = None
    adb: str | None = None
    watchman: str | None = None
    ruby: str | None = None


class ProjectContext(Base):
    schema_version: int = CONTEXT_SCHEMA_VERSION
    agent_version: str = ""
    generated_at: str = Field(default_factory=_now)
    scan_duration_ms: int = 0

    root: str = ""
    name: str | None = None
    version: str | None = None
    private: bool = False

    react_native: ReactNativeInfo = Field(default_factory=ReactNativeInfo)
    package_manager: PackageManagerInfo = Field(default_factory=PackageManagerInfo)
    android: AndroidInfo = Field(default_factory=AndroidInfo)
    ios: IOSInfo = Field(default_factory=IOSInfo)
    git: GitInfo = Field(default_factory=GitInfo)
    architecture: ArchitectureInfo = Field(default_factory=ArchitectureInfo)
    source: SourceStats = Field(default_factory=SourceStats)
    tooling: ToolingInfo = Field(default_factory=ToolingInfo)

    dependencies: list[DependencyInfo] = Field(default_factory=list)
    scripts: dict[str, str] = Field(default_factory=dict)
    native_modules: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    node_modules_present: bool = False

    # -- lookups -----------------------------------------------------------
    def dependency(self, name: str) -> DependencyInfo | None:
        for dependency in self.dependencies:
            if dependency.name == name:
                return dependency
        return None

    def has_dependency(self, name: str) -> bool:
        return self.dependency(name) is not None

    def dependency_names(self) -> set[str]:
        return {dependency.name for dependency in self.dependencies}

    def native_dependencies(self) -> list[DependencyInfo]:
        return [dependency for dependency in self.dependencies if dependency.native]

    @property
    def rn_version(self) -> str | None:
        return self.react_native.version

    @property
    def is_typescript(self) -> bool:
        return self.react_native.typescript
