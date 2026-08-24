"""Android and iOS file parsing against the shapes real projects use."""

from __future__ import annotations

import plistlib
from pathlib import Path

from rn_agent.project.android import analyze_android, parse_ext_block, parse_properties
from rn_agent.project.ios import analyze_ios


# --- android ---------------------------------------------------------------
def test_android_absent(builder):
    builder.write_package_json()
    info, notes = analyze_android(builder.root)
    assert info.present is False
    assert notes == []


def test_android_reads_literal_sdk_values(builder):
    builder.write_package_json().android(use_ext_indirection=False, compile_sdk=34, target_sdk=33, min_sdk=23)
    info, _ = analyze_android(builder.root)
    assert (info.compile_sdk, info.target_sdk, info.min_sdk) == (34, 33, 23)


def test_android_resolves_root_project_ext_indirection(builder):
    """The RN template writes `compileSdk rootProject.ext.compileSdkVersion`."""
    builder.write_package_json().android(use_ext_indirection=True, compile_sdk=35, target_sdk=35, min_sdk=24)
    info, _ = analyze_android(builder.root)
    assert (info.compile_sdk, info.target_sdk, info.min_sdk) == (35, 35, 24)


def test_android_reads_toolchain_versions(builder):
    builder.write_package_json().android(gradle="8.10.2", agp="8.6.0", kotlin="2.0.21", java="17")
    info, _ = analyze_android(builder.root)
    assert info.gradle_version == "8.10.2"
    assert info.agp_version == "8.6.0"
    assert info.kotlin_version == "2.0.21"
    assert info.java_source_compatibility == "17"
    assert info.build_tools_version == "35.0.0"


def test_android_unpinned_agp_is_reported_not_guessed(builder):
    """RN 0.76+ omits the AGP version; the agent must say so, not invent one."""
    builder.write_package_json().android(agp=None)
    info, notes = analyze_android(builder.root)
    assert info.agp_version is None
    assert any("not pinned" in note for note in notes)


def test_android_flags_and_permissions(builder):
    builder.write_package_json().android(
        new_arch=True,
        hermes=False,
        permissions=("android.permission.INTERNET", "android.permission.CAMERA"),
    )
    info, _ = analyze_android(builder.root)
    assert info.new_architecture is True
    assert info.hermes_enabled is False
    assert info.permissions == ["android.permission.CAMERA", "android.permission.INTERNET"]
    assert info.gradle_properties["newArchEnabled"] == "true"


def test_android_missing_exported_is_detected(builder):
    builder.write_package_json().android(exported=False)
    _, notes = analyze_android(builder.root)
    assert any("android:exported" in note for note in notes)
    assert any("MainActivity" in note for note in notes)


def test_android_exported_present_produces_no_note(builder):
    builder.write_package_json().android(exported=True)
    _, notes = analyze_android(builder.root)
    assert not any("android:exported" in note for note in notes)


def test_android_namespace_and_application_id(builder):
    builder.write_package_json().android()
    info, _ = analyze_android(builder.root)
    assert info.namespace == "com.demo.app"
    assert info.application_id == "com.demo.app"


def test_android_handles_unreadable_gradle(builder):
    builder.write_package_json()
    (builder.root / "android").mkdir()
    info, _ = analyze_android(builder.root)
    assert info.present is True
    assert info.compile_sdk is None


def test_parse_properties_ignores_comments():
    values = parse_properties("# comment\nkey=value\n\nother = spaced \n!bang=1\n")
    assert values == {"key": "value", "other": "spaced"}


def test_parse_ext_block_strips_quotes():
    ext = parse_ext_block('ext {\n  kotlinVersion = "2.0.21"\n  minSdkVersion = 24\n}\n')
    assert ext["kotlinVersion"] == "2.0.21"
    assert ext["minSdkVersion"] == "24"


