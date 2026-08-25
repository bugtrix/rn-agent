"""``rn-agent review`` - an opinion about your code, with its evidence.

Read-only by construction: ``read_only = True`` means ``execute()`` is never
called, so a review cannot change a line of your app.

Two rules keep the output honest. A finding about a file the model was never
given is dropped, because it can only be speculation; and the score uses the
same penalty table as ``health``, so "78/100" means the same thing in both
commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agents.context_builder import ContextBuilder, PromptContext
from ..agents.engine import AIEngine
from ..agents.prompts import review_messages
from ..agents.rules import ProjectRules
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..errors import RNAgentError
from ..models.health import Severity
from ..models.project import ProjectContext
from ..models.review import REVIEW_AREAS, ReviewFinding, ReviewReport
from ..reporting.review_view import render_review
from ..utils.io import write_json
from .health import CONTEXT_STALE_SECONDS


@dataclass(slots=True)
class ReviewAnalysis:
    project: ProjectContext
    selected: PromptContext
    findings: list[ReviewFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ReviewPlan:
    report: ReviewReport


class ReviewCommand(AgentCommand[ReviewAnalysis, ReviewPlan]):
    name = "review"
    description = "Analyse components, hooks, state and performance with your model"
    read_only = True
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        files: tuple[str, ...] = (),
        changed: bool = False,
        areas: tuple[str, ...] = (),
        instruction: str | None = None,
        limit: int | None = None,
        fail_under: int | None = None,
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.files = files
        self.changed = changed
        self.areas = areas
        self.instruction = instruction
        self.limit = limit
        self.fail_under = fail_under
        self.verbose = verbose
        #: Set in plan(); lets `--json` emit the report without a second run.
        self.report: ReviewReport | None = None
        self.selected: PromptContext | None = None

    # -- phases ------------------------------------------------------------
    def analyze(self) -> ReviewAnalysis:
        unknown = [area for area in self.areas if area not in REVIEW_AREAS]
        if unknown:
            raise RNAgentError(
                f"unknown review area(s): {', '.join(unknown)}",
                hint=f"Known areas: {', '.join(REVIEW_AREAS)}.",
            )

        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        selected = ContextBuilder(self.context).select(
            paths=self.files,
            changed=self.changed,
            query=self.instruction,
            limit=self.limit,
        )
        if not selected:
            raise RNAgentError(
                "no reviewable source files were selected",
                hint="Pass --file, commit some code, or check context.exclude_globs.",
            )
        self.selected = selected

        engine = AIEngine(self.context)
        findings, notes, completion = engine.review(
            review_messages(
                project=project,
                rules=ProjectRules.load(self.context.paths),
                context=selected,
                areas=self.areas,
                instruction=self.instruction,
            )
        )
        self.logger.info("review returned %s finding(s)", len(findings))
        return ReviewAnalysis(
            project=project,
            selected=selected,
            findings=findings,
            notes=notes,
            provider=completion.provider,
            model=completion.model,
            usage=engine.usage,
        )

    def plan(self, analysis: ReviewAnalysis) -> ReviewPlan:
        sent = set(analysis.selected.paths)
        kept: list[ReviewFinding] = []
        notes = list(analysis.notes)
        dropped_area = 0
        dropped_unseen = 0
        for finding in analysis.findings:
            if self.areas and finding.area not in self.areas:
                dropped_area += 1
                continue
            if finding.file and finding.file not in sent:
                dropped_unseen += 1
                continue
            kept.append(finding)
        if dropped_unseen:
            notes.append(
                f"{dropped_unseen} finding(s) referred to files that were not sent to the "
                "model and were dropped."
            )
        if dropped_area:
            notes.append(f"{dropped_area} finding(s) fell outside the requested areas.")

        report = ReviewReport(
            project_root=analysis.project.root,
            rn_version=analysis.project.rn_version,
            provider=analysis.provider,
            model=analysis.model,
            files_reviewed=list(analysis.selected.paths),
            files_skipped=[*analysis.selected.skipped, *analysis.selected.refused],
            findings=kept,
            notes=notes,
            input_tokens=analysis.usage.get("input_tokens", 0),
            output_tokens=analysis.usage.get("output_tokens", 0),
        )
        self.report = report
        return ReviewPlan(report=report)

    def validate(self, plan: ReviewPlan) -> dict[str, Any]:
        if self.context.dry_run:
            return {}
        report_path = self.context.paths.cache_dir / "review-report.json"
        try:
            self.context.paths.ensure()
            write_json(report_path, plan.report.model_dump(mode="json"))
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write review report: %s", exc)
            return {}
        if self.context.run_id is not None:
            try:
                self.context.store.record_findings(
                    self.context.run_id,
                    "review",
                    [finding.model_dump(mode="json") for finding in plan.report.findings],
                )
            except Exception as exc:  # pragma: no cover - storage failure
                self.logger.warning("could not record findings: %s", exc)
        return {"report": str(report_path)}

    def render(self, analysis: ReviewAnalysis, plan: ReviewPlan) -> None:
        render_review(
            plan.report,
            context=analysis.selected,
            usage=analysis.usage,
            verbose=self.verbose,
        )

    def summary(self, analysis: ReviewAnalysis, plan: ReviewPlan) -> dict[str, Any]:
        report = plan.report
        return {
            "score": report.score,
            "grade": report.grade,
            "provider": report.provider,
            "model": report.model,
            "areas": list(self.areas),
            **report.counts(),
            **analysis.usage,
        }

    def exit_code(self, analysis: ReviewAnalysis, plan: ReviewPlan) -> int:
        report = plan.report
        if report.by_severity(Severity.CRITICAL):
            return 1
        if self.fail_under is not None and report.score < self.fail_under:
            return 1
        return 0


register(ReviewCommand, phase=3)
