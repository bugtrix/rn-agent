"""Android project parsing.

Gradle is a programming language, so this is deliberately a *pragmatic* reader
rather than a Gradle evaluator. It handles the shapes React Native templates and
real apps actually use:

* ``compileSdk 35`` / ``compileSdkVersion = 35`` literals
* ``compileSdk rootProject.ext.compileSdkVersion`` indirection into the root
  ``build.gradle``'s ``ext { }`` block (the RN template default)
* AGP declared as ``classpath("com.android.tools.build:gradle:8.7.2")`` *and*
  the version-less form used since RN 0.76, where the version comes from the
  React Native Gradle plugin - reported as unknown instead of guessed
* ``gradle-wrapper.properties`` distribution URLs
* ``gradle.properties`` flags (``newArchEnabled``, ``hermesEnabled``)

Every value is optional: an unparsable file yields ``None``, never an exception.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models.project import AndroidInfo
from ..utils.io import read_text
from ..utils.semver import coerce

_EXT_ASSIGN_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>[^\n/]+)", re.MULTILINE
)
_SDK_KEYS = {
    "compile_sdk": ("compileSdk", "compileSdkVersion"),
    "target_sdk": ("targetSdk", "targetSdkVersion"),
    "min_sdk": ("minSdk", "minSdkVersion"),
}
_AGP_RE = re.compile(r"com\.android\.tools\.build:gradle(?::(?P<version>[0-9][0-9.\-A-Za-z]*))?")
_AGP_PLUGIN_RE = re.compile(
    r"id\s*\(?\s*[\"']com\.android\.(?:application|library)[\"']\s*\)?\s*version\s*[\"'](?P<version>[^\"']+)[\"']"
)
_KOTLIN_RE = re.compile(
    r"(?:kotlinVersion|kotlin_version)\s*=\s*[\"'](?P<version>[^\"']+)[\"']|"
    r"org\.jetbrains\.kotlin[:.](?:android|jvm|gradle-plugin)?[\"']?\s*version\s*[\"'](?P<version2>[^\"']+)[\"']|"
    r"org\.jetbrains\.kotlin:kotlin-gradle-plugin:(?P<version3>[0-9][^\"'\s]*)"
)
_NDK_RE = re.compile(r"ndkVersion\s*=?\s*[\"'](?P<version>[^\"']+)[\"']")
_BUILD_TOOLS_RE = re.compile(r"buildToolsVersion\s*=?\s*[\"'](?P<version>[^\"']+)[\"']")
_NAMESPACE_RE = re.compile(r"namespace\s*=?\s*[\"'](?P<value>[^\"']+)[\"']")
_APP_ID_RE = re.compile(r"applicationId\s*=?\s*[\"'](?P<value>[^\"']+)[\"']")
_JAVA_SOURCE_RE = re.compile(
    r"sourceCompatibility\s*=?\s*(?:JavaVersion\.VERSION_)?[\"']?(?P<value>[0-9_]+)[\"']?"
)
_JAVA_TARGET_RE = re.compile(
    r"targetCompatibility\s*=?\s*(?:JavaVersion\.VERSION_)?[\"']?(?P<value>[0-9_]+)[\"']?"
)
_PERMISSION_RE = re.compile(r"<uses-permission[^>]*android:name\s*=\s*\"(?P<name>[^\"]+)\"")
_FLAVOR_BLOCK_RE = re.compile(r"productFlavors\s*\{(?P<body>.*?)\n\s{4}\}", re.DOTALL)
_FLAVOR_NAME_RE = re.compile(r"^\s{8}(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*\{", re.MULTILINE)
_SIGNING_RE = re.compile(r"signingConfigs\s*\{(?P<body>.*?)\n\s{4}\}", re.DOTALL)
_SIGNING_NAME_RE = re.compile(r"^\s{8}(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*\{", re.MULTILINE)
_EXPORTED_ACTIVITY_RE = re.compile(
    r"<(?P<tag>activity|service|receiver)\b(?P<attrs>[^>]*)>(?P<body>.*?)</\1>",
    re.DOTALL,
)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def parse_properties(text: str | None) -> dict[str, str]:
    """Minimal ``.properties`` reader (``key=value``, ``#`` comments)."""
    values: dict[str, str] = {}
    if not text:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def parse_ext_block(root_gradle: str | None) -> dict[str, str]:
    """Collect ``ext { key = value }`` assignments from the root build.gradle."""
    if not root_gradle:
        return {}
    cleaned = _strip_comments(root_gradle)
    values: dict[str, str] = {}
    for match in _EXT_ASSIGN_RE.finditer(cleaned):
        key = match.group("key")
        value = match.group("value").strip().rstrip(",").strip()
        values[key] = value.strip("\"'")
    return values


def _resolve_number(raw: str | None, ext: dict[str, str]) -> int | None:
    """Turn a Gradle value (literal or ``rootProject.ext.x``) into an int."""
    if raw is None:
        return None
    candidate = raw.strip().strip("\"'")
    if candidate.isdigit():
        return int(candidate)
    reference = candidate.split(".")[-1]
    resolved = ext.get(reference)
    if resolved is None:
        return None
    resolved = resolved.strip().strip("\"'")
    return int(resolved) if resolved.isdigit() else None


def _find_sdk(text: str, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        match = re.search(rf"\b{key}\b\s*=?\s*(?P<value>[^\s\n]+)", text)
        if match:
            return match.group("value").strip().rstrip(")").rstrip(",")
    return None


def _first_group(match: re.Match[str] | None) -> str | None:
    if match is None:
        return None
    for value in match.groupdict().values():
        if value:
            return str(value)
    return None


def _bool_flag(properties: dict[str, str], key: str) -> bool | None:
    raw = properties.get(key)
    if raw is None:
        return None
    return raw.strip().lower() in {"true", "1", "yes"}


def _count_sources(directory: Path, suffix: str) -> int:
    if not directory.is_dir():
        return 0
    try:
        return sum(1 for _ in directory.rglob(f"*{suffix}"))
    except OSError:  # pragma: no cover - unreadable tree
        return 0


def _manifest_missing_exported(manifest_text: str) -> list[str]:
    """Components with an intent-filter but no ``android:exported``.

    Required since Android 12 (API 31): the app fails to install without it.
    """
    offenders: list[str] = []
    for match in _EXPORTED_ACTIVITY_RE.finditer(manifest_text):
        attributes = match.group("attrs")
        body = match.group("body")
        if "intent-filter" not in body:
            continue
        if "android:exported" in attributes:
            continue
        name = re.search(r"android:name\s*=\s*\"(?P<name>[^\"]+)\"", attributes)
        offenders.append(name.group("name") if name else match.group("tag"))
    return offenders


def analyze_android(root: Path) -> tuple[AndroidInfo, list[str]]:
    """Parse ``android/``. Returns the model plus non-fatal notes."""
    android_dir = root / "android"
    notes: list[str] = []
    if not android_dir.is_dir():
        return AndroidInfo(present=False), notes

    root_gradle = read_text(android_dir / "build.gradle") or read_text(
        android_dir / "build.gradle.kts"
    )
    app_gradle = read_text(android_dir / "app" / "build.gradle") or read_text(
        android_dir / "app" / "build.gradle.kts"
    )
    wrapper = parse_properties(read_text(android_dir / "gradle" / "wrapper" / "gradle-wrapper.properties"))
    gradle_properties = parse_properties(read_text(android_dir / "gradle.properties"))
    settings_gradle = read_text(android_dir / "settings.gradle") or read_text(
        android_dir / "settings.gradle.kts"
    )

    ext = parse_ext_block(root_gradle)
    combined_gradle = _strip_comments("\n".join(part for part in (root_gradle, app_gradle) if part))

    gradle_version = coerce(wrapper.get("distributionUrl"))
    agp_version = _first_group(_AGP_PLUGIN_RE.search(combined_gradle)) or _first_group(
        _AGP_RE.search(combined_gradle)
    )
    if agp_version is None and root_gradle and "com.android.tools.build:gradle" in root_gradle:
        notes.append(
            "Android Gradle Plugin version is not pinned in build.gradle "
            "(resolved by the React Native Gradle plugin); reported as unknown."
        )

    kotlin_version = _first_group(_KOTLIN_RE.search(combined_gradle)) or ext.get("kotlinVersion")
    ndk_version = _first_group(_NDK_RE.search(combined_gradle)) or ext.get("ndkVersion")
    build_tools = _first_group(_BUILD_TOOLS_RE.search(combined_gradle)) or ext.get(
        "buildToolsVersion"
    )

    sdk_values: dict[str, int | None] = {}
    for field_name, keys in _SDK_KEYS.items():
        raw = _find_sdk(app_gradle or "", keys) or _find_sdk(root_gradle or "", keys)
        sdk_values[field_name] = _resolve_number(raw, ext)

    manifest_path = android_dir / "app" / "src" / "main" / "AndroidManifest.xml"
    manifest_text = read_text(manifest_path)
    permissions = sorted({match.group("name") for match in _PERMISSION_RE.finditer(manifest_text or "")})
    if manifest_text:
        missing_exported = _manifest_missing_exported(manifest_text)
        if missing_exported:
            notes.append(
                "AndroidManifest components with intent-filters are missing android:exported: "
                + ", ".join(missing_exported[:5])
            )

    java_main = android_dir / "app" / "src" / "main" / "java"
    kotlin_main = android_dir / "app" / "src" / "main" / "kotlin"
    main_application = _find_file(android_dir, ("MainApplication.kt", "MainApplication.java"))
    main_activity = _find_file(android_dir, ("MainActivity.kt", "MainActivity.java"))

    flavors: list[str] = []
    flavor_block = _FLAVOR_BLOCK_RE.search(app_gradle or "")
    if flavor_block:
        flavors = [match.group("name") for match in _FLAVOR_NAME_RE.finditer(flavor_block.group("body"))]
    signing: list[str] = []
    signing_block = _SIGNING_RE.search(app_gradle or "")
    if signing_block:
        signing = [match.group("name") for match in _SIGNING_NAME_RE.finditer(signing_block.group("body"))]

    interesting_flags = {
        key: value
        for key, value in gradle_properties.items()
        if key
        in {
            "newArchEnabled",
            "hermesEnabled",
            "reactNativeArchitectures",
            "android.useAndroidX",
            "android.enableJetifier",
            "org.gradle.jvmargs",
            "org.gradle.parallel",
            "org.gradle.caching",
            "expo.useLegacyPackaging",
            "kotlin.incremental",
            "bundleInDebug",
            "bundleInRelease",
        }
    }

    info = AndroidInfo(
        present=True,
        gradle_version=str(gradle_version) if gradle_version else None,
        agp_version=agp_version,
        kotlin_version=kotlin_version,
        ndk_version=ndk_version,
        build_tools_version=build_tools,
        compile_sdk=sdk_values.get("compile_sdk"),
        target_sdk=sdk_values.get("target_sdk"),
        min_sdk=sdk_values.get("min_sdk"),
        java_source_compatibility=_normalise_java(_first_group(_JAVA_SOURCE_RE.search(app_gradle or ""))),
        java_target_compatibility=_normalise_java(_first_group(_JAVA_TARGET_RE.search(app_gradle or ""))),
        namespace=_first_group(_NAMESPACE_RE.search(app_gradle or "")),
        application_id=_first_group(_APP_ID_RE.search(app_gradle or "")),
        new_architecture=_bool_flag(gradle_properties, "newArchEnabled"),
        hermes_enabled=_bool_flag(gradle_properties, "hermesEnabled"),
        permissions=permissions,
        manifest_path=str(manifest_path.relative_to(root)) if manifest_text else None,
        main_application=_relative(main_application, root),
        main_activity=_relative(main_activity, root),
        kotlin_sources=_count_sources(kotlin_main, ".kt") + _count_sources(java_main, ".kt"),
        java_sources=_count_sources(java_main, ".java"),
        gradle_properties=interesting_flags,
        flavors=flavors,
        signing_configs=signing,
    )

    if settings_gradle and "includeBuild" in settings_gradle:
        notes.append("android/settings.gradle uses includeBuild (composite build detected).")
    return info, notes


def _normalise_java(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.replace("_", ".").strip()
    if cleaned.startswith("1.") and cleaned.count(".") == 1:
        return cleaned
    return cleaned


def _find_file(base: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        try:
            match = next(base.rglob(name), None)
        except OSError:  # pragma: no cover - unreadable tree
            match = None
        if match is not None:
            return match
    return None


def _relative(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(root))
    except ValueError:  # pragma: no cover - outside the project
        return str(path)
