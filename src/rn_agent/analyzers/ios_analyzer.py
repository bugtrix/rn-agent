"""iOS checks: deployment target, CocoaPods state, Info.plist, privacy.

The high-value checks here are the ones that waste a developer's afternoon:

* ``Podfile.lock`` pinning a different React Native than ``package.json``
  (symptom: "module not found" after a version bump - you forgot ``pod install``)
* deployment target disagreeing between ``Podfile`` and ``project.pbxproj``
* a permission-using native module without its ``NS...UsageDescription``
  (symptom: instant crash on first use, App Store rejection)
"""

from __future__ import annotations

from ..models.health import Category, CheckStatus, HealthCheck, Severity
from ..utils.semver import coerce
from .base import Analyzer, summarize

PRIVACY_DOCS = (
    "https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api"
)


class IOSAnalyzer(Analyzer):
    category = Category.IOS
    title = "iOS"

    def run(self) -> list[HealthCheck]:
        ios = self.project.ios
        if not ios.present:
            return [
                self.skip(
                    "ios.absent",
                    "iOS project",
                    "No ios/ directory (Expo managed or JS-only project).",
                )
            ]
        checks = [
            self._xcode_project(),
            self._deployment_target(),
            self._pods_state(),
            self._pods_version_match(),
            self._privacy_manifest(),
            self._entitlements(),
        ]
        checks.extend(self._usage_descriptions())
        return checks

    # -- project -----------------------------------------------------------
    def _xcode_project(self) -> HealthCheck:
        ios = self.project.ios
        if ios.xcodeproj is None:
            return self.fail(
                "ios.project.missing",
                "Xcode project",
                "No .xcodeproj found under ios/.",
                severity=Severity.CRITICAL,
                recommendation="Restore the iOS project or run `npx expo prebuild`.",
            )
        if ios.workspace is None:
            return self.warn(
                "ios.workspace.missing",
                "Xcode workspace",
                "No .xcworkspace: CocoaPods integration builds through the workspace.",
                severity=Severity.MEDIUM,
                recommendation="Run `pod install` in ios/ to generate the workspace.",
            )
        return self.ok(
            "ios.project",
            "Xcode project",
            f"{ios.xcodeproj} + {ios.workspace}",
            evidence={"bundle_id": ios.bundle_identifier or "-"},
        )

    def _deployment_target(self) -> HealthCheck:
        ios = self.project.ios
        if ios.deployment_target is None:
            return self.skip(
                "ios.deployment_target",
                "iOS deployment target",
                "IPHONEOS_DEPLOYMENT_TARGET was not found.",
            )
        podfile_literal = coerce(ios.podfile_platform)
        project_target = coerce(ios.deployment_target)
        if podfile_literal and project_target and podfile_literal != project_target:
            return self.warn(
                "ios.deployment_target.mismatch",
                "iOS deployment target",
                (
                    f"Podfile targets iOS {podfile_literal} but the Xcode project targets "
                    f"{project_target}."
                ),
                severity=Severity.MEDIUM,
                recommendation="Align the Podfile platform line with the project setting.",
                evidence={"podfile": str(podfile_literal), "project": str(project_target)},
            )
        detail = f"iOS {ios.deployment_target} (from {ios.deployment_target_source})"
        if ios.podfile_platform and not podfile_literal:
            detail += f"; Podfile uses `{ios.podfile_platform}`"
        return self.ok("ios.deployment_target", "iOS deployment target", detail)

    # -- CocoaPods ---------------------------------------------------------
    def _pods_state(self) -> HealthCheck:
        ios = self.project.ios
        if not ios.podfile_present:
            return self.warn(
                "ios.podfile.missing",
                "CocoaPods setup",
                "No Podfile in ios/.",
                severity=Severity.HIGH,
                recommendation="Restore the Podfile from the React Native template.",
            )
        if not ios.podfile_lock_present:
            return self.fail(
                "ios.podfile_lock.missing",
                "CocoaPods setup",
                "Podfile.lock is missing: pod versions are not reproducible.",
                severity=Severity.HIGH,
                recommendation="Run `pod install` in ios/ and commit Podfile.lock.",
            )
        if not ios.pods_installed:
            return self.warn(
                "ios.pods.not_installed",
                "CocoaPods setup",
                "ios/Pods is missing; the workspace will not build until pods are installed.",
                severity=Severity.MEDIUM,
                recommendation="Run `pod install` in ios/ (or `bundle exec pod install`).",
            )
        detail = f"Podfile.lock written by CocoaPods {ios.cocoapods_version or 'unknown'}"
        installed = coerce(self.project.tooling.cocoapods)
        locked = coerce(ios.cocoapods_version)
        if installed and locked and installed.major == locked.major and installed != locked:
            detail += f"; local pod is {installed}"
        return self.ok("ios.pods", "CocoaPods setup", detail)

    def _pods_version_match(self) -> HealthCheck:
        ios = self.project.ios
        rn_version = self.project.react_native.version
        if ios.pods_react_native_version is None or rn_version is None:
            return self.skip(
                "ios.pods.rn_match",
                "Pods match React Native",
                "Podfile.lock does not pin React-Core, or the RN version is unknown.",
            )
        locked = coerce(ios.pods_react_native_version)
        declared = coerce(rn_version)
        if locked and declared and locked != declared:
            return self.fail(
                "ios.pods.rn_match",
                "Pods match React Native",
                (
                    f"Podfile.lock pins React-Core {locked} but the project uses React Native "
                    f"{declared}."
                ),
                severity=Severity.HIGH,
                recommendation="Run `pod install` in ios/ after every React Native version change.",
                evidence={"podfile_lock": str(locked), "package_json": str(declared)},
            )
        return self.ok(
            "ios.pods.rn_match",
            "Pods match React Native",
            f"React-Core {locked} matches React Native {declared}",
        )

    # -- privacy / permissions --------------------------------------------
    def _privacy_manifest(self) -> HealthCheck:
        ios = self.project.ios
        effective = self.knowledge.privacy_manifest_effective
        if ios.privacy_manifest:
            return self.ok(
                "ios.privacy_manifest",
                "Privacy manifest",
                "PrivacyInfo.xcprivacy is present",
                docs=PRIVACY_DOCS,
            )
        return self.warn(
            "ios.privacy_manifest",
            "Privacy manifest",
            (
                "No PrivacyInfo.xcprivacy found. App Store submissions have required one for "
                f"apps using listed APIs since {effective.isoformat() if effective else 'May 2024'}."
            ),
            severity=Severity.MEDIUM,
            recommendation="Add a privacy manifest describing required-reason API usage.",
            source=self.knowledge.privacy_manifest_source,
            docs=PRIVACY_DOCS,
        )

    def _usage_descriptions(self) -> list[HealthCheck]:
        ios = self.project.ios
        declared = set(ios.usage_descriptions)
        missing: dict[str, list[str]] = {}
        advisory: dict[str, list[str]] = {}
        sources: dict[str, str] = {}
        for dependency in self.project.dependencies:
            requirement = self.knowledge.permission_for(dependency.name)
            if requirement is None:
                continue
            for key in requirement.ios_keys:
                if key in declared:
                    continue
                bucket = missing if requirement.required else advisory
                bucket.setdefault(key, []).append(dependency.name)
                if requirement.source and key not in sources:
                    sources[key] = requirement.source

        checks: list[HealthCheck] = []
        if missing:
            checks.append(
                self.fail(
                    "ios.usage_descriptions",
                    "Info.plist usage descriptions",
                    _keys_detail(missing, required=True),
                    severity=Severity.CRITICAL,
                    recommendation=(
                        f"Add {'this' if len(missing) == 1 else 'these'} to "
                        f"{ios.info_plist or 'ios/<App>/Info.plist'} inside the top-level "
                        "<dict>, replacing each placeholder with the sentence the user will "
                        "read in the permission prompt:"
                    ),
                    fix=_plist_lines(missing),
                    evidence={
                        key: f"needed by {', '.join(modules)}"
                        for key, modules in sorted(missing.items())
                    }
                    | {f"docs:{key}": url for key, url in sorted(sources.items()) if key in missing},
                    docs="https://developer.apple.com/documentation/bundleresources/information_property_list",
                )
            )
        else:
            checks.append(
                self.ok(
                    "ios.usage_descriptions",
                    "Info.plist usage descriptions",
                    f"{len(declared)} usage description(s) declared, none missing",
                )
            )
        if advisory:
            checks.append(
                self.check(
                    "ios.usage_descriptions.optional",
                    "Optional usage descriptions",
                    CheckStatus.PASS,
                    detail=_keys_detail(advisory, required=False),
                    fix=_plist_lines(advisory),
                    evidence={
                        key: f"only if you use {', '.join(modules)}'s optional features"
                        for key, modules in sorted(advisory.items())
                    },
                )
            )
        return checks

    def _entitlements(self) -> HealthCheck:
        ios = self.project.ios
        needs_push = self.project.has_dependency("@react-native-firebase/messaging") or (
            self.project.has_dependency("@react-native-community/push-notification-ios")
        )
        if not needs_push:
            return self.skip(
                "ios.entitlements",
                "Push entitlement",
                "No push-notification module installed.",
            )
        if not ios.entitlements:
            return self.warn(
                "ios.entitlements.missing",
                "Push entitlement",
                "A push-notification module is installed but no .entitlements file exists.",
                severity=Severity.HIGH,
                recommendation=(
                    "Add the Push Notifications capability in Xcode (creates <App>.entitlements "
                    "with aps-environment)."
                ),
            )
        return self.ok(
            "ios.entitlements",
            "Push entitlement",
            f"{len(ios.entitlements)} entitlements file(s) present",
            evidence={"files": ", ".join(ios.entitlements[:3])},
        )

#: iOS shows this string in the permission prompt, so an empty value is both a
#: bad prompt and an App Store rejection.
_PLACEHOLDER = "<string>Explain why the app needs this</string>"


def _keys_detail(keys: dict[str, list[str]], *, required: bool) -> str:
    """One key is named inline; several are counted, with names in the paste."""
    crash = "iOS terminates the app the first time the API is used."
    if required:
        if len(keys) == 1:
            key, modules = next(iter(keys.items()))
            return f"{key} is required by {', '.join(modules)} but is missing. {crash}"
        return (
            f"{len(keys)} required usage descriptions are missing: "
            + summarize(sorted(keys), limit=4)
            + f". {crash}"
        )
    return (
        f"{len(keys)} optional key(s) not declared, only needed for some features: "
        + summarize(sorted(keys), limit=4)
        + "."
    )


def _plist_lines(keys: dict[str, list[str]]) -> list[str]:
    """The plist entries to paste, annotated with the module that needs each."""
    lines: list[str] = []
    for key in sorted(keys):
        lines.append(f"<!-- {', '.join(keys[key])} -->")
        lines.append(f"<key>{key}</key>")
        lines.append(_PLACEHOLDER)
    return lines
