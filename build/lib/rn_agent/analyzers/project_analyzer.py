"""Project-level hygiene: lockfiles, git, toolchain, agent state."""

from __future__ import annotations

from ..models.health import Category, HealthCheck, Severity
from .base import Analyzer


class ProjectAnalyzer(Analyzer):
    category = Category.PROJECT
    title = "Project"

    def run(self) -> list[HealthCheck]:
        checks: list[HealthCheck] = [
            self._lockfiles(),
            self._node_modules(),
            self._git(),
            self._agent_dir_ignored(),
            self._platforms(),
        ]
        checks.extend(self._node_engine())
        return checks

    # -- checks ------------------------------------------------------------
    def _lockfiles(self) -> HealthCheck:
        manager = self.project.package_manager
        found = manager.lockfiles_found
        if not found:
            return self.warn(
                "project.lockfile.missing",
                "Dependency lockfile",
                "No lockfile found; installs are not reproducible.",
                severity=Severity.MEDIUM,
                recommendation=f"Commit a lockfile (`{manager.install_command}`).",
            )
        if len(found) > 1:
            return self.fail(
                "project.lockfile.conflict",
                "Dependency lockfile",
                f"Multiple lockfiles present: {', '.join(found)}.",
                severity=Severity.HIGH,
                recommendation=(
                    f"Keep only the lockfile for {manager.name} and delete the others; "
                    "mixed lockfiles produce different dependency trees per machine."
                ),
                evidence={"lockfiles": ", ".join(found), "package_manager": manager.name},
            )
        return self.ok(
            "project.lockfile",
            "Dependency lockfile",
            f"{found[0]} ({manager.name})",
            evidence={"lockfile": found[0]},
        )

    def _node_modules(self) -> HealthCheck:
        if self.project.node_modules_present:
            return self.ok(
                "project.node_modules",
                "Dependencies installed",
                "node_modules is present, so versions are exact.",
            )
        return self.warn(
            "project.node_modules.missing",
            "Dependencies installed",
            "node_modules is missing: versions come from package.json ranges only.",
            severity=Severity.LOW,
            recommendation=f"Run `{self.project.package_manager.install_command}`.",
        )

    def _git(self) -> HealthCheck:
        git = self.project.git
        if not git.repository:
            return self.warn(
                "project.git.missing",
                "Git repository",
                "Not a git repository; rn-agent cannot offer rollback for edits.",
                severity=Severity.MEDIUM,
                recommendation="Run `git init` and commit before using fix/feature/migrate.",
            )
        if git.dirty:
            return self.warn(
                "project.git.dirty",
                "Git working tree",
                (
                    f"{git.modified} modified, {git.staged} staged, {git.untracked} untracked "
                    f"file(s) on {git.branch or 'HEAD'}."
                ),
                severity=Severity.LOW,
                recommendation="Commit or stash before running modifying commands.",
                evidence={
                    "branch": git.branch or "detached",
                    "modified": str(git.modified),
                    "untracked": str(git.untracked),
                },
            )
        return self.ok(
            "project.git",
            "Git working tree",
            f"clean on {git.branch or 'HEAD'}",
            evidence={"branch": git.branch or "detached"},
        )

    def _agent_dir_ignored(self) -> HealthCheck:
        git = self.project.git
        if not git.repository:
            return self.skip(
                "project.agent_dir",
                "Agent cache ignored by git",
                "No git repository to check.",
            )
        if git.ignores_agent_dir:
            return self.ok(
                "project.agent_dir",
                "Agent cache ignored by git",
                ".rn-agent/cache is git-ignored.",
            )
        return self.warn(
            "project.agent_dir",
            "Agent cache ignored by git",
            ".rn-agent/cache and .rn-agent/logs are not git-ignored.",
            severity=Severity.LOW,
            recommendation="Add `.rn-agent/cache/`, `.rn-agent/logs/` and `.rn-agent/knowledge/` to .gitignore.",
        )

    def _platforms(self) -> HealthCheck:
        android = self.project.android.present
        ios = self.project.ios.present
        if android and ios:
            return self.ok("project.platforms", "Native platforms", "android/ and ios/ present")
        if self.project.react_native.expo_managed:
            return self.ok(
                "project.platforms",
                "Native platforms",
                "Expo managed project (no native folders checked in).",
            )
        missing = ", ".join(name for name, present in (("android", android), ("ios", ios)) if not present)
        return self.warn(
            "project.platforms",
            "Native platforms",
            f"Missing native folder(s): {missing}.",
            severity=Severity.LOW,
            recommendation="Run `npx expo prebuild` or restore the native folders if this is a bare project.",
        )

    def _node_engine(self) -> list[HealthCheck]:
        """Compare the running Node against react-native's declared engine."""
        from ..utils.semver import coerce, satisfies

        node_version = self.project.tooling.node
        requirement = self.project.react_native.node_requirement
        if node_version is None:
            return [
                self.skip(
                    "project.node.version",
                    "Node.js version",
                    "Node.js was not found on PATH.",
                )
            ]
        if requirement is None:
            parsed = coerce(self.project.rn_version)
            fallback = self.knowledge.compat_for_series(parsed.series if parsed else None)
            if fallback is None or fallback.node is None:
                return [
                    self.ok(
                        "project.node.version",
                        "Node.js version",
                        f"Node {node_version} (no engine requirement available to compare).",
                    )
                ]
            verdict = satisfies(node_version, fallback.node)
            if verdict is False:
                return [
                    self.warn(
                        "project.node.version",
                        "Node.js version",
                        f"Node {node_version} is below the {fallback.node} expected by React Native {fallback.series}.",
                        severity=Severity.MEDIUM,
                        recommendation=f"Install Node {fallback.node}.",
                        source="offline compatibility table",
                        evidence={"confidence": fallback.confidence},
                    )
                ]
            return [
                self.ok(
                    "project.node.version",
                    "Node.js version",
                    f"Node {node_version} satisfies {fallback.node} (offline table).",
                )
            ]

        verdict = satisfies(node_version, requirement)
        if verdict is False:
            return [
                self.fail(
                    "project.node.version",
                    "Node.js version",
                    f"Node {node_version} does not satisfy react-native's engine requirement {requirement}.",
                    severity=Severity.HIGH,
                    recommendation=f"Install a Node version matching {requirement} (nvm/fnm/volta).",
                    source="node_modules/react-native/package.json (engines.node)",
                    evidence={"node": node_version, "required": requirement},
                )
            ]
        if verdict is None:
            return [
                self.skip(
                    "project.node.version",
                    "Node.js version",
                    f"Cannot compare Node {node_version} against {requirement}.",
                )
            ]
        return [
            self.ok(
                "project.node.version",
                "Node.js version",
                f"Node {node_version} satisfies {requirement}.",
                source="node_modules/react-native/package.json (engines.node)",
            )
        ]
