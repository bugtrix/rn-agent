"""``rn-agent feature`` - build a feature the way this project builds features.

The value is not "an LLM wrote a screen". It is that the model is handed the
project's inferred architecture and its ``rules.yaml`` before it is asked for
anything, the answer is screened against those rules, and the result is
typechecked - so a project on Redux Saga does not get handed React Query, and a
feature that does not compile does not survive the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agents.apply import ApplyOutcome
from ..agents.context_builder import ContextBuilder, PromptContext
from ..agents.engine import AIEngine
from ..agents.prompts import feature_messages
from ..agents.rules import ProjectRules, RuleViolation
from ..agents.workflow import EditWorkflow
from ..cli import ui
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..errors import RNAgentError
from ..models.project import ProjectContext
from ..models.proposal import EditAction, EditRunReport, Proposal, ProposalSet, RefusedEdit
from ..models.validation import ValidationReport
from ..reporting import change_view
from ..utils.io import write_json
from .health import CONTEXT_STALE_SECONDS


@dataclass(slots=True)
class FeatureAnalysis:
    project: ProjectContext
    selected: PromptContext
    proposals: ProposalSet
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class FeaturePlan:
    kept: list[Proposal]
    refused: list[RuleViolation]
    workflow: EditWorkflow


class FeatureCommand(AgentCommand[FeatureAnalysis, FeaturePlan]):
    name = "feature"
    description = "Implement a feature following the project's existing architecture"
    read_only = False
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        description: str = "",
        files: tuple[str, ...] = (),
        allow_dependencies: bool = False,
        checks: tuple[str, ...] = ("typecheck",),
        keep_on_failure: bool = False,
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.feature = description.strip()
        self.files = files
        self.allow_dependencies = allow_dependencies
        self.checks = checks
        self.keep_on_failure = keep_on_failure
        self.verbose = verbose
        self.report = EditRunReport(task="feature", dry_run=context.dry_run)
        self.outcome: ApplyOutcome | None = None
        self.validation: ValidationReport | None = None

    # -- phases ------------------------------------------------------------
    def analyze(self) -> FeatureAnalysis:
        if not self.feature:
            raise RNAgentError(
                "feature needs a description",
                hint='Example: rn-agent feature "add a pull-to-refresh on the orders list".',
            )
        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        selected = ContextBuilder(self.context).select(paths=self.files, query=self.feature)
        if not selected:
            raise RNAgentError(
                "no source files could be selected as an example to follow",
                hint="Pass --file to show the model the patterns to imitate.",
            )
        engine = AIEngine(self.context)
        proposals = engine.propose(
            feature_messages(
                project=project,
                rules=ProjectRules.load(self.context.paths),
                context=selected,
                description=self.feature,
            ),
            task="feature",
        )
        return FeatureAnalysis(
            project=project, selected=selected, proposals=proposals, usage=engine.usage
        )

    def plan(self, analysis: FeatureAnalysis) -> FeaturePlan:
        workflow = EditWorkflow(
            self.context,
            rules=ProjectRules.load(self.context.paths),
            task="feature",
            allow_dependencies=self.allow_dependencies,
            allowed_native_paths=self.files,
            keep_on_failure=self.keep_on_failure,
        )
        kept, refused = workflow.screen(analysis.proposals.proposals)
        report = self.report
        report.subject = [self.feature]
        report.proposals = kept
        report.refused = [
            RefusedEdit(path=violation.path, rule=violation.rule, detail=violation.detail)
            for violation in refused
        ]
        report.notes = list(analysis.proposals.notes)
        report.provider = analysis.proposals.provider
        report.model = analysis.proposals.model
        report.usage = analysis.usage
        return FeaturePlan(kept=kept, refused=refused, workflow=workflow)

    def execute(self, plan: FeaturePlan) -> None:
        if not plan.kept:
            self.logger.info("nothing to apply: every proposal was refused or empty")
            return
        files = sum(len(proposal.usable_edits) for proposal in plan.kept)
        self.outcome = plan.workflow.apply(
            plan.kept,
            reason=f"feature: {self.feature}",
            question=f"Create/modify {files} file(s) for this feature?",
        )
        self.report.applied = list(self.outcome.applied)
        self.report.unchanged = list(self.outcome.unchanged)

    def validate(self, plan: FeaturePlan) -> dict[str, Any]:
        if self.outcome is not None:
            self.validation = plan.workflow.prove(self.checks, outcome=self.outcome)
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
            path = self.context.paths.cache_dir / "feature-report.json"
            write_json(path, self.report.model_dump(mode="json"))
            summary["report"] = str(path)
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write feature report: %s", exc)
        return summary

    def render(self, analysis: FeatureAnalysis, plan: FeaturePlan) -> None:
        ui.header("Feature", self.feature)
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
        self._closing_line(analysis, plan)

    def summary(self, analysis: FeatureAnalysis, plan: FeaturePlan) -> dict[str, Any]:
        created = sum(
            1
            for proposal in plan.kept
            for edit in proposal.usable_edits
            if edit.action is EditAction.CREATE
        )
        return {
            "description": self.feature,
            "proposals": len(plan.kept),
            "refused": len(plan.refused),
            "created": created,
            "applied": len(self.report.applied),
            "rolled_back": self.report.rolled_back,
            "validated": self.report.validated,
            **analysis.usage,
        }

    def exit_code(self, analysis: FeatureAnalysis, plan: FeaturePlan) -> int:
        if self.validation is not None and not self.validation.ok:
            return 1
        if not plan.kept:
            return 1
        return 0

    # -- helpers -----------------------------------------------------------
    def _closing_line(self, analysis: FeatureAnalysis, plan: FeaturePlan) -> None:
        ui.blank()
        if self.context.dry_run:
            ui.note("dry run: nothing was written")
            return
        if self.report.rolled_back:
            ui.failure("the feature did not compile; every file was rolled back")
            ui.note("re-run with --keep to inspect it, or narrow the request")
            return
        if self.validation is not None and not self.validation.ok:
            ui.failure("the feature does not compile; the files were kept (--keep)")
            return
        if self.report.applied:
            ui.success(f"{len(self.report.applied)} file(s) written")
            for proposal in plan.kept:
                for command in proposal.commands:
                    ui.bullet(f"run: {command}")
            ui.note("review the diff before you commit it")
            return
        ui.warning("no usable proposal survived your rules")


register(FeatureCommand, phase=3)
