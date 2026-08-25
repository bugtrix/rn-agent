"""Android checks: Gradle/AGP/JDK alignment, SDK levels, manifest, permissions.

Only rules that are published, stable facts are asserted:

* AGP 8.x requires Gradle 8.x and JDK 17
* Google Play's targetSdk deadlines (dated, from the packaged policy file)
* components with an ``intent-filter`` must declare ``android:exported`` (API 31+)
* permissions required by installed native modules

Anything version-specific that cannot be read from the project is skipped.
"""

from __future__ import annotations

from ..models.health import Category, CheckStatus, HealthCheck, Severity
from ..utils.semver import coerce
from .base import Analyzer, summarize

PLAY_DOCS = "https://developer.android.com/google/play/requirements/target-sdk"
AGP_DOCS = "https://developer.android.com/build/releases/gradle-plugin"


class AndroidAnalyzer(Analyzer):
    category = Category.ANDROID
    title = "Android"

    def run(self) -> list[HealthCheck]:
        android = self.project.android
        if not android.present:
            return [
                self.skip(
                    "android.absent",
                    "Android project",
                    "No android/ directory (Expo managed or JS-only project).",
                )
            ]
        checks = [
            self._gradle(),
            self._agp_gradle_alignment(),
            self._jdk(),
            self._sdk_levels(),
            self._target_sdk_policy(),
            self._manifest(),
            self._kotlin(),
        ]
        checks.extend(self._permissions())
        return checks

    # -- toolchain ---------------------------------------------------------
    def _gradle(self) -> HealthCheck:
        android = self.project.android
        if android.gradle_version is None:
            return self.warn(
                "android.gradle.unknown",
                "Gradle wrapper",
                "Could not read the Gradle version from gradle-wrapper.properties.",
                severity=Severity.LOW,
                recommendation="Ensure android/gradle/wrapper/gradle-wrapper.properties is committed.",
            )
        return self.ok(
            "android.gradle",
            "Gradle wrapper",
            f"Gradle {android.gradle_version}",
            evidence={"gradle": android.gradle_version},
        )

    def _agp_gradle_alignment(self) -> HealthCheck:
        android = self.project.android
        agp = coerce(android.agp_version)
        gradle = coerce(android.gradle_version)
        if agp is None:
            return self.skip(
                "android.agp",
                "AGP / Gradle alignment",
                "AGP version is not pinned in build.gradle (React Native resolves it).",
                source="android/build.gradle",
            )
        if gradle is None:
            return self.skip(
                "android.agp",
                "AGP / Gradle alignment",
                f"AGP {android.agp_version} found but the Gradle version is unknown.",
            )
        if agp.major >= 8 and gradle.major < 8:
            return self.fail(
                "android.agp.gradle",
                "AGP / Gradle alignment",
                f"AGP {agp} requires Gradle 8.0+, but the wrapper is {gradle}.",
                severity=Severity.CRITICAL,
                recommendation="Update the Gradle wrapper to 8.x (`./gradlew wrapper --gradle-version 8.x`).",
                docs=AGP_DOCS,
                evidence={"agp": str(agp), "gradle": str(gradle)},
            )
        if agp.major == 7 and gradle.major < 7:
            return self.fail(
                "android.agp.gradle",
                "AGP / Gradle alignment",
                f"AGP {agp} requires Gradle 7.x, but the wrapper is {gradle}.",
                severity=Severity.HIGH,
                recommendation="Update the Gradle wrapper to 7.x.",
                docs=AGP_DOCS,
            )
        return self.ok(
            "android.agp",
            "AGP / Gradle alignment",
            f"AGP {agp} with Gradle {gradle}",
        )

    def _jdk(self) -> HealthCheck:
        android = self.project.android
        agp = coerce(android.agp_version)
        java_source = android.java_source_compatibility
        installed_java = coerce(self.project.tooling.java)

        if java_source is None and installed_java is None:
            return self.skip(
                "android.jdk",
                "Java toolchain",
                "No compileOptions in app/build.gradle and no java on PATH.",
            )
        if agp is not None and agp.major >= 8 and installed_java is not None:
            java_major = installed_java.major if installed_java.major > 1 else installed_java.minor
            if java_major < 17:
                return self.fail(
                    "android.jdk.version",
                    "Java toolchain",
                    f"AGP {agp} requires JDK 17+, but java on PATH reports {self.project.tooling.java}.",
                    severity=Severity.CRITICAL,
                    recommendation="Install JDK 17 (Temurin/Zulu) and point JAVA_HOME at it.",
                    docs=AGP_DOCS,
                    evidence={"java": str(installed_java), "agp": str(agp)},
                )
        detail = f"sourceCompatibility {java_source}" if java_source else "compileOptions not set"
        if installed_java is not None:
            detail += f", java on PATH {self.project.tooling.java}"
        return self.ok("android.jdk", "Java toolchain", detail)

    def _kotlin(self) -> HealthCheck:
        android = self.project.android
        if android.kotlin_version is None:
            if android.kotlin_sources:
                return self.warn(
                    "android.kotlin.unpinned",
                    "Kotlin version",
                    f"{android.kotlin_sources} Kotlin source file(s) but no kotlinVersion is pinned.",
                    severity=Severity.LOW,
                    recommendation="Pin kotlinVersion in android/build.gradle ext to keep builds reproducible.",
                )
            return self.skip("android.kotlin", "Kotlin version", "No Kotlin sources or version found.")
        return self.ok(
            "android.kotlin",
            "Kotlin version",
            f"Kotlin {android.kotlin_version}",
            evidence={"kotlin_sources": str(android.kotlin_sources)},
        )

    # -- SDK levels --------------------------------------------------------
    def _sdk_levels(self) -> HealthCheck:
        android = self.project.android
        compile_sdk, target_sdk, min_sdk = android.compile_sdk, android.target_sdk, android.min_sdk
        if compile_sdk is None and target_sdk is None and min_sdk is None:
            return self.skip(
                "android.sdk",
                "SDK levels",
                "Could not resolve compileSdk/targetSdk/minSdk from Gradle files.",
            )
        if compile_sdk is not None and target_sdk is not None and target_sdk > compile_sdk:
            return self.fail(
                "android.sdk.target_above_compile",
                "SDK levels",
                f"targetSdk {target_sdk} is higher than compileSdk {compile_sdk}; the build will fail.",
                severity=Severity.CRITICAL,
                recommendation=f"Raise compileSdk to at least {target_sdk}.",
                evidence={"compileSdk": str(compile_sdk), "targetSdk": str(target_sdk)},
            )
        if min_sdk is not None and target_sdk is not None and min_sdk > target_sdk:
            return self.fail(
                "android.sdk.min_above_target",
                "SDK levels",
                f"minSdk {min_sdk} is higher than targetSdk {target_sdk}.",
                severity=Severity.HIGH,
                recommendation="Fix the SDK levels in android/build.gradle.",
            )
        return self.ok(
            "android.sdk",
            "SDK levels",
            f"compileSdk {compile_sdk}, targetSdk {target_sdk}, minSdk {min_sdk}",
            evidence={
                "compileSdk": str(compile_sdk),
                "targetSdk": str(target_sdk),
                "minSdk": str(min_sdk),
            },
        )

    def _target_sdk_policy(self) -> HealthCheck:
        android = self.project.android
        requirement = self.knowledge.required_target_sdk()
        upcoming = self.knowledge.upcoming_target_sdk()
        if android.target_sdk is None:
            return self.skip(
                "android.play_policy",
                "Google Play targetSdk policy",
                "targetSdk could not be resolved.",
            )
        if requirement is None:
            return self.skip(
                "android.play_policy",
                "Google Play targetSdk policy",
                "No in-force policy entry in the packaged data.",
            )
        if android.target_sdk < requirement.target_sdk:
            return self.fail(
                "android.play_policy",
                "Google Play targetSdk policy",
                (
                    f"targetSdk {android.target_sdk} is below API {requirement.target_sdk}, "
                    f"required by Google Play since {requirement.effective.isoformat()}."
                ),
                severity=Severity.CRITICAL,
                recommendation=f"Raise targetSdkVersion (and compileSdk) to {requirement.target_sdk}+.",
                docs=PLAY_DOCS,
                source=self.knowledge.target_sdk_source,
                evidence={"targetSdk": str(android.target_sdk), "required": str(requirement.target_sdk)},
            )
        if upcoming and android.target_sdk < upcoming.target_sdk:
            return self.check(
                "android.play_policy.upcoming",
                "Google Play targetSdk policy",
                status=self._info_status(),
                detail=(
                    f"targetSdk {android.target_sdk} meets the current requirement; API "
                    f"{upcoming.target_sdk} is expected by {upcoming.effective.isoformat()} "
                    f"(confidence: {upcoming.confidence})."
                ),
                source=self.knowledge.target_sdk_source,
                docs=PLAY_DOCS,
            )
        return self.ok(
            "android.play_policy",
            "Google Play targetSdk policy",
            f"targetSdk {android.target_sdk} meets the API {requirement.target_sdk} requirement",
            docs=PLAY_DOCS,
        )

    @staticmethod
    def _info_status() -> CheckStatus:
        return CheckStatus.PASS

    # -- manifest ----------------------------------------------------------
    def _manifest(self) -> HealthCheck:
        android = self.project.android
        if android.manifest_path is None:
            return self.fail(
                "android.manifest.missing",
                "AndroidManifest.xml",
                "app/src/main/AndroidManifest.xml was not found.",
                severity=Severity.CRITICAL,
                recommendation="Restore the manifest; the app cannot build without it.",
            )
        exported_note = next(
            (note for note in self.project.warnings if "android:exported" in note), None
        )
        if exported_note:
            return self.fail(
                "android.manifest.exported",
                "AndroidManifest.xml",
                exported_note,
                severity=Severity.CRITICAL,
                recommendation=(
                    "Add android:exported=\"true|false\" to every activity/service/receiver "
                    "that declares an intent-filter (required since Android 12)."
                ),
                docs="https://developer.android.com/guide/topics/manifest/activity-element#exported",
            )
        return self.ok(
            "android.manifest",
            "AndroidManifest.xml",
            f"{android.manifest_path} with {len(android.permissions)} permission(s)",
        )

    def _permissions(self) -> list[HealthCheck]:
        android = self.project.android
        declared = set(android.permissions)
        #: permission -> the modules that need it. A permission can be required
        #: by more than one module, and naming all of them is what tells a
        #: developer whether removing one module removes the requirement.
        missing: dict[str, list[str]] = {}
        sources: dict[str, str] = {}
        for dependency in self.project.dependencies:
            requirement = self.knowledge.permission_for(dependency.name)
            if requirement is None or not requirement.required:
                continue
            for permission in requirement.android_permissions:
                if permission in declared:
                    continue
                missing.setdefault(permission, []).append(dependency.name)
                if requirement.source and permission not in sources:
                    sources[permission] = requirement.source
        if not missing:
            return [
                self.ok(
                    "android.permissions",
                    "Permissions match installed modules",
                    f"{len(declared)} permission(s) declared",
                )
            ]

        manifest = android.manifest_path or "android/app/src/main/AndroidManifest.xml"
        return [
            self.warn(
                "android.permissions.missing",
                "Permissions match installed modules",
                _missing_detail(missing),
                severity=Severity.HIGH,
                recommendation=(
                    f"Add {'this' if len(missing) == 1 else 'these'} to {manifest}, as "
                    "children of <manifest> and before <application>:"
                ),
                fix=_manifest_lines(missing),
                evidence={name: f"needed by {', '.join(modules)}" for name, modules in sorted(missing.items())}
                | {f"docs:{name}": url for name, url in sorted(sources.items())},
                docs="https://developer.android.com/guide/topics/manifest/uses-permission-element",
            )
        ]


def _missing_detail(missing: dict[str, list[str]]) -> str:
    """Name what is missing and who wants it.

    One permission fits on the line, so it is named there. Several would make a
    paragraph nobody reads, so the count leads and the paste-ready block below
    carries the names - each annotated with the module that needs it.
    """
    if len(missing) == 1:
        name, modules = next(iter(missing.items()))
        return f"{name} is required by {', '.join(modules)} but is not declared."
    return (
        f"{len(missing)} permissions required by installed modules are not declared: "
        + summarize(sorted(missing), limit=4)
        + "."
    )


def _manifest_lines(missing: dict[str, list[str]]) -> list[str]:
    """The XML to paste, annotated so the manifest records *why* each is there."""
    lines: list[str] = []
    for name in sorted(missing):
        lines.append(f"<!-- {', '.join(missing[name])} -->")
        lines.append(f'<uses-permission android:name="{name}" />')
    return lines
