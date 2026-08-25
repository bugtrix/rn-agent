"""JavaScript/TypeScript checks: types, lint, duplicate and deprecated deps.

The peer-dependency check is the valuable one here: it reads each installed
package's own ``peerDependencies`` and verifies them against what is actually
installed - the same thing npm warns about at install time, but visible on
demand and without network access.
"""

from __future__ import annotations

from pathlib import Path

from ..models.health import Category, HealthCheck, Severity
from ..utils.io import read_json
from ..utils.semver import coerce, is_undecidable_range, satisfies
from .base import Analyzer, summarize

MAX_REPORTED = 6


class JavaScriptAnalyzer(Analyzer):
    category = Category.JAVASCRIPT
    title = "JavaScript"

    def run(self) -> list[HealthCheck]:
        checks = [
            self._typescript(),
            self._tsconfig(),
            self._eslint(),
            self._duplicate_react(),
        ]
        checks.extend(self._peer_dependencies())
        checks.extend(self._deprecated())
        checks.extend(self._duplicate_declarations())
        if self.data.deep:
            checks.extend(self._deep_checks())
        return checks

    # -- language ----------------------------------------------------------
    def _typescript(self) -> HealthCheck:
        rn = self.project.react_native
        if not rn.typescript:
            return self.warn(
                "js.typescript.missing",
                "TypeScript",
                "Project is JavaScript only.",
                severity=Severity.LOW,
                recommendation="TypeScript catches most React Native prop and navigation errors at build time.",
            )
        return self.ok(
            "js.typescript",
            "TypeScript",
            f"typescript {rn.typescript_version or 'installed'}",
        )

    def _tsconfig(self) -> HealthCheck:
        rn = self.project.react_native
        if not rn.typescript:
            return self.skip("js.tsconfig", "tsconfig.json", "Project is not TypeScript.")
        if rn.tsconfig is None:
            return self.fail(
                "js.tsconfig.missing",
                "tsconfig.json",
                "TypeScript is installed but tsconfig.json is missing.",
                severity=Severity.HIGH,
                recommendation='Create tsconfig.json extending "@react-native/typescript-config".',
            )
        payload = read_json(self.root / rn.tsconfig, default={})
        if not isinstance(payload, dict):
            return self.warn(
                "js.tsconfig.unreadable",
                "tsconfig.json",
                "tsconfig.json could not be parsed (comments or trailing commas are allowed by tsc).",
                severity=Severity.LOW,
            )
        extends = payload.get("extends")
        options = payload.get("compilerOptions") if isinstance(payload.get("compilerOptions"), dict) else {}
        strict = bool((options or {}).get("strict", False))
        if not extends:
            return self.warn(
                "js.tsconfig.extends",
                "tsconfig.json",
                "tsconfig.json does not extend the React Native preset.",
                severity=Severity.MEDIUM,
                recommendation='Set "extends": "@react-native/typescript-config".',
            )
        if not strict:
            return self.warn(
                "js.tsconfig.strict",
                "tsconfig.json",
                f'strict mode is off (extends "{extends}").',
                severity=Severity.LOW,
                recommendation='Enable "strict": true to catch null/undefined bugs before runtime.',
            )
        return self.ok("js.tsconfig", "tsconfig.json", f"extends {extends}, strict mode on")

    def _eslint(self) -> HealthCheck:
        candidates = (
            ".eslintrc.js",
            ".eslintrc.json",
            ".eslintrc.cjs",
            ".eslintrc.yml",
            "eslint.config.js",
            "eslint.config.mjs",
            "eslint.config.cjs",
        )
        found = next((name for name in candidates if (self.root / name).is_file()), None)
        has_dependency = self.project.has_dependency("eslint")
        if found and has_dependency:
            return self.ok("js.eslint", "ESLint", found)
        if found and not has_dependency:
            return self.warn(
                "js.eslint.dependency",
                "ESLint",
                f"{found} exists but eslint is not a dependency.",
                severity=Severity.LOW,
                recommendation="Add eslint (and @react-native/eslint-config) to devDependencies.",
            )
        return self.warn(
            "js.eslint.missing",
            "ESLint",
            "No ESLint configuration found.",
            severity=Severity.LOW,
            recommendation="Add @react-native/eslint-config to catch hook and import mistakes.",
        )

    # -- dependency integrity ---------------------------------------------
    def _duplicate_react(self) -> HealthCheck:
        """Nested copies of react/react-native break hooks at runtime."""
        if not self.project.node_modules_present:
            return self.skip(
                "js.duplicate_react",
                "Single React copy",
                "node_modules is not installed.",
            )
        duplicates: list[str] = []
        node_modules = self.root / "node_modules"
        for package in ("react", "react-native"):
            try:
                nested = [
                    str(path.relative_to(self.root))
                    for path in node_modules.glob(f"*/node_modules/{package}/package.json")
                ]
                nested += [
                    str(path.relative_to(self.root))
                    for path in node_modules.glob(f"@*/*/node_modules/{package}/package.json")
                ]
            except OSError:  # pragma: no cover
                nested = []
            duplicates.extend(nested)
        if not duplicates:
            return self.ok(
                "js.duplicate_react", "Single React copy", "one react and react-native tree"
            )

        # A nested copy under @types/* is a types-only artifact: it bloats the
        # tree and confuses tsc, but it is not loaded at runtime, so it does not
        # deserve the same severity as a duplicated runtime copy.
        runtime = [path for path in duplicates if not path.startswith("node_modules/@types/")]
        offenders = sorted(runtime or duplicates)
        owner = offenders[0].split("/node_modules/")[0].removeprefix("node_modules/")
        if runtime:
            return self.fail(
                "js.duplicate_react",
                "Single React copy",
                f"{len(runtime)} nested runtime copy/copies of react or react-native (via {owner}).",
                severity=Severity.CRITICAL,
                recommendation=(
                    "Deduplicate with `npm dedupe` / `yarn dedupe`, or pin the version with "
                    "resolutions/overrides. Two runtime React copies break hooks with "
                    "'Invalid hook call'."
                ),
                evidence={"paths": ", ".join(offenders[:MAX_REPORTED])},
            )
        return self.warn(
            "js.duplicate_react.types",
            "Single React copy",
            f"{len(duplicates)} nested copy/copies of react-native under @types/ (via {owner}).",
            severity=Severity.HIGH,
            recommendation=(
                f"Uninstall {owner}: it drags in a second react-native tree that confuses "
                "TypeScript resolution."
            ),
            evidence={"paths": ", ".join(offenders[:MAX_REPORTED])},
        )

    def _peer_dependencies(self) -> list[HealthCheck]:
        if not self.project.node_modules_present:
            return [
                self.skip(
                    "js.peer_dependencies",
                    "Peer dependencies satisfied",
                    "node_modules is not installed; peer requirements cannot be read.",
                )
            ]
        installed = {
            dependency.name: dependency.installed
            for dependency in self.project.dependencies
            if dependency.installed
        }
        conflicts: list[str] = []
        unmet: list[str] = []
        for dependency in self.project.dependencies:
            for peer, requirement in dependency.peer_dependencies.items():
                if is_undecidable_range(requirement):
                    continue
                actual = installed.get(peer)
                if actual is None:
                    if peer in {"react", "react-native"}:
                        unmet.append(f"{dependency.name} needs {peer}@{requirement} (not installed)")
                    continue
                if satisfies(actual, requirement) is False:
                    conflicts.append(f"{dependency.name} needs {peer}@{requirement}, found {actual}")

        checks: list[HealthCheck] = []
        if conflicts:
            checks.append(
                self.warn(
                    "js.peer_dependencies.conflict",
                    "Peer dependencies satisfied",
                    f"{len(conflicts)} unsatisfied peer requirement(s): "
                    f"{summarize(conflicts)}.",
                    severity=Severity.MEDIUM,
                    recommendation="Align these versions before upgrading React Native.",
                    evidence={
                        f"conflict_{index + 1}": item
                        for index, item in enumerate(sorted(conflicts)[:MAX_REPORTED])
                    },
                    source="each package's own peerDependencies",
                )
            )
        else:
            checks.append(
                self.ok(
                    "js.peer_dependencies",
                    "Peer dependencies satisfied",
                    "every installed package's peer requirements are met",
                )
            )
        if unmet:
            checks.append(
                self.fail(
                    "js.peer_dependencies.missing",
                    "Core peer dependencies installed",
                    f"{len(unmet)} package(s) require a core peer that is not installed: "
                    f"{summarize(unmet)}.",
                    severity=Severity.HIGH,
                    recommendation=f"Run `{self.project.package_manager.install_command}`.",
                    evidence={
                        f"missing_{index + 1}": item
                        for index, item in enumerate(sorted(unmet)[:MAX_REPORTED])
                    },
                )
            )
        return checks

    def _deprecated(self) -> list[HealthCheck]:
        rn_version = coerce(self.project.rn_version)
        findings: list[tuple[str, str, str, str | None, str | None]] = []
        for dependency in self.project.dependencies:
            entry = self.knowledge.deprecated_for(dependency.name)
            if entry is None or not entry.applies(rn_version):
                continue
            findings.append(
                (entry.severity, dependency.name, entry.reason, entry.replacement, entry.source)
            )
        if not findings:
            return [
                self.ok(
                    "js.deprecated",
                    "Deprecated packages",
                    "no known deprecated or renamed packages",
                )
            ]
        checks: list[HealthCheck] = []
        for severity, name, reason, replacement, source in findings:
            checks.append(
                self.warn(
                    f"js.deprecated.{name}",
                    f"Deprecated package: {name}",
                    reason,
                    severity=Severity(severity) if severity in Severity._value2member_map_ else Severity.LOW,
                    recommendation=_replacement_advice(name, replacement),
                    source=source,
                    evidence={"package": name},
                )
            )
        return checks

    def _duplicate_declarations(self) -> list[HealthCheck]:
        """The same package in both dependencies and devDependencies."""
        package_json = read_json(self.root / "package.json", default={}) or {}
        prod = set((package_json.get("dependencies") or {}).keys())
        dev = set((package_json.get("devDependencies") or {}).keys())
        overlap = sorted(prod & dev)
        if not overlap:
            return []
        return [
            self.warn(
                "js.duplicate_declaration",
                "Duplicate dependency declarations",
                f"{len(overlap)} package(s) in both dependencies and devDependencies: "
                f"{summarize(overlap, limit=6)}.",
                severity=Severity.LOW,
                recommendation="Keep each package in a single section to avoid version drift.",
                evidence={"packages": ", ".join(overlap[:MAX_REPORTED])},
            )
        ]

    # -- deep (opt-in, runs tools) ----------------------------------------
    def _deep_checks(self) -> list[HealthCheck]:
        checks: list[HealthCheck] = []
        if self.project.react_native.typescript:
            checks.append(self._typecheck())
        checks.append(self._lint_run())
        return checks

    def _typecheck(self) -> HealthCheck:
        tsc = self._local_bin("tsc")
        if tsc is None:
            return self.skip("js.typecheck", "TypeScript compiles", "typescript is not installed.")
        result = self.data.runner.run([str(tsc), "--noEmit"], timeout=600.0, force=True)
        if result.ok:
            return self.ok("js.typecheck", "TypeScript compiles", "tsc --noEmit passed")
        errors = [line for line in result.output.splitlines() if ": error TS" in line]
        return self.fail(
            "js.typecheck",
            "TypeScript compiles",
            f"tsc --noEmit reported {len(errors) or 'some'} error(s).",
            severity=Severity.HIGH,
            recommendation="Fix the type errors, or run `rn-agent review` for a prioritised list.",
            evidence={
                f"error_{index + 1}": line.strip()
                for index, line in enumerate(errors[:MAX_REPORTED])
            },
        )

    def _lint_run(self) -> HealthCheck:
        eslint = self._local_bin("eslint")
        if eslint is None:
            return self.skip("js.lint", "ESLint passes", "eslint is not installed.")
        result = self.data.runner.run([str(eslint), ".", "--format", "unix"], timeout=600.0, force=True)
        if result.ok:
            return self.ok("js.lint", "ESLint passes", "no lint errors")
        lines = [line for line in result.output.splitlines() if ".js" in line or ".ts" in line]
        return self.warn(
            "js.lint",
            "ESLint passes",
            f"eslint reported {len(lines) or 'some'} finding(s).",
            severity=Severity.LOW,
            recommendation="Run your lint script and fix or silence the findings.",
            evidence={
                f"finding_{index + 1}": line.strip()
                for index, line in enumerate(lines[:MAX_REPORTED])
            },
        )

    def _local_bin(self, name: str) -> Path | None:
        candidate = self.root / "node_modules" / ".bin" / name
        return candidate if candidate.exists() else None


def _replacement_advice(package: str, replacement: str | None) -> str | None:
    """Phrase the advice naturally for removals as well as renames."""
    if not replacement:
        return None
    if replacement.lower().startswith("remove"):
        return f"Uninstall {package}."
    return f"Replace {package} with {replacement}."