def test_android_flavors_and_signing(builder):
    builder.write_package_json().android()
    app_gradle = builder.root / "android" / "app" / "build.gradle"
    app_gradle.write_text(
        "android {\n"
        '    namespace "com.demo.app"\n'
        "    signingConfigs {\n"
        "        release {\n"
        "            storeFile file('x.keystore')\n"
        "        }\n"
        "    }\n"
        "    productFlavors {\n"
        "        staging {\n"
        "            applicationId 'com.demo.staging'\n"
        "        }\n"
        "        production {\n"
        "            applicationId 'com.demo.app'\n"
        "        }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    info, _ = analyze_android(builder.root)
    assert info.flavors == ["staging", "production"]
    assert info.signing_configs == ["release"]


# --- ios -------------------------------------------------------------------
def test_ios_absent(builder):
    builder.write_package_json()
    info, notes = analyze_ios(builder.root)
    assert info.present is False
    assert notes == []


def test_ios_reads_deployment_target_from_pbxproj(builder):
    """`platform :ios, min_ios_version_supported` is not a literal version."""
    builder.write_package_json().ios(deployment_target="15.1", podfile_platform="min_ios_version_supported")
    info, _ = analyze_ios(builder.root)
    assert info.deployment_target == "15.1"
    assert info.deployment_target_source == "project.pbxproj"
    assert info.podfile_platform == "min_ios_version_supported"


def test_ios_reads_literal_podfile_platform(builder):
    builder.write_package_json().ios(deployment_target="16.0", podfile_platform="15.1")
    info, _ = analyze_ios(builder.root)
    assert info.podfile_platform == "15.1"
    assert info.deployment_target == "16.0"


def test_ios_project_metadata(builder):
    builder.write_package_json().ios(project="Demo", cocoapods="1.15.2")
    info, _ = analyze_ios(builder.root)
    assert info.project_name == "Demo"
    assert info.xcodeproj == "ios/Demo.xcodeproj"
    assert info.workspace == "ios/Demo.xcworkspace"
    assert info.cocoapods_version == "1.15.2"
    assert info.pods_installed is True
    assert info.bundle_identifier == "com.demo.app"
    assert info.use_frameworks == "static"
    assert info.app_delegate_language == "swift"


def test_ios_privacy_manifest_and_usage_keys(builder):
    builder.write_package_json().ios(
        privacy_manifest=True, usage_descriptions=("NSCameraUsageDescription",)
    )
    info, _ = analyze_ios(builder.root)
    assert info.privacy_manifest is True
    assert info.usage_descriptions == ["NSCameraUsageDescription"]


def test_ios_pod_lock_mismatch_is_noted(builder):
    builder.write_package_json().ios(pods_rn="0.79.1")
    _, notes = analyze_ios(builder.root, declared_rn_version="0.81.0")
    assert any("pod install" in note for note in notes)


def test_ios_pod_lock_match_is_silent(builder):
    builder.write_package_json().ios(pods_rn="0.81.0")
    info, notes = analyze_ios(builder.root, declared_rn_version="0.81.0")
    assert info.pods_react_native_version == "0.81.0"
    assert notes == []


def test_ios_entitlements_detected(builder):
    builder.write_package_json().ios(entitlements=True)
    info, _ = analyze_ios(builder.root)
    assert info.entitlements and info.entitlements[0].endswith(".entitlements")


def test_ios_multiple_deployment_targets_noted(builder):
    builder.write_package_json().ios(deployment_target="15.1")
    pbxproj = builder.root / "ios" / "Demo.xcodeproj" / "project.pbxproj"
    pbxproj.write_text(
        "IPHONEOS_DEPLOYMENT_TARGET = 15.1;\nIPHONEOS_DEPLOYMENT_TARGET = 16.4;\n", encoding="utf-8"
    )
    info, notes = analyze_ios(builder.root)
    assert any("Multiple IPHONEOS_DEPLOYMENT_TARGET" in note for note in notes)
    assert info.deployment_target == "15.1"


def test_ios_survives_a_binary_plist(builder):
    builder.write_package_json().ios()
    plist_path = builder.root / "ios" / "Demo" / "Info.plist"
    plist_path.write_bytes(plistlib.dumps({"CFBundleDisplayName": "Demo"}, fmt=plistlib.FMT_BINARY))
    info, _ = analyze_ios(builder.root)
    assert info.display_name == "Demo"


def test_ios_survives_a_corrupt_plist(builder):
    builder.write_package_json().ios()
    (builder.root / "ios" / "Demo" / "Info.plist").write_bytes(b"\x00not a plist")
    info, _ = analyze_ios(builder.root)
    assert info.present is True
    assert info.usage_descriptions == []


def test_ios_never_reads_signing_material(builder, monkeypatch):
    """A provisioning profile or key must never be opened by the parser."""
    builder.write_package_json().ios()
    secret = builder.root / "ios" / "Demo" / "profile.mobileprovision"
    secret.write_bytes(b"SECRET")
    opened: list[str] = []
    real_open = Path.open

    def tracking_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    analyze_ios(builder.root)
    assert not any(name.endswith(".mobileprovision") for name in opened)
