"""The scanner: builds the shared project brain.

``rn-agent scan`` runs this once; every other command reads the result from
``.rn-agent/project-context.json``. Nothing here calls an AI model - the whole
scan is deterministic file reading plus a handful of ``--version`` probes.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..constants import APP_VERSION
from ..core.paths import AgentPaths
from ..errors import ProjectNotScanned
from ..knowledge.data import KnowledgeData, load_knowledge_data
from ..models.project import (
    CONTEXT_SCHEMA_VERSION,
    GitInfo,
    PackageManagerInfo,
    ProjectContext,
    ReactNativeInfo,
    SourceStats,
    ToolingInfo,
)
from ..runner.command_runner import CommandRunner
from ..utils.io import read_json, read_text, write_json
from ..utils.semver import coerce
from .android import analyze_android
from .architecture import infer_architecture
from .detector import DetectedProject
from .ios import analyze_ios
from .packages import (
    collect_dependencies,
    detect_package_manager,
    lockfile_package_version,
    read_installed_package,
    scripts_from,
)

CONFIG_FILE_CANDIDATES = {
    "metro_config": ("metro.config.js", "metro.config.ts", "metro.config.cjs"),
    "babel_config": ("babel.config.js", "babel.config.ts", "babel.config.cjs", ".babelrc"),
    "rn_config": ("react-native.config.js", "react-native.config.ts"),
    "app_json": ("app.json", "app.config.js", "app.config.ts"),
    "tsconfig": ("tsconfig.json",),
}


class ProjectScanner:
    """Turns a detected project into a :class:`ProjectContext`."""

    __slots__ = ("detected", "paths", "runner", "knowledge", "capabilities", "_notes")

    def __init__(
        self,
        detected: DetectedProject,
        paths: AgentPaths,
        runner: CommandRunner,
        *,
        knowledge: KnowledgeData | None = None,
    ) -> None:
        self.detected = detected
        self.paths = paths
        self.runner = runner
        self.knowledge = knowledge or load_knowledge_data()
        self.capabilities: list[str] = []
        self._notes: list[str] = []

    # -- entry point -------------------------------------------------------
    def scan(
        self,
        *,
        probe_tools: bool = True,
        git_info: GitInfo | None = None,
        source_stats: SourceStats | None = None,
    ) -> ProjectContext:
        started = time.perf_counter()
        root = self.detected.root
        package_json = self.detected.package_json

        package_manager = detect_package_manager(root, package_json)
        if len(package_manager.lockfiles_found) > 1:
            self._notes.append(
                "Multiple lockfiles present ("
                + ", ".join(package_manager.lockfiles_found)
                + "); delete the ones you do not use to avoid inconsistent installs."
            )

        dependencies, node_modules_present, detection_method = collect_dependencies(
            root, package_json, self.knowledge
        )
        if not node_modules_present:
            self._notes.append(
                "node_modules is not installed: versions come from package.json ranges and "
                "native-module detection is heuristic. Run "
                f"`{package_manager.install_command}` for exact results."
            )

        react_native = self._react_native_info(root, package_manager, node_modules_present)
        android, android_notes = analyze_android(root)
        self._notes.extend(android_notes)
        ios, ios_notes = analyze_ios(root, declared_rn_version=react_native.version)
        self._notes.extend(ios_notes)

        architecture = infer_architecture(
            root, dependencies, self.knowledge, typescript=react_native.typescript
        )

        native_modules = sorted(
            dependency.name for dependency in dependencies if dependency.native
        )
        capabilities = [
            label
            for dependency in dependencies
            for role, label in self.knowledge.roles_for(dependency.name)
            if role == "capabilities"
        ]

        architecture.notes = list(
            dict.fromkeys([*architecture.notes, *self._native_detection_note(detection_method)])
        )

        context = ProjectContext(
            schema_version=CONTEXT_SCHEMA_VERSION,
            agent_version=APP_VERSION,
            root=str(root),
            name=self.detected.name,
            version=str(package_json.get("version")) if package_json.get("version") else None,
            private=bool(package_json.get("private", False)),
            react_native=react_native,
            package_manager=package_manager,
            android=android,
            ios=ios,
            git=git_info or GitInfo(),
            architecture=architecture,
            source=source_stats or SourceStats(),
            tooling=self._tooling() if probe_tools else ToolingInfo(),
            dependencies=dependencies,
            scripts=scripts_from(package_json),
            native_modules=native_modules,
            node_modules_present=node_modules_present,
            warnings=list(dict.fromkeys(self._notes)),
        )
        context.scan_duration_ms = int((time.perf_counter() - started) * 1000)
        self.capabilities = capabilities
        return context

    # -- pieces ------------------------------------------------------------
    def _native_detection_note(self, method: str) -> list[str]:
        if method == "heuristic":
            return ["Native-module detection was heuristic (node_modules missing)."]
        return []

    def _react_native_info(
        self,
        root: Path,
        package_manager: PackageManagerInfo,
        node_modules_present: bool,
    ) -> ReactNativeInfo:
        package_json = self.detected.package_json
        node_modules = root / "node_modules"

        rn_installed = (
            read_installed_package(node_modules, "react-native") if node_modules_present else None
        )
        react_installed = (
            read_installed_package(node_modules, "react") if node_modules_present else None
        )
        types_react_installed = (
            read_installed_package(node_modules, "@types/react") if node_modules_present else None
        )
        typescript_installed = (
            read_installed_package(node_modules, "typescript") if node_modules_present else None
        )
        expo_installed = read_installed_package(node_modules, "expo") if node_modules_present else None

        declared_rn = self.detected.react_native_declared
        installed_rn = rn_installed.version if rn_installed else None
        version_source = "node_modules" if installed_rn else None
        if installed_rn is None:
            installed_rn = lockfile_package_version(
                root, package_manager.lockfile, "react-native", declared=declared_rn
            )
            if installed_rn:
                version_source = package_manager.lockfile

        resolved = installed_rn or (str(coerce(declared_rn)) if coerce(declared_rn) else None)
        if version_source is None and resolved:
            version_source = "package.json"

        declared_react = _declared(package_json, "react")
        declared_typescript = _declared(package_json, "typescript")

        tsconfig_present = (root / "tsconfig.json").is_file()
        typescript = bool(declared_typescript or typescript_installed or tsconfig_present)

        configs: dict[str, str | None] = {}
        for key, candidates in CONFIG_FILE_CANDIDATES.items():
            configs[key] = next((name for name in candidates if (root / name).is_file()), None)

        hermes, new_arch = self._runtime_flags(root)

        return ReactNativeInfo(
            version=resolved,
            declared_range=declared_rn,
            installed_version=installed_rn if version_source == "node_modules" else None,
            version_source=version_source,
            react_version=react_installed.version
            if react_installed
            else lockfile_package_version(
                root, package_manager.lockfile, "react", declared=declared_react
            ),
            react_declared_range=declared_react,
            react_requirement=(rn_installed.peer_dependencies.get("react") if rn_installed else None),
            types_react_version=types_react_installed.version if types_react_installed else None,
            types_react_requirement=(
                rn_installed.peer_dependencies.get("@types/react") if rn_installed else None
            ),
            node_requirement=(rn_installed.engines.get("node") if rn_installed else None),
            hermes_enabled=hermes,
            new_architecture=new_arch,
            typescript=typescript,
            typescript_version=(
                typescript_installed.version
                if typescript_installed
                else (str(coerce(declared_typescript)) if coerce(declared_typescript) else None)
            ),
            expo=bool(self.detected.expo_declared),
            expo_version=expo_installed.version
            if expo_installed
            else (str(coerce(self.detected.expo_declared)) if coerce(self.detected.expo_declared) else None),
            expo_managed=self.detected.is_expo_managed,
            metro_config=configs["metro_config"],
            babel_config=configs["babel_config"],
            rn_config=configs["rn_config"],
            app_json=configs["app_json"],
            tsconfig=configs["tsconfig"],
            template_source=None,
        )

    def _runtime_flags(self, root: Path) -> tuple[bool | None, bool | None]:
        """Hermes / New Architecture, agreeing across platforms when possible."""
        from .android import parse_properties

        properties = parse_properties(read_text(root / "android" / "gradle.properties"))
        hermes: bool | None = None
        new_arch: bool | None = None
        if "hermesEnabled" in properties:
            hermes = properties["hermesEnabled"].strip().lower() in {"true", "1", "yes"}
        if "newArchEnabled" in properties:
            new_arch = properties["newArchEnabled"].strip().lower() in {"true", "1", "yes"}

        app_json = read_json(root / "app.json")
        if isinstance(app_json, dict):
            expo_block = app_json.get("expo")
            if isinstance(expo_block, dict) and expo_block.get("newArchEnabled") is not None:
                new_arch = bool(expo_block["newArchEnabled"])
        return hermes, new_arch

    def _tooling(self) -> ToolingInfo:
        def version(executable: str, *args: str) -> str | None:
            raw = self.runner.tool_version(executable, args or ("--version",))
            if raw is None:
                return None
            parsed = coerce(raw)
            return str(parsed) if parsed else raw.strip()[:40]

        return ToolingInfo(
            node=version("node"),
            npm=version("npm"),
            yarn=version("yarn"),
            pnpm=version("pnpm"),
            bun=version("bun"),
            git=version("git"),
            java=self._java_version(),
            cocoapods=version("pod"),
            xcodebuild=self._xcodebuild_version(),
            adb=version("adb"),
            watchman=version("watchman"),
            ruby=version("ruby"),
        )

    def _java_version(self) -> str | None:
        if not self.runner.available("java"):
            return None
        result = self.runner.run(["java", "-version"], timeout=25.0, force=True)
        parsed = coerce(result.output)
        return str(parsed) if parsed else None

    def _xcodebuild_version(self) -> str | None:
        if not self.runner.available("xcodebuild"):
            return None
        result = self.runner.run(["xcodebuild", "-version"], timeout=40.0, force=True)
        if not result.ok:
            return None
        parsed = coerce(result.first_line())
        return str(parsed) if parsed else None


def _declared(package_json: dict, name: str) -> str | None:
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        block = package_json.get(section)
        if isinstance(block, dict) and name in block:
            value = block[name]
            return str(value) if value is not None else None
    return None


def save_context(paths: AgentPaths, context: ProjectContext) -> Path:
    """Persist the brain to ``.rn-agent/project-context.json``."""
    paths.ensure()
    return write_json(paths.context_file, context.model_dump(mode="json"))


def load_context(paths: AgentPaths) -> ProjectContext:
    """Read the brain, or explain how to create it."""
    payload = read_json(paths.context_file)
    if not isinstance(payload, dict):
        raise ProjectNotScanned(
            "no project context found",
            hint="Run `rn-agent scan` first - every command shares that result.",
        )
    try:
        context = ProjectContext.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError
        raise ProjectNotScanned(
            f"project context is unreadable: {exc}",
            hint="Run `rn-agent scan` again to regenerate it.",
        ) from exc
    if context.schema_version != CONTEXT_SCHEMA_VERSION:
        raise ProjectNotScanned(
            f"project context was written by an incompatible version "
            f"(schema {context.schema_version}, expected {CONTEXT_SCHEMA_VERSION})",
            hint="Run `rn-agent scan` to refresh it.",
        )
    return context


def context_age_seconds(paths: AgentPaths) -> float | None:
    try:
        return max(0.0, time.time() - paths.context_file.stat().st_mtime)
    except OSError:
        return None
