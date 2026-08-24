"""The validator must run the project's own tools - and be honest when it cannot.

The distinction under test everywhere here: ``SKIP`` (could not run, with the
reason) is never allowed to look like ``PASS`` (ran and succeeded), because only
the second one is evidence that a change is safe.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rn_agent.models.validation import StepStatus, ValidationReport, ValidationStep
from rn_agent.project.scanner import ProjectScanner
from rn_agent.validation.runner import STEP_NAMES, ProjectValidator

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the fake tool scripts are POSIX shell"
)


def fake_bin(root: Path, name: str, *, exit_code: int = 0, output: str = "") -> Path:
    """A stand-in for a locally installed node tool."""
    target = root / "node_modules" / ".bin" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f'#!/bin/sh\n{f"echo {output!r}" if output else ""}\nexit {exit_code}\n', encoding="utf-8"
    )
    target.chmod(0o755)
    return target


def validator(builder, **kwargs) -> ProjectValidator:
    context = builder.context(**kwargs)
    scanner = ProjectScanner(
        context.detected, context.paths, context.runner, knowledge=context.knowledge
    )
    context.set_project(scanner.scan(probe_tools=False, source_stats=context.walker.stats()))
    return ProjectValidator(context)


# ---------------------------------------------------------------------------
# report semantics
# ---------------------------------------------------------------------------
def test_all_skipped_is_not_a_proof():
    report = ValidationReport(
        steps=[ValidationStep(name="tests", status=StepStatus.SKIP, detail="no tests")]
    )

    assert report.ok is True
    assert report.proved is False


def test_one_failure_makes_the_report_fail():
    report = ValidationReport(
        steps=[
            ValidationStep(name="typecheck", status=StepStatus.PASS),
            ValidationStep(
                name="tests",
                status=StepStatus.FAIL,
                command="jest",
                output_tail="FAIL src/App.test.tsx",
            ),
        ]
    )

    assert report.ok is False
    assert report.proved is False
    assert report.counts() == {"steps": 2, "passed": 1, "failed": 1, "skipped": 0}
    assert "jest" in report.failure_text()
    assert "FAIL src/App.test.tsx" in report.failure_text()


# ---------------------------------------------------------------------------
# skipping honestly
# ---------------------------------------------------------------------------
def test_typecheck_skips_without_local_typescript(project):
    step = validator(project).typecheck()

    assert step.status is StepStatus.SKIP
    assert "typescript is not installed" in step.detail


def test_typecheck_skips_without_a_tsconfig(project):
    fake_bin(project.root, "tsc")
    (project.root / "tsconfig.json").unlink()

    step = validator(project).typecheck()

    assert step.status is StepStatus.SKIP
    assert "tsconfig" in step.detail


def test_tests_skip_without_jest_or_a_script(project):
    project.write_package_json(scripts={"start": "react-native start"})

    step = validator(project).unit_tests()

    assert step.status is StepStatus.SKIP
    assert "test" in step.detail


def test_pods_skip_without_a_podfile(project, tmp_path):
    (project.root / "ios" / "Podfile").unlink()

    step = validator(project).pod_install()

    assert step.status is StepStatus.SKIP
    assert "Podfile" in step.detail


def test_android_skips_without_a_gradle_wrapper(project):
    step = validator(project).android_build()

    assert step.status is StepStatus.SKIP
    assert "gradlew" in step.detail


def test_ios_skips_when_the_platform_is_absent(project, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)

    step = validator(project).ios_build()

    assert step.status is StepStatus.SKIP


# ---------------------------------------------------------------------------
# running the real tools
# ---------------------------------------------------------------------------
def test_typecheck_passes_when_tsc_exits_zero(project):
    fake_bin(project.root, "tsc")

    step = validator(project).typecheck()

    assert step.status is StepStatus.PASS
    assert "tsc" in step.command
    assert "--noEmit" in step.command


def test_typecheck_fails_and_keeps_the_output(project):
    fake_bin(project.root, "tsc", exit_code=2, output="src/App.tsx(3,5): error TS2322")

    step = validator(project).typecheck()

    assert step.status is StepStatus.FAIL
    assert "exited with code 2" in step.detail
    assert "error TS2322" in step.output_tail


def test_tests_run_the_local_jest_with_the_named_files(project):
    fake_bin(project.root, "jest", output="PASS")

    step = validator(project).unit_tests(["src/App.test.tsx"])

    assert step.status is StepStatus.PASS
    assert step.command.endswith("src/App.test.tsx")


def test_lint_failure_is_reported_without_stopping_the_run(project):
    fake_bin(project.root, "eslint", exit_code=1, output="src/App.tsx:1:1: oops")

    report = validator(project).run(["lint"])

    assert report.ok is False
    assert report.failures[0].name == "lint"


def test_android_build_uses_the_project_wrapper(project):
    wrapper = project.root / "android" / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)

    step = validator(project).android_build()

    assert step.status is StepStatus.PASS
    assert "assembleDebug" in step.command


# ---------------------------------------------------------------------------
# ordering, selection, dry-run
# ---------------------------------------------------------------------------
def test_run_executes_the_named_steps_in_canonical_order(project):
    fake_bin(project.root, "tsc")
    fake_bin(project.root, "eslint")
    fake_bin(project.root, "jest")

    report = validator(project).run(["tests", "typecheck", "lint"])

    assert [step.name for step in report.steps] == ["typecheck", "lint", "tests"]
    assert report.proved is True


def test_run_ignores_unknown_step_names(project):
    report = validator(project).run(["nonsense"])

    assert report.steps == []


def test_dry_run_executes_nothing(project):
    fake_bin(project.root, "tsc")

    report = validator(project, dry_run=True).run(["typecheck"])

    step = report.step("typecheck")
    assert step is not None
    assert step.status is StepStatus.SKIP
    assert "dry run" in step.detail


def test_every_declared_step_is_reachable(project):
    """STEP_NAMES is the CLI's vocabulary; each name must map to a method."""
    report = validator(project, dry_run=True).run(list(STEP_NAMES))

    assert [step.name for step in report.steps] == list(STEP_NAMES)
