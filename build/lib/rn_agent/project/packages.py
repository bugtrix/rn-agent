"""Dependencies, package manager and native-module detection.

Facts first: when ``node_modules`` exists the installed version, the peer
dependencies and the presence of platform code are read from disk. Only when
dependencies are not installed does the agent fall back to the declared range
plus the curated heuristics, and it records which method it used.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..constants import LOCKFILES, REACT_PACKAGE, RN_PACKAGE
from ..knowledge.data import KnowledgeData
from ..models.project import DependencyInfo, DependencyKind, PackageManagerInfo
from ..utils.io import read_json, read_text
from ..utils.semver import satisfies

NATIVE_MARKER_DIRS = ("android", "ios", "apple", "windows", "macos")
RN_SCOPE_PREFIXES = ("react-native-", "@react-native-", "@react-native/")


@dataclass(frozen=True, slots=True)
class InstalledPackage:
    """What ``node_modules/<name>/package.json`` says."""

    name: str
    version: str | None
    peer_dependencies: dict[str, str]
    native_platforms: tuple[str, ...]
    engines: dict[str, str]
    deprecated: str | None = None

    @property
    def native(self) -> bool:
        return bool(self.native_platforms)


def detect_package_manager(root: Path, package_json: dict) -> PackageManagerInfo:
    """Resolve the package manager deterministically.

    Order of authority: the ``packageManager`` field (corepack), then the
    lockfile. Projects with several lockfiles are reported so the developer can
    delete the stale ones - a real and common source of broken installs.
    """
    found = [name for name in LOCKFILES if (root / name).is_file()]
    declared = package_json.get("packageManager")
    declared_name: str | None = None
    declared_version: str | None = None
    if isinstance(declared, str) and declared:
        declared_name = declared.split("@", 1)[0].strip() or None
        if "@" in declared:
            declared_version = declared.split("@", 1)[1].split("+", 1)[0].strip() or None

    chosen: str | None = None
    lockfile: str | None = None
    if declared_name and declared_name in set(LOCKFILES.values()):
        chosen = declared_name
        for name in found:
            if LOCKFILES[name] == declared_name:
                lockfile = name
                break
    if chosen is None and found:
        newest = max(found, key=lambda name: (root / name).stat().st_mtime)
        chosen = LOCKFILES[newest]
        lockfile = newest

    workspaces: list[str] = []
    raw_workspaces = package_json.get("workspaces")
    if isinstance(raw_workspaces, list):
        workspaces = [str(entry) for entry in raw_workspaces]
    elif isinstance(raw_workspaces, dict) and isinstance(raw_workspaces.get("packages"), list):
        workspaces = [str(entry) for entry in raw_workspaces["packages"]]

    return PackageManagerInfo(
        name=chosen or "unknown",
        lockfile=lockfile,
        lockfiles_found=found,
        declared=declared if isinstance(declared, str) else None,
        version=declared_version,
        workspaces=workspaces,
    )


def read_installed_package(node_modules: Path, name: str) -> InstalledPackage | None:
    """Read one installed package's metadata, or ``None`` when absent."""
    package_dir = node_modules / Path(*name.split("/"))
    payload = read_json(package_dir / "package.json")
    if not isinstance(payload, dict):
        return None
    platforms: list[str] = []
    for marker in NATIVE_MARKER_DIRS:
        if (package_dir / marker).is_dir():
            platforms.append("ios" if marker in ("apple",) else marker)
    if not any(platform in platforms for platform in ("ios",)):
        try:
            if any(package_dir.glob("*.podspec")):
                platforms.append("ios")
        except OSError:  # pragma: no cover - unreadable package
            pass
    peers = payload.get("peerDependencies")
    engines = payload.get("engines")
    return InstalledPackage(
        name=name,
        version=str(payload["version"]) if payload.get("version") else None,
        peer_dependencies={
            str(key): str(value) for key, value in (peers or {}).items() if value is not None
        }
        if isinstance(peers, dict)
        else {},
        native_platforms=tuple(dict.fromkeys(platforms)),
        engines={
            str(key): str(value) for key, value in (engines or {}).items() if value is not None
        }
        if isinstance(engines, dict)
        else {},
        deprecated=str(payload["deprecated"]) if payload.get("deprecated") else None,
    )


def _heuristic_native(name: str, knowledge: KnowledgeData) -> bool:
    """Best-effort native guess used only when node_modules is missing."""
    if name in knowledge.javascript_only:
        return False
    if name in {RN_PACKAGE, REACT_PACKAGE}:
        return False
    if name.startswith("@react-native/"):
        return False
    return name.startswith(RN_SCOPE_PREFIXES) or name.startswith(
        ("expo-", "@react-native-firebase/", "@shopify/react-native-", "@notifee/")
    )


