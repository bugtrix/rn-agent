"""Running the project's own checks, and reporting them honestly.

Nothing here is a proxy for the real thing: ``typecheck`` runs the project's own
``tsc``, ``tests`` runs the project's own test script, ``android`` runs the
project's own Gradle wrapper. Tools are taken from ``node_modules/.bin`` rather
than fetched with ``npx``, for the same reason the scanner reads
``node_modules``: what is installed here is the truth, and no command of ours
should install something behind the developer's back.

A step that cannot run says why (``SKIP``). That distinction matters: "the tests
pass" and "there are no tests" are different facts, and only one of them is
evidence that a change is safe.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..core.context import AgentContext
from ..models.validation import StepStatus, ValidationReport, ValidationStep
from ..runner.command_runner import CommandResult
from ..utils.redaction import redact

#: Step names accepted by :meth:`ProjectValidator.run`, in execution order.
STEP_NAMES: Final[tuple[str, ...]] = (
    "install",
    "pods",
    "typecheck",
    "lint",
    "tests",
    "android",
    "ios",
)

#: Generous, because these are real builds - and bounded, because a hung build
#: must not hang the agent.
TIMEOUTS: Final[dict[str, float]] = {
    "install": 900.0,
    "pods": 900.0,
    "typecheck": 600.0,
    "lint": 600.0,
    "tests": 1200.0,
    "android": 2400.0,
    "ios": 2400.0,
}

SCRIPT_RUNNERS: Final[dict[str, tuple[str, ...]]] = {
    "npm": ("npm", "run"),
    "yarn": ("yarn",),
    "pnpm": ("pnpm", "run"),
    "bun": ("bun", "run"),
}


@dataclass(slots=True)
class ProjectValidator:
    """Runs install/typecheck/lint/test/build steps for one project."""

    context: AgentContext

    # -- entry point -------------------------------------------------------
    def run(
        self, steps: Sequence[str], *, test_paths: Sequence[str] = ()
    ) -> ValidationReport:
        """Run the named steps in canonical order, skipping unknown names.

        ``test_paths`` narrows the ``tests`` step to specific files, which is how
        ``rn-agent test`` proves the tests it just generated without waiting for
        the whole suite.
        """
        wanted = [name for name in STEP_NAMES if name in set(steps)]
        methods = {
            "install": self.install,
            "pods": self.pod_install,
            "typecheck": self.typecheck,
            "lint": self.lint,
            "tests": lambda: self.unit_tests(test_paths),
            "android": self.android_build,
            "ios": self.ios_build,
        }
        return ValidationReport(steps=[methods[name]() for name in wanted])

    # -- steps -------------------------------------------------------------
    def install(self) -> ValidationStep:
        manager = self.context.project.package_manager
        executable = manager.name if manager.name in SCRIPT_RUNNERS else "npm"
        if not self.context.runner.available(executable):
            return _skip("install", f"{executable} is not on PATH")
        argv = [executable, "install"]
        return self._execute("install", argv)

    def pod_install(self) -> ValidationStep:
        ios = self.context.root / "ios"
        if not (ios / "Podfile").is_file():
            return _skip("pods", "no ios/Podfile in this project")
        if self.context.runner.available("pod"):
            argv = ["pod", "install"]
        elif (self.context.root / "Gemfile").is_file() and self.context.runner.available("bundle"):
            argv = ["bundle", "exec", "pod", "install"]
        else:
            return _skip("pods", "CocoaPods is not installed")
        return self._execute("pods", argv, cwd=ios)

    def typecheck(self) -> ValidationStep:
        tsc = self._local_bin("tsc")
        if tsc is None:
            return _skip("typecheck", "typescript is not installed in node_modules")
        if not (self.context.root / "tsconfig.json").is_file():
            return _skip("typecheck", "no tsconfig.json")
        return self._execute("typecheck", [str(tsc), "--noEmit"])

    def lint(self) -> ValidationStep:
        eslint = self._local_bin("eslint")
        if eslint is None:
            return _skip("lint", "eslint is not installed in node_modules")
        return self._execute("lint", [str(eslint), ".", "--format", "unix"])

    def unit_tests(self, paths: Sequence[str] = ()) -> ValidationStep:
        """Run the project's tests - the named files, or the whole suite."""
        jest = self._local_bin("jest")
        if jest is not None:
            argv = [str(jest), *paths]
        else:
            script = self._script_argv("test")
            if script is None:
                return _skip("tests", "no jest binary and no `test` script in package.json")
            argv = [*script, *(["--", *paths] if paths else [])]
        return self._execute("tests", argv)

    def android_build(self) -> ValidationStep:
        android = self.context.root / "android"
        wrapper = android / ("gradlew.bat" if _is_windows() else "gradlew")
        if not wrapper.is_file():
            return _skip("android", "no android/gradlew in this project")
        argv = [str(wrapper), "assembleDebug", "--console=plain"]
        return self._execute("android", argv, cwd=android)

    def ios_build(self) -> ValidationStep:
        info = self.context.project.ios
        ios = self.context.root / "ios"
        if not info.present:
            return _skip("ios", "no ios/ directory in this project")
        if not self.context.runner.available("xcodebuild"):
            return _skip("ios", "xcodebuild is not available (not a macOS machine?)")
        scheme = info.project_name
        if not scheme:
            return _skip("ios", "could not determine the Xcode scheme")
        if info.workspace:
            container = ["-workspace", Path(info.workspace).name]
        elif info.xcodeproj:
            container = ["-project", Path(info.xcodeproj).name]
        else:
            return _skip("ios", "no .xcworkspace or .xcodeproj found")
        argv = [
            "xcodebuild",
            *container,
            "-scheme",
            scheme,
            "-configuration",
            "Debug",
            "-sdk",
            "iphonesimulator",
            "-derivedDataPath",
            "build",
            "build",
            "-quiet",
        ]
        return self._execute("ios", argv, cwd=ios)

    # -- internals ---------------------------------------------------------
    def _execute(
        self, name: str, argv: Sequence[str], *, cwd: Path | None = None
    ) -> ValidationStep:
        result = self.context.runner.run(
            list(argv), cwd=cwd, timeout=TIMEOUTS.get(name, 600.0)
        )
        return _from_result(name, result)

    def _local_bin(self, name: str) -> Path | None:
        candidate = self.context.root / "node_modules" / ".bin" / name
        return candidate if candidate.exists() else None

    def _script_argv(self, script: str) -> list[str] | None:
        project = self.context.project
        if script not in project.scripts:
            return None
        manager = project.package_manager.name
        prefix = SCRIPT_RUNNERS.get(manager, SCRIPT_RUNNERS["npm"])
        if not self.context.runner.available(prefix[0]):
            return None
        return [*prefix, script]


def _from_result(name: str, result: CommandResult) -> ValidationStep:
    """Translate one command result into a step, without losing the reason."""
    command = result.command
    if result.skipped:  # dry-run: the runner reports intent, not an outcome
        return ValidationStep(
            name=name,
            status=StepStatus.SKIP,
            command=command,
            detail="dry run: not executed",
        )
    if result.executable_missing:
        return ValidationStep(
            name=name,
            status=StepStatus.SKIP,
            command=command,
            detail=f"{result.argv[0]} not found",
        )
    if result.ok:
        return ValidationStep(
            name=name,
            status=StepStatus.PASS,
            command=command,
            detail="passed",
            duration_ms=result.duration_ms,
        )
    detail = (
        f"timed out after {TIMEOUTS.get(name, 600.0):g}s"
        if result.timed_out
        else f"exited with code {result.returncode}"
    )
    return ValidationStep(
        name=name,
        status=StepStatus.FAIL,
        command=command,
        detail=detail,
        duration_ms=result.duration_ms,
        output_tail=redact(result.tail(25)),
    )


def _skip(name: str, reason: str) -> ValidationStep:
    return ValidationStep(name=name, status=StepStatus.SKIP, detail=reason)


def _is_windows() -> bool:
    return os.name == "nt"
