"""iOS project parsing.

Reads the files that actually determine an iOS build:

* ``Podfile`` - platform line (literal ``platform :ios, '15.1'`` *or* the RN
  helper ``platform :ios, min_ios_version_supported``), ``use_frameworks!``
* ``Podfile.lock`` - the CocoaPods version that produced it and the resolved
  React Native pod version (a mismatch with package.json means "run pod install")
* ``*.xcodeproj/project.pbxproj`` - ``IPHONEOS_DEPLOYMENT_TARGET``, bundle id
* ``Info.plist`` - display name, usage descriptions (never secrets)
* ``PrivacyInfo.xcprivacy`` / ``*.entitlements`` - presence only

Signing material (``*.mobileprovision``, ``*.p12``, keys) is never opened.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

from ..models.project import IOSInfo
from ..utils.io import read_text
from ..utils.semver import Version, coerce

_PLATFORM_RE = re.compile(r"platform\s+:ios\s*,\s*(?P<value>[^\n#]+)")
_USE_FRAMEWORKS_RE = re.compile(r"use_frameworks!\s*(?::linkage\s*=>\s*:?(?P<linkage>\w+))?")
_LINKAGE_ENV_RE = re.compile(r"ENV\[[\"']USE_FRAMEWORKS[\"']\]")
_COCOAPODS_RE = re.compile(r"^COCOAPODS:\s*(?P<version>[0-9][^\s]*)", re.MULTILINE)
_POD_RN_RE = re.compile(r"^\s{2}-\s+React-Core\s+\((?P<version>[^)]+)\)", re.MULTILINE)
_DEPLOYMENT_TARGET_RE = re.compile(r"IPHONEOS_DEPLOYMENT_TARGET\s*=\s*(?P<value>[0-9.]+)")
_BUNDLE_ID_RE = re.compile(r"PRODUCT_BUNDLE_IDENTIFIER\s*=\s*(?P<value>[^;\n]+)")
_NEW_ARCH_RE = re.compile(r"RCT_NEW_ARCH_ENABLED\s*[=:]\s*[\"']?(?P<value>[01])")


def _count(directory: Path, suffix: str) -> int:
    if not directory.is_dir():
        return 0
    try:
        return sum(1 for _ in directory.rglob(f"*{suffix}"))
    except OSError:  # pragma: no cover
        return 0


def _read_plist(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _first_match(pattern: re.Pattern[str], text: str | None, group: str = "value") -> str | None:
    if not text:
        return None
    match = pattern.search(text)
    return match.group(group).strip() if match else None


def analyze_ios(root: Path, *, declared_rn_version: str | None = None) -> tuple[IOSInfo, list[str]]:
    """Parse ``ios/``. Returns the model plus non-fatal notes."""
    ios_dir = root / "ios"
    notes: list[str] = []
    if not ios_dir.is_dir():
        return IOSInfo(present=False), notes

    podfile = read_text(ios_dir / "Podfile")
    podfile_lock = read_text(ios_dir / "Podfile.lock")

    platform_raw = _first_match(_PLATFORM_RE, podfile)
    # Keep the literal as written ("15.1", or the RN helper
    # `min_ios_version_supported`); comparisons coerce it where needed.
    podfile_platform = platform_raw.strip().strip("\"'").rstrip(",") if platform_raw else None

    xcodeproj = next(iter(sorted(ios_dir.glob("*.xcodeproj"))), None)
    workspace = next(iter(sorted(ios_dir.glob("*.xcworkspace"))), None)
    project_name = xcodeproj.stem if xcodeproj else None

    pbxproj_text = read_text(xcodeproj / "project.pbxproj") if xcodeproj else None
    deployment_targets = sorted(
        {match.group("value") for match in _DEPLOYMENT_TARGET_RE.finditer(pbxproj_text or "")},
        key=lambda value: coerce(value) or Version(0, 0, 0),
    )
    deployment_target = deployment_targets[0] if deployment_targets else None
    deployment_source = "project.pbxproj" if deployment_target else None
    if deployment_target is None and podfile_platform and coerce(podfile_platform):
        deployment_target = str(coerce(podfile_platform))
        deployment_source = "Podfile"
    if len(deployment_targets) > 1:
        notes.append(
            "Multiple IPHONEOS_DEPLOYMENT_TARGET values in project.pbxproj: "
            + ", ".join(deployment_targets)
        )

    bundle_identifier = _first_match(_BUNDLE_ID_RE, pbxproj_text)
    if bundle_identifier:
        bundle_identifier = bundle_identifier.strip().strip('"')

    info_plist_path = _find_info_plist(ios_dir, project_name)
    info_plist = _read_plist(info_plist_path) if info_plist_path else {}
    usage_descriptions = sorted(
        key for key in info_plist if isinstance(key, str) and key.endswith("UsageDescription")
    )
    display_name = info_plist.get("CFBundleDisplayName")
    plist_bundle_id = info_plist.get("CFBundleIdentifier")
    if bundle_identifier is None and isinstance(plist_bundle_id, str):
        bundle_identifier = plist_bundle_id

    entitlements = sorted(
        str(path.relative_to(root)) for path in ios_dir.rglob("*.entitlements") if path.is_file()
    )
    privacy_manifest = any(ios_dir.rglob("PrivacyInfo.xcprivacy"))

    linkage_match = _USE_FRAMEWORKS_RE.search(podfile or "")
    use_frameworks: str | None = None
    if linkage_match:
        use_frameworks = linkage_match.group("linkage") or "dynamic"
    elif podfile and _LINKAGE_ENV_RE.search(podfile):
        use_frameworks = "env-controlled"

    pods_rn_version = _first_match(_POD_RN_RE, podfile_lock, group="version")
    if (
        pods_rn_version
        and declared_rn_version
        and coerce(pods_rn_version)
        and coerce(declared_rn_version)
        and coerce(pods_rn_version) != coerce(declared_rn_version)
    ):
        notes.append(
            f"Podfile.lock pins React-Core {pods_rn_version} but package.json declares "
            f"react-native {declared_rn_version}; run `pod install`."
        )

    app_delegate = _find_app_delegate(ios_dir)
    info = IOSInfo(
        present=True,
        deployment_target=deployment_target,
        deployment_target_source=deployment_source,
        podfile_platform=podfile_platform,
        podfile_present=podfile is not None,
        podfile_lock_present=podfile_lock is not None,
        pods_installed=(ios_dir / "Pods").is_dir(),
        cocoapods_version=_first_match(_COCOAPODS_RE, podfile_lock, group="version"),
        pods_react_native_version=pods_rn_version,
        use_frameworks=use_frameworks,
        workspace=str(workspace.relative_to(root)) if workspace else None,
        xcodeproj=str(xcodeproj.relative_to(root)) if xcodeproj else None,
        project_name=project_name,
        bundle_identifier=bundle_identifier,
        display_name=str(display_name) if isinstance(display_name, str) else None,
        privacy_manifest=privacy_manifest,
        info_plist=_relative(info_plist_path, root),
        usage_descriptions=usage_descriptions,
        entitlements=entitlements,
        app_delegate=_relative(app_delegate, root),
        app_delegate_language=_delegate_language(app_delegate),
        swift_sources=_count(ios_dir, ".swift"),
        objc_sources=_count(ios_dir, ".m") + _count(ios_dir, ".mm"),
        new_architecture=_new_arch_flag(podfile, pbxproj_text),
    )
    return info, notes


def _find_info_plist(ios_dir: Path, project_name: str | None) -> Path | None:
    if project_name:
        candidate = ios_dir / project_name / "Info.plist"
        if candidate.is_file():
            return candidate
    try:
        for candidate in sorted(ios_dir.glob("*/Info.plist")):
            if "Pods" not in candidate.parts and "Tests" not in candidate.parts:
                return candidate
    except OSError:  # pragma: no cover
        return None
    return None


def _find_app_delegate(ios_dir: Path) -> Path | None:
    for name in ("AppDelegate.swift", "AppDelegate.mm", "AppDelegate.m"):
        try:
            match = next(
                (
                    path
                    for path in ios_dir.rglob(name)
                    if "Pods" not in path.parts and "build" not in path.parts
                ),
                None,
            )
        except OSError:  # pragma: no cover
            match = None
        if match is not None:
            return match
    return None


def _delegate_language(path: Path | None) -> str | None:
    if path is None:
        return None
    return {".swift": "swift", ".mm": "objective-c++", ".m": "objective-c"}.get(path.suffix)


def _new_arch_flag(podfile: str | None, pbxproj: str | None) -> bool | None:
    for text in (podfile, pbxproj):
        value = _first_match(_NEW_ARCH_RE, text)
        if value is not None:
            return value == "1"
    return None


def _relative(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(root))
    except ValueError:  # pragma: no cover
        return str(path)