def collect_dependencies(
    root: Path,
    package_json: dict,
    knowledge: KnowledgeData,
) -> tuple[list[DependencyInfo], bool, str]:
    """Build the dependency list.

    Returns ``(dependencies, node_modules_present, native_detection_method)``.
    """
    node_modules = root / "node_modules"
    installed_present = node_modules.is_dir()
    sections: tuple[tuple[str, DependencyKind], ...] = (
        ("dependencies", DependencyKind.PROD),
        ("devDependencies", DependencyKind.DEV),
        ("peerDependencies", DependencyKind.PEER),
        ("optionalDependencies", DependencyKind.OPTIONAL),
    )

    collected: dict[str, DependencyInfo] = {}
    for section, kind in sections:
        block = package_json.get(section)
        if not isinstance(block, dict):
            continue
        for name, declared in block.items():
            key = str(name)
            if key in collected:
                continue
            installed = read_installed_package(node_modules, key) if installed_present else None
            if installed is not None:
                native = installed.native
                platforms = list(installed.native_platforms)
            else:
                native = _heuristic_native(key, knowledge)
                platforms = ["android", "ios"] if native else []
            collected[key] = DependencyInfo(
                name=key,
                declared=str(declared) if declared is not None else None,
                installed=installed.version if installed else None,
                kind=kind,
                native=native,
                platforms=platforms,
                peer_dependencies=installed.peer_dependencies if installed else {},
            )

    method = "filesystem" if installed_present else "heuristic"
    return sorted(collected.values(), key=lambda item: item.name), installed_present, method


def scripts_from(package_json: dict) -> dict[str, str]:
    block = package_json.get("scripts")
    if not isinstance(block, dict):
        return {}
    return {str(key): str(value) for key, value in block.items()}


def lockfile_package_version(
    root: Path,
    lockfile: str | None,
    package: str,
    *,
    declared: str | None = None,
) -> str | None:
    """Resolve a package version from a lockfile, or ``None`` if ambiguous.

    Used only as a hint when ``node_modules`` is missing. A lockfile routinely
    holds several entries for the same package (``react-native@*`` pulled in
    transitively next to ``react-native@0.79.1`` for the app itself), so the
    entry that satisfies the *declared* range wins. When nothing can be
    resolved unambiguously this returns ``None`` rather than a wrong answer.
    """
    if not lockfile:
        return None
    path = root / lockfile
    if lockfile in ("package-lock.json", "npm-shrinkwrap.json"):
        return _npm_lock_version(path, package)
    if lockfile == "yarn.lock":
        return _yarn_lock_version(path, package, declared)
    return None


def _npm_lock_version(path: Path, package: str) -> str | None:
    payload = read_json(path)
    if not isinstance(payload, dict):
        return None
    packages = payload.get("packages")
    if isinstance(packages, dict):
        # The hoisted top-level entry is the version the app actually gets.
        entry = packages.get(f"node_modules/{package}")
        if isinstance(entry, dict) and entry.get("version"):
            return str(entry["version"])
    dependencies = payload.get("dependencies")
    if isinstance(dependencies, dict):
        entry = dependencies.get(package)
        if isinstance(entry, dict) and entry.get("version"):
            return str(entry["version"])
    return None


def _yarn_lock_entries(text: str, package: str) -> list[tuple[tuple[str, ...], str]]:
    """``[(specs, version), ...]`` for one package in a yarn v1 lockfile."""
    entries: list[tuple[tuple[str, ...], str]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith((" ", "\t")) or not line.rstrip().endswith(":"):
            continue
        keys = [part.strip().strip('"') for part in line.rstrip().rstrip(":").split(",")]
        specs: list[str] = []
        for key in keys:
            name, _, spec = key.rpartition("@")
            if name == package:
                specs.append(spec)
        if not specs:
            continue
        for follow in lines[index + 1 : index + 10]:
            if follow and not follow.startswith((" ", "\t")):
                break
            candidate = follow.strip()
            if candidate.startswith("version"):
                version = candidate.split(" ", 1)[-1].strip().strip('"')
                entries.append((tuple(specs), version))
                break
    return entries


def _yarn_lock_version(path: Path, package: str, declared: str | None) -> str | None:
    text = read_text(path)
    if text is None:
        return None
    entries = _yarn_lock_entries(text, package)
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0][1]
    if declared:
        # Prefer the entry whose key literally matches what package.json asks
        # for, then any entry whose resolved version satisfies the range.
        for specs, version in entries:
            if declared in specs:
                return version
        for _specs, version in entries:
            if satisfies(version, declared):
                return version
    return None
