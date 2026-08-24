"""React Native checks: version, React pairing, Hermes, New Architecture, configs.

Fact hierarchy used throughout:

1. ``node_modules/react-native/package.json`` - ``peerDependencies``/``engines``
   are the project's own truth and need no table.
2. the curated offline table, clearly labelled, when dependencies are absent.
3. skip, when neither is available.
"""

from __future__ import annotations

from ..models.health import Category, HealthCheck, Severity
from ..utils.semver import coerce, satisfies
from .base import Analyzer

UPGRADE_DOCS = "https://reactnative.dev/docs/upgrading"


class ReactNativeAnalyzer(Analyzer):
    category = Category.REACT_NATIVE
    title = "React Native"

    def run(self) -> list[HealthCheck]:
        checks = [
            self._version(),
            self._react_pairing(),
            self._types_react(),
            self._hermes(),
            self._new_architecture(),
            self._metro(),
            self._babel(),
        ]
        checks.extend(self._version_consistency())
        return checks

    # -- version -----------------------------------------------------------
    def _version(self) -> HealthCheck:
        rn = self.project.react_native
        if rn.version is None:
            return self.fail(
                "rn.version.unknown",
                "React Native version",
                "Could not determine the React Native version.",
                severity=Severity.HIGH,
                recommendation="Check the react-native entry in package.json.",
            )
        parsed = coerce(rn.version)
        detail = f"{rn.version}"
        if rn.declared_range and rn.installed_version and rn.declared_range != rn.installed_version:
            detail += f" (installed) / {rn.declared_range} (declared)"
        return self.ok(
            "rn.version",
            "React Native version",
            detail,
            evidence={
                "installed": rn.installed_version or "-",
                "declared": rn.declared_range or "-",
                "series": parsed.series if parsed else "-",
            },
            docs=UPGRADE_DOCS,
        )

    def _version_consistency(self) -> list[HealthCheck]:
        """Installed version must satisfy the declared range."""
        rn = self.project.react_native
        if not rn.installed_version or not rn.declared_range:
            return []
        verdict = satisfies(rn.installed_version, rn.declared_range)
        if verdict is False:
            return [
                self.fail(
                    "rn.version.drift",
                    "React Native install matches package.json",
                    f"Installed {rn.installed_version} does not satisfy declared {rn.declared_range}.",
                    severity=Severity.HIGH,
                    recommendation=f"Run `{self.project.package_manager.install_command}` to resync node_modules.",
                    evidence={"installed": rn.installed_version, "declared": rn.declared_range},
                )
            ]
        return []

    # -- React pairing -----------------------------------------------------
    def _react_pairing(self) -> HealthCheck:
        rn = self.project.react_native
        react = rn.react_version or (str(coerce(rn.react_declared_range)) if coerce(rn.react_declared_range) else None)
        if react is None:
            return self.fail(
                "rn.react.missing",
                "React version",
                "React is not declared or installed.",
                severity=Severity.CRITICAL,
                recommendation="Add the react version required by your React Native release.",
            )

        if rn.react_requirement:
            verdict = satisfies(react, rn.react_requirement)
            source = "node_modules/react-native/package.json (peerDependencies.react)"
            if verdict is False:
                return self.fail(
                    "rn.react.mismatch",
                    "React / React Native compatibility",
                    f"React {react} does not satisfy react-native's peer requirement {rn.react_requirement}.",
                    severity=Severity.CRITICAL,
                    recommendation=f"Install react@{rn.react_requirement}.",
                    source=source,
                    evidence={"react": react, "required": rn.react_requirement},
                    docs=UPGRADE_DOCS,
                )
            if verdict is None:
                return self.skip(
                    "rn.react.mismatch",
                    "React / React Native compatibility",
                    f"Cannot compare React {react} with {rn.react_requirement}.",
                )
            return self.ok(
                "rn.react",
                "React / React Native compatibility",
                f"React {react} satisfies {rn.react_requirement}.",
                source=source,
            )

        # No installed metadata: fall back to the labelled offline table and
        # only assert a problem when the *major* version disagrees.
        parsed_rn = coerce(self.project.rn_version)
        entry = self.knowledge.compat_for_series(parsed_rn.series if parsed_rn else None)
        if entry is None or entry.react is None:
            return self.skip(
                "rn.react",
                "React / React Native compatibility",
                f"No compatibility data for React Native {self.project.rn_version}; install dependencies to verify.",
            )
        expected = coerce(entry.react)
        actual = coerce(react)
        if expected and actual and expected.major != actual.major:
            return self.fail(
                "rn.react.mismatch",
                "React / React Native compatibility",
                f"React {react} looks wrong for React Native {entry.series}, which expects React {entry.react}.",
                severity=Severity.HIGH,
                recommendation=f"Install react@{entry.react} (verify with the Upgrade Helper).",
                source="offline compatibility table",
                evidence={"confidence": entry.confidence},
                docs=UPGRADE_DOCS,
            )
        return self.ok(
            "rn.react",
            "React / React Native compatibility",
            f"React {react} matches the expected major for RN {entry.series} (offline table).",
            source="offline compatibility table",
        )

    def _types_react(self) -> HealthCheck:
        rn = self.project.react_native
        if not rn.typescript:
            return self.skip("rn.types_react", "@types/react version", "Project is not TypeScript.")
        if rn.types_react_version is None:
            return self.skip(
                "rn.types_react",
                "@types/react version",
                "@types/react is not installed.",
            )
        if rn.types_react_requirement:
            verdict = satisfies(rn.types_react_version, rn.types_react_requirement)
            if verdict is False:
                return self.warn(
                    "rn.types_react",
                    "@types/react version",
                    f"@types/react {rn.types_react_version} does not satisfy {rn.types_react_requirement}.",
                    severity=Severity.MEDIUM,
                    recommendation=f"Install @types/react@{rn.types_react_requirement}.",
                    source="node_modules/react-native/package.json (peerDependencies)",
                )
        react = coerce(rn.react_version)
        types = coerce(rn.types_react_version)
        if react and types and react.major != types.major:
            return self.warn(
                "rn.types_react.major",
                "@types/react version",
                f"@types/react {rn.types_react_version} does not match React {rn.react_version}.",
                severity=Severity.MEDIUM,
                recommendation=f"Install @types/react@^{react.major}.",
            )
        return self.ok(
            "rn.types_react",
            "@types/react version",
            f"@types/react {rn.types_react_version}",
        )

    # -- runtime -----------------------------------------------------------
    def _hermes(self) -> HealthCheck:
        rn = self.project.react_native
        android = self.project.android
        if rn.hermes_enabled is None and not android.present:
            return self.skip("rn.hermes", "Hermes engine", "No android/gradle.properties to read.")
        enabled = rn.hermes_enabled
        if enabled is None:
            return self.skip(
                "rn.hermes",
                "Hermes engine",
                "hermesEnabled is not set in android/gradle.properties (template default applies).",
            )
        if enabled:
            return self.ok("rn.hermes", "Hermes engine", "enabled")
        return self.warn(
            "rn.hermes",
            "Hermes engine",
            "Hermes is disabled (hermesEnabled=false).",
            severity=Severity.LOW,
            recommendation=(
                "Hermes is the default engine and improves startup and memory. "
                "Re-enable it unless you depend on JSC behaviour."
            ),
            evidence={"hermesEnabled": "false"},
        )

    def _new_architecture(self) -> HealthCheck:
        android_flag = self.project.android.new_architecture
        ios_flag = self.project.ios.new_architecture
        rn = self.project.react_native.new_architecture
        if android_flag is None and ios_flag is None and rn is None:
            return self.skip(
                "rn.new_arch",
                "New Architecture",
                "No newArchEnabled/RCT_NEW_ARCH_ENABLED flag found.",
            )
        if (
            android_flag is not None
            and ios_flag is not None
            and android_flag != ios_flag
        ):
            return self.fail(
                "rn.new_arch.mismatch",
                "New Architecture",
                f"Android newArchEnabled={android_flag} but iOS RCT_NEW_ARCH_ENABLED={ios_flag}.",
                severity=Severity.HIGH,
                recommendation="Enable the New Architecture on both platforms or neither.",
                evidence={"android": str(android_flag), "ios": str(ios_flag)},
            )
        state = android_flag if android_flag is not None else (ios_flag if ios_flag is not None else rn)
        return self.ok(
            "rn.new_arch",
            "New Architecture",
            "enabled" if state else "disabled (old architecture)",
            evidence={"android": str(android_flag), "ios": str(ios_flag)},
        )

    # -- build config ------------------------------------------------------
    def _metro(self) -> HealthCheck:
        rn = self.project.react_native
        if rn.metro_config is None:
            return self.warn(
                "rn.metro.missing",
                "Metro configuration",
                "No metro.config.js found.",
                severity=Severity.MEDIUM,
                recommendation="Add metro.config.js from the React Native template for your version.",
            )
        text = (self.root / rn.metro_config).read_text(encoding="utf-8", errors="replace")
        if "@react-native/metro-config" in text or "getDefaultConfig" in text:
            return self.ok("rn.metro", "Metro configuration", rn.metro_config)
        return self.warn(
            "rn.metro.custom",
            "Metro configuration",
            f"{rn.metro_config} does not extend @react-native/metro-config.",
            severity=Severity.LOW,
            recommendation="Extend the official preset so upgrades keep working.",
        )

    def _babel(self) -> HealthCheck:
        rn = self.project.react_native
        if rn.babel_config is None:
            return self.warn(
                "rn.babel.missing",
                "Babel configuration",
                "No babel.config.js found.",
                severity=Severity.MEDIUM,
                recommendation="Add babel.config.js with the @react-native/babel-preset preset.",
            )
        text = (self.root / rn.babel_config).read_text(encoding="utf-8", errors="replace")
        if "metro-react-native-babel-preset" in text:
            return self.fail(
                "rn.babel.legacy_preset",
                "Babel configuration",
                f"{rn.babel_config} uses metro-react-native-babel-preset, replaced in React Native 0.73.",
                severity=Severity.HIGH,
                recommendation="Switch the preset to @react-native/babel-preset.",
                docs=UPGRADE_DOCS,
            )
        reanimated = self.project.has_dependency("react-native-reanimated")
        if reanimated and "reanimated" not in text and "worklets" not in text:
            return self.fail(
                "rn.babel.reanimated",
                "Babel configuration",
                "react-native-reanimated is installed but its Babel plugin is not configured.",
                severity=Severity.HIGH,
                recommendation=(
                    "Add the Reanimated plugin (v3: 'react-native-reanimated/plugin', "
                    "v4: 'react-native-worklets/plugin') as the last entry in babel.config.js."
                ),
                evidence={"config": rn.babel_config},
            )
        return self.ok("rn.babel", "Babel configuration", rn.babel_config)
