"""Renders the result of ``rn-agent scan``."""

from __future__ import annotations

from ..cli import ui
from ..models.project import ProjectContext, ReactNativeInfo


def render_scan(context: ProjectContext, *, verbose: bool = False, wrote: bool = True) -> None:
    rn = context.react_native
    ui.header(
        f"{context.name or 'React Native project'}",
        f"React Native {rn.version or 'unknown'}",
    )

    ui.section("Project")
    ui.key_values(
        [
            ("root", context.root),
            ("app version", context.version),
            ("language", context.architecture.language),
            ("package manager", _package_manager(context)),
            ("platforms", _platforms(context)),
            ("scan time", f"{context.scan_duration_ms} ms"),
        ]
    )

    ui.section("React Native")
    ui.key_values(
        [
            ("react-native", _rn_version_line(rn)),
            ("react", _version_pair(rn.react_version, rn.react_declared_range)),
            ("typescript", rn.typescript_version if rn.typescript else "not used"),
            ("hermes", _flag(rn.hermes_enabled)),
            ("new architecture", _flag(rn.new_architecture)),
            ("expo", rn.expo_version if rn.expo else "no"),
            ("node requirement", rn.node_requirement),
            ("configs", _configs(rn)),
        ]
    )

    architecture = context.architecture
    ui.section("Architecture (inferred)")
    ui.key_values(
        [
            ("source root", architecture.source_root),
            ("layout", architecture.feature_layout),
            ("state", _joined(architecture.state_management)),
            ("navigation", _joined(architecture.navigation)),
            ("api layer", _joined(architecture.api_layer)),
            ("data fetching", _joined(architecture.data_fetching)),
            ("styling", _joined(architecture.styling)),
            ("forms", _joined(architecture.forms)),
            ("validation", _joined(architecture.validation)),
            ("testing", _joined(architecture.testing)),
        ]
    )
    if architecture.directories:
        ui.note("directories: " + ", ".join(f"{k}={v}" for k, v in architecture.directories.items()))
    if architecture.conventions:
        ui.note("conventions: " + ", ".join(f"{k}={v}" for k, v in architecture.conventions.items()))

    if context.android.present:
        android = context.android
        ui.section("Android")
        ui.key_values(
            [
                ("gradle", android.gradle_version),
                ("agp", android.agp_version or "not pinned"),
                ("kotlin", android.kotlin_version),
                ("compileSdk", android.compile_sdk),
                ("targetSdk", android.target_sdk),
                ("minSdk", android.min_sdk),
                ("java", android.java_source_compatibility),
                ("namespace", android.namespace or android.application_id),
                ("permissions", len(android.permissions)),
                ("flavors", _joined(android.flavors)),
            ]
        )

    if context.ios.present:
        ios = context.ios
        ui.section("iOS")
        ui.key_values(
            [
                ("project", ios.project_name),
                ("deployment target", ios.deployment_target),
                ("cocoapods", ios.cocoapods_version),
                ("pods installed", "yes" if ios.pods_installed else "no"),
                ("pods react-native", ios.pods_react_native_version),
                ("bundle id", ios.bundle_identifier),
                ("privacy manifest", "yes" if ios.privacy_manifest else "no"),
                ("usage keys", len(ios.usage_descriptions)),
                ("app delegate", ios.app_delegate),
            ]
        )

    ui.section("Dependencies")
    counts = _dependency_counts(context)
    ui.key_values(
        [
            ("total", counts["total"]),
            ("production", counts["prod"]),
            ("development", counts["dev"]),
            ("native modules", len(context.native_modules)),
            ("detection", "filesystem" if context.node_modules_present else "heuristic"),
        ]
    )
    if verbose and context.native_modules:
        ui.note("native: " + ", ".join(context.native_modules[:24]))
        if len(context.native_modules) > 24:
            ui.note(f"... and {len(context.native_modules) - 24} more")

    source = context.source
    if source.files:
        ui.section("Source")
        ui.key_values(
            [
                ("files", source.files),
                ("typescript", source.typescript_files),
                ("javascript", source.javascript_files),
                ("tests", source.test_files),
                ("components", source.component_files),
                ("screens", source.screen_files),
                ("lines", f"{source.total_lines:,}"),
            ]
        )

    git = context.git
    ui.section("Git")
    if git.repository:
        ui.key_values(
            [
                ("branch", git.branch or "detached"),
                ("state", "dirty" if git.dirty else "clean"),
                ("modified", git.modified),
                ("untracked", git.untracked),
                ("last commit", f"{git.last_commit} {git.last_commit_subject or ''}".strip()),
            ]
        )
    else:
        ui.note("not a git repository")

    tooling = context.tooling
    ui.section("Toolchain")
    ui.key_values(
        [
            ("node", tooling.node),
            ("npm", tooling.npm),
            ("yarn", tooling.yarn),
            ("pnpm", tooling.pnpm),
            ("java", tooling.java),
            ("cocoapods", tooling.cocoapods),
            ("xcodebuild", tooling.xcodebuild),
            ("watchman", tooling.watchman),
        ]
    )

    if context.warnings:
        ui.section("Notes")
        for warning in context.warnings:
            ui.bullet(warning, style="warn", marker="!")
    if context.architecture.notes:
        for note in context.architecture.notes:
            ui.note(note)

    ui.blank()
    if wrote:
        ui.success("project context saved to .rn-agent/project-context.json")
        ui.note("every rn-agent command now shares this context. Next: rn-agent health")
    else:
        ui.warning("dry run: nothing was written")


def _package_manager(context: ProjectContext) -> str:
    manager = context.package_manager
    parts = [manager.name]
    if manager.lockfile:
        parts.append(f"({manager.lockfile})")
    if len(manager.lockfiles_found) > 1:
        parts.append(f"[warn]{len(manager.lockfiles_found)} lockfiles![/warn]")
    return " ".join(parts)


def _platforms(context: ProjectContext) -> str:
    platforms = []
    if context.android.present:
        platforms.append("android")
    if context.ios.present:
        platforms.append("ios")
    return ", ".join(platforms) if platforms else "none (managed workflow?)"


def _version_pair(installed: str | None, declared: str | None) -> str:
    if installed and declared and installed != declared.lstrip("^~="):
        return f"{installed} (declared {declared})"
    return installed or declared or "unknown"


def _flag(value: bool | None) -> str:
    if value is None:
        return "not set"
    return "enabled" if value else "disabled"


def _configs(rn: ReactNativeInfo) -> str:
    present = [
        name
        for name in (rn.metro_config, rn.babel_config, rn.rn_config, rn.app_json, rn.tsconfig)
        if name
    ]
    return ", ".join(present) if present else "none"


def _joined(values: list[str]) -> str:
    return ", ".join(values) if values else "none detected"


def _dependency_counts(context: ProjectContext) -> dict[str, int]:
    prod = sum(1 for dependency in context.dependencies if dependency.kind == "prod")
    dev = sum(1 for dependency in context.dependencies if dependency.kind == "dev")
    return {"total": len(context.dependencies), "prod": prod, "dev": dev}


def _rn_version_line(rn: ReactNativeInfo) -> str:
    """`0.79.1 (declared 0.79.1, from yarn.lock)` - always name the source."""
    version = rn.version or "unknown"
    parts = []
    if rn.declared_range and rn.declared_range != version:
        parts.append(f"declared {rn.declared_range}")
    if rn.version_source and rn.version_source != "node_modules":
        parts.append(f"from {rn.version_source}")
    return f"{version} ({', '.join(parts)})" if parts else version
