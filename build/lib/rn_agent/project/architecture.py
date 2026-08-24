"""Architecture inference.

Requirement §11/§12: infer the architecture that already exists; never impose
one. If a project uses Redux Saga, later commands must keep using Redux Saga.

Inference combines two signals:

* declared dependencies mapped through ``knowledge/data/libraries.yaml``
* the directory layout (``src/``, ``app/``, feature folders, ``services/``)
"""

from __future__ import annotations

from pathlib import Path

from ..knowledge.data import KnowledgeData
from ..models.project import ArchitectureInfo, DependencyInfo

SOURCE_ROOT_CANDIDATES = ("src", "app", "source", "js", "lib")
API_DIR_CANDIDATES = ("services", "api", "network", "http", "client", "data")
STATE_DIR_CANDIDATES = ("store", "redux", "state", "stores", "slices", "sagas")
NAV_DIR_CANDIDATES = ("navigation", "navigators", "routes", "router")
COMPONENT_DIR_CANDIDATES = ("components", "ui", "widgets")
SCREEN_DIR_CANDIDATES = ("screens", "pages", "views", "containers")
HOOK_DIR_CANDIDATES = ("hooks",)
THEME_DIR_CANDIDATES = ("theme", "themes", "styles", "styling", "design-system")
UTIL_DIR_CANDIDATES = ("utils", "helpers", "lib", "common")


def _existing(base: Path, names: tuple[str, ...]) -> str | None:
    for name in names:
        if (base / name).is_dir():
            return name
    return None


def _find_source_root(root: Path) -> Path:
    for name in SOURCE_ROOT_CANDIDATES:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return root


def _feature_layout(source_root: Path) -> str | None:
    """``feature-first`` when features/modules dominate, else ``layer-first``."""
    for name in ("features", "modules", "domains"):
        if (source_root / name).is_dir():
            return "feature-first"
    if any((source_root / name).is_dir() for name in SCREEN_DIR_CANDIDATES):
        return "layer-first"
    return None


def infer_architecture(
    root: Path,
    dependencies: list[DependencyInfo],
    knowledge: KnowledgeData,
    *,
    typescript: bool,
) -> ArchitectureInfo:
    """Build the architecture view of the project."""
    buckets: dict[str, list[str]] = {
        "state_management": [],
        "navigation": [],
        "api_layer": [],
        "data_fetching": [],
        "styling": [],
        "forms": [],
        "validation": [],
        "testing": [],
        "i18n": [],
        "analytics": [],
        "capabilities": [],
    }
    for dependency in dependencies:
        for role, label in knowledge.roles_for(dependency.name):
            bucket = buckets.setdefault(role, [])
            if label not in bucket:
                bucket.append(label)

    source_root = _find_source_root(root)
    directories: dict[str, str] = {}
    for key, candidates in (
        ("api", API_DIR_CANDIDATES),
        ("state", STATE_DIR_CANDIDATES),
        ("navigation", NAV_DIR_CANDIDATES),
        ("components", COMPONENT_DIR_CANDIDATES),
        ("screens", SCREEN_DIR_CANDIDATES),
        ("hooks", HOOK_DIR_CANDIDATES),
        ("theme", THEME_DIR_CANDIDATES),
        ("utils", UTIL_DIR_CANDIDATES),
    ):
        found = _existing(source_root, candidates)
        if found:
            relative = (source_root / found).relative_to(root)
            directories[key] = str(relative)

    notes: list[str] = []
    if not buckets["state_management"]:
        notes.append("No state-management library detected; component state only.")
    if len(buckets["state_management"]) > 2:
        notes.append(
            "Multiple state-management libraries detected: "
            + ", ".join(buckets["state_management"])
        )
    if buckets["api_layer"] and directories.get("api"):
        notes.append(
            f"API calls appear to live in {directories['api']}/ using {buckets['api_layer'][0]}."
        )
    if not buckets["testing"]:
        notes.append("No testing library detected.")

    conventions: dict[str, str] = {}
    if typescript:
        conventions["types"] = "typescript"
    if (root / ".prettierrc").exists() or (root / ".prettierrc.js").exists():
        conventions["formatter"] = "prettier"
    if any((root / name).exists() for name in (".eslintrc.js", ".eslintrc.json", "eslint.config.js", "eslint.config.mjs")):
        conventions["linter"] = "eslint"
    if (root / "patches").is_dir():
        conventions["patching"] = "patch-package"
    if (root / "fastlane").is_dir():
        conventions["release"] = "fastlane"
    if (root / ".husky").is_dir():
        conventions["git_hooks"] = "husky"

    return ArchitectureInfo(
        language="typescript" if typescript else "javascript",
        state_management=buckets["state_management"],
        navigation=buckets["navigation"],
        api_layer=buckets["api_layer"],
        data_fetching=buckets["data_fetching"],
        styling=buckets["styling"],
        forms=buckets["forms"],
        validation=buckets["validation"],
        testing=buckets["testing"],
        i18n=buckets["i18n"],
        analytics=buckets["analytics"],
        source_root=str(source_root.relative_to(root)) if source_root != root else ".",
        feature_layout=_feature_layout(source_root),
        directories=directories,
        conventions=conventions,
        notes=notes,
    )


def architecture_yaml_payload(architecture: ArchitectureInfo, capabilities: list[str]) -> dict:
    """Shape written to ``.rn-agent/architecture.yaml``."""
    return {
        "architecture": {
            "language": architecture.language,
            "source_root": architecture.source_root,
            "layout": architecture.feature_layout,
            "state_management": architecture.state_management,
            "navigation": architecture.navigation,
            "api_layer": architecture.api_layer,
            "data_fetching": architecture.data_fetching,
            "styling": architecture.styling,
            "forms": architecture.forms,
            "validation": architecture.validation,
            "testing": architecture.testing,
            "i18n": architecture.i18n,
            "analytics": architecture.analytics,
            "capabilities": capabilities,
            "directories": architecture.directories,
            "conventions": architecture.conventions,
        },
        "notes": architecture.notes,
    }
