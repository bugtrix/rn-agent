"""``rn-agent test`` - generate tests, then make them prove themselves.

Two rules make this trustworthy rather than a code generator:

* a proposal that touches anything but a test file is refused - "write me tests"
  must never turn into a rewrite of the code under test;
* the generated tests are executed, and if they fail they are rolled back. A
  failing generated test is worse than no test, because it teaches the team to
  ignore red.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agents.apply import ApplyOutcome
from ..agents.context_builder import ContextBuilder, PromptContext
from ..agents.engine import AIEngine
from ..agents.prompts import test_messages
from ..agents.rules import ProjectRules, RuleViolation
from ..agents.workflow import EditWorkflow
from ..cli import ui
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..errors import RNAgentError
from ..models.project import ProjectContext
from ..models.proposal import EditRunReport, Proposal, ProposalSet, RefusedEdit
from ..models.validation import ValidationReport
from ..reporting import change_view
from ..utils.io import write_json
from .health import CONTEXT_STALE_SECONDS

#: A path is a test file when it says so. Anything else is production code.
TEST_MARKERS: tuple[str, ...] = (".test.", ".spec.", "/__tests__/")

#: Packages that decide the framework, most specific first.
FRAMEWORK_PACKAGES: tuple[tuple[str, str], ...] = (
    ("@testing-library/react-native", "jest with @testing-library/react-native"),
    ("jest", "jest"),
    ("vitest", "vitest"),
)


def is_test_path(path: str) -> bool:
    posix = f"/{path.replace(chr(92), '/').lstrip('/')}"
    return any(marker in posix for marker in TEST_MARKERS)


@dataclass(slots=True)
class TestAnalysis:
    project: ProjectContext
    selected: PromptContext
    proposals: ProposalSet
    framework: str
    framework_source: str
    conventions: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class TestPlan:
    kept: list[Proposal]
    refused: list[RuleViolation]
    workflow: EditWorkflow


class TestCommand(AgentCommand[TestAnalysis, TestPlan]):
    name = "test"
    description = "Generate tests for your code and run them"
    read_only = False
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        targets: tuple[str, ...] = (),
        framework: str | None = None,
        run_tests: bool = True,
        keep_on_failure: bool = False,
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.targets = targets
        self.framework = framework
        self.run_tests = run_tests
        self.keep_on_failure = keep_on_failure
        self.verbose = verbose
        self.report = EditRunReport(task="test", dry_run=context.dry_run)
        self.outcome: ApplyOutcome | None = None
        self.validation: ValidationReport | None = None

    # -- phases ------------------------------------------------------------
    def analyze(self) -> TestAnalysis:
        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        framework, source = self._framework(project)
        builder = ContextBuilder(self.context)
        selected = (
            builder.select(paths=self.targets)
            if self.targets
            else builder.select(query="component hook screen")
        )
        if not selected:
            raise RNAgentError(
                "no source files were selected to test",
                hint="Name a file or directory: rn-agent test src/screens/HomeScreen.tsx",
            )
        conventions = self._existing_tests()
        engine = AIEngine(self.context)
        proposals = engine.propose(
            test_messages(
                project=project,
                rules=ProjectRules.load(self.context.paths),
                context=selected,
                framework=framework,
                conventions=conventions,
            ),
            task="test",
        )
        return TestAnalysis(
            project=project,
            selected=selected,
            proposals=proposals,
            framework=framework,
            framework_source=source,
            conventions=conventions,
            usage=engine.usage,
        )

    def plan(self, analysis: TestAnalysis) -> TestPlan:
        workflow = EditWorkflow(
            self.context,
            rules=ProjectRules.load(self.context.paths),
            task="test",
            keep_on_failure=self.keep_on_failure,
        )
        # Refuse production-code edits before the rules even see them: this
        # command's contract is "add tests", not "change my app".
        off_target: list[RuleViolation] = []
        narrowed: list[Proposal] = []
        for proposal in analysis.proposals.proposals:
            allowed = [edit for edit in proposal.usable_edits if is_test_path(edit.path)]
            off_target.extend(
                RuleViolation(
                    "test.only-test-files",
                    edit.path,
                    "not a test file; `rn-agent test` only writes tests",
                )
                for edit in proposal.usable_edits
                if not is_test_path(edit.path)
            )
            if allowed:
                narrowed.append(proposal.model_copy(update={"edits": allowed}))
        kept, refused = workflow.screen(narrowed)
        refused = [*off_target, *refused]

        report = self.report
        report.subject = list(self.targets) or list(analysis.selected.paths)
        report.proposals = kept
        report.refused = [
            RefusedEdit(path=violation.path, rule=violation.rule, detail=violation.detail)
            for violation in refused
        ]
        report.notes = list(analysis.proposals.notes)
        report.provider = analysis.proposals.provider
        report.model = analysis.proposals.model
        report.usage = analysis.usage
        return TestPlan(kept=kept, refused=refused, workflow=workflow)

    def execute(self, plan: TestPlan) -> None:
        if not plan.kept:
            self.logger.info("no test files were proposed")
            return
        files = sum(len(proposal.usable_edits) for proposal in plan.kept)
        self.outcome = plan.workflow.apply(
            plan.kept,
            reason="test: generated tests",
            question=f"Write {files} test file(s)?",
        )
        self.report.applied = list(self.outcome.applied)
        self.report.unchanged = list(self.outcome.unchanged)

    def validate(self, plan: TestPlan) -> dict[str, Any]:
        if self.outcome is not None and self.run_tests:
            self.validation = plan.workflow.prove(
                ("tests",), outcome=self.outcome, test_paths=self.report.applied
            )
            self.report.rolled_back = plan.workflow.rolled_back
            self.report.validation = self.validation
        summary: dict[str, Any] = {
            "applied": len(self.report.applied),
            "rolled_back": self.report.rolled_back,
        }
        if self.context.dry_run:
            return summary
        try:
            self.context.paths.ensure()
            path = self.context.paths.cache_dir / "test-report.json"
            write_json(path, self.report.model_dump(mode="json"))
            summary["report"] = str(path)
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write test report: %s", exc)
        return summary

    def render(self, analysis: TestAnalysis, plan: TestPlan) -> None:
        ui.header("Generated tests", f"{analysis.framework} ({analysis.framework_source})")
        change_view.render_context(analysis.selected, verbose=self.verbose)
        change_view.render_proposals(analysis.proposals, verbose=self.verbose)
        change_view.render_refusals(plan.refused)
        if self.outcome is not None:
            change_view.render_outcome(self.outcome, dry_run=self.context.dry_run)
        change_view.render_validation(self.validation)
        change_view.render_usage(
            analysis.usage,
            model=analysis.proposals.model,
            provider=analysis.proposals.provider,
        )
        self._closing_line()

    def summary(self, analysis: TestAnalysis, plan: TestPlan) -> dict[str, Any]:
        return {
            "framework": analysis.framework,
            "framework_source": analysis.framework_source,
            "targets": list(self.targets),
            "proposals": len(plan.kept),
            "refused": len(plan.refused),
            "applied": len(self.report.applied),
            "rolled_back": self.report.rolled_back,
            "validated": self.report.validated,
            **analysis.usage,
        }

    def exit_code(self, analysis: TestAnalysis, plan: TestPlan) -> int:
        if self.validation is not None and not self.validation.ok:
            return 1
        if not plan.kept:
            return 1
        return 0

    # -- helpers -----------------------------------------------------------
    def _framework(self, project: ProjectContext) -> tuple[str, str]:
        """What to write tests with, and what decided that."""
        if self.framework:
            return self.framework, "--framework"
        for package, label in FRAMEWORK_PACKAGES:
            if project.has_dependency(package):
                return label, f"{package} in package.json"
        if project.architecture.testing:
            return project.architecture.testing[0], "inferred architecture"
        if "test" in project.scripts:
            return "the project's `test` script", "package.json scripts"
        raise RNAgentError(
            "this project has no test framework installed",
            hint="Add jest (and @testing-library/react-native), or pass --framework.",
        )

    def _existing_tests(self) -> list[str]:
        """A few real test paths, so generated tests land where these live."""
        root = self.context.root
        found: list[str] = []
        for path in self.context.walker.source_files():
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover - walker stays inside the root
                continue
            if is_test_path(relative):
                found.append(relative)
            if len(found) >= 5:
                break
        return found

    def _closing_line(self) -> None:
        ui.blank()
        if self.context.dry_run:
            ui.note("dry run: nothing was written")
            return
        if self.report.rolled_back:
            ui.failure("the generated tests failed; they were rolled back")
            ui.note("re-run with --keep to inspect them")
            return
        if self.validation is not None and not self.validation.ok:
            ui.failure("the generated tests fail; they were kept (--keep)")
            return
        if self.report.applied and self.validation is not None:
            ui.success(f"{len(self.report.applied)} test file(s) written and passing")
            return
        if self.report.applied:
            ui.success(f"{len(self.report.applied)} test file(s) written")
            ui.note("run them yourself: --no-run skipped execution")
            return
        ui.warning("no test file was produced")


register(TestCommand, phase=3)
