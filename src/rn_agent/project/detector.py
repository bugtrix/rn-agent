"""Is this a React Native project, and where does it start?

Walks upwards from the working directory looking for a ``package.json`` that
declares ``react-native`` (or ``expo``). Monorepos are handled by continuing
upwards when the nearest ``package.json`` is not the app itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..constants import (
    ANDROID_DIR,
    EXPO_PACKAGE,
    IOS_DIR,
    MAX_SCAN_DEPTH,
    RN_PACKAGE,
)
from ..core.paths import marker_files
from ..errors import NotAReactNativeProject
from ..utils.io import read_json


@dataclass(frozen=True, slots=True)
class DetectedProject:
    """Result of the upward search."""

    root: Path
    package_json: dict
    react_native_declared: str | None
    expo_declared: str | None
    has_android: bool
    has_ios: bool
    markers: tuple[str, ...] = field(default_factory=tuple)
    workspace_root: Path | None = None

    @property
    def name(self) -> str | None:
        value = self.package_json.get("name")
        return str(value) if value else None

    @property
    def is_expo_managed(self) -> bool:
        """Expo without native folders means a managed (prebuild-less) app."""
        return bool(self.expo_declared) and not (self.has_android or self.has_ios)

    @property
    def platforms(self) -> tuple[str, ...]:
        platforms: list[str] = []
        if self.has_android:
            platforms.append("android")
        if self.has_ios:
            platforms.append("ios")
        return tuple(platforms)


def _dependency_version(package_json: dict, name: str) -> str | None:
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = package_json.get(section)
        if isinstance(block, dict) and name in block:
            value = block[name]
            return str(value) if value is not None else None
    return None


def _read_package_json(directory: Path) -> dict | None:
    payload = read_json(directory / "package.json")
    return payload if isinstance(payload, dict) else None


def find_project_root(start: Path, *, max_depth: int = MAX_SCAN_DEPTH) -> Path | None:
    """Nearest ancestor whose ``package.json`` declares React Native or Expo."""
    current = Path(start).expanduser().resolve()
    fallback: Path | None = None
    for _ in range(max_depth + 1):
        package_json = _read_package_json(current)
        if package_json is not None:
            if _dependency_version(package_json, RN_PACKAGE) or _dependency_version(
                package_json, EXPO_PACKAGE
            ):
                return current
            if fallback is None:
                fallback = current
        if current.parent == current:
            break
        current = current.parent
    return fallback


def detect_project(start: Path | None = None) -> DetectedProject:
    """Detect the React Native project containing ``start``.

    Raises :class:`NotAReactNativeProject` with an actionable hint rather than
    guessing, because every later command depends on this being right.
    """
    origin = Path(start or Path.cwd()).expanduser().resolve()
    root = find_project_root(origin)
    if root is None:
        raise NotAReactNativeProject(
            f"no package.json found in {origin} or its parents",
            hint="Run rn-agent from inside a React Native project.",
        )

    package_json = _read_package_json(root) or {}
    rn_declared = _dependency_version(package_json, RN_PACKAGE)
    expo_declared = _dependency_version(package_json, EXPO_PACKAGE)
    has_android = (root / ANDROID_DIR).is_dir()
    has_ios = (root / IOS_DIR).is_dir()

    if not rn_declared and not expo_declared:
        raise NotAReactNativeProject(
            f"{root} does not declare react-native or expo in package.json",
            hint=(
                "rn-agent works on React Native apps. "
                "If this is a monorepo, run it inside the app package."
            ),
        )

    workspace_root: Path | None = None
    parent = root.parent
    for _ in range(MAX_SCAN_DEPTH):
        candidate = _read_package_json(parent)
        if candidate is not None and candidate.get("workspaces"):
            workspace_root = parent
            break
        if parent.parent == parent:
            break
        parent = parent.parent

    return DetectedProject(
        root=root,
        package_json=package_json,
        react_native_declared=rn_declared,
        expo_declared=expo_declared,
        has_android=has_android,
        has_ios=has_ios,
        markers=tuple(marker_files(root)),
        workspace_root=workspace_root,
    )
