"""``rn-agent fix`` - repair what ``health`` or ``review`` reported.

The interesting part is not the model call, it is that every write went through
``FileManager`` with a backup. Validation is opt-in (``--check``): a failed
proof can undo the change, which is only safe because ``rollback()`` restores
the previous bytes exactly.

``--issue <id>`` reads the finding out of the knowledge store, which is what
makes the commands one agent: ``health`` recorded the id, ``fix`` acts on it,
and nobody has to paste an error message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agents.apply import ApplyOutcome
from ..agents.context_builder import ContextBuilder, PromptContext
from ..agents.engine import AIEngine
from ..agents.prompts import fix_messages
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


@dataclass(frozen=True, slots=True)
class ResolvedIssue:
    """A finding recorded by an earlier command."""

    id: str
    title: str
    detail: str = ""
    recommendation: str | None = None
    #: The literal lines the check already worked out. Passing them stops the
    #: model re-deriving syntax that is known exactly.
    fix: tuple[str, ...] = ()
    file: str | None = None
    kind: str = "health"

    def as_prompt_line(self) -> str:
        parts = [f"{self.id}: {self.title}"]
        if self.detail:
            parts.append(f"- {self.detail}")
        if self.recommendation:
            parts.append(f"(recommended: {self.recommendation})")
        if self.fix:
            parts.append("(exact lines: " + " ".join(self.fix) + ")")
        if self.file:
            parts.append(f"[{self.file}]")
        return " ".join(parts)


@dataclass(slots=True)
class FixAnalysis:
    project: ProjectContext
    selected: PromptContext
    proposals: ProposalSet
    issues: list[ResolvedIssue] = field(default_factory=list)
    unknown_issues: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class FixPlan:
    proposals: ProposalSet
    kept: list[Proposal]
    refused: list[RuleViolation]
    workflow: EditWorkflow


class FixCommand(AgentCommand[FixAnalysis, FixPlan]):
    name = "fix"
    description = "Fix reported problems"
    read_only = False
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        issues: tuple[str, ...] = (),
        files: tuple[str, ...] = (),
        instruction: str | None = None,
        changed: bool = False,
        checks: tuple[str, ...] = (),
        allow_native: bool = False,
        allow_dependencies: bool = False,
        keep_on_failure: bool = False,
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.issues = issues
        self.files = files
        self.instruction = instruction
        self.changed = changed
        self.checks = checks
        self.allow_native = allow_native
        self.allow_dependencies = allow_dependencies
        self.keep_on_failure = keep_on_failure
        self.verbose = verbose
        self.report = EditRunReport(task="fix", dry_run=context.dry_run)
        self.outcome: ApplyOutcome | None = None
        self.validation: ValidationReport | None = None

    # -- phases ------------------------------------------------------------
    def analyze(self) -> FixAnalysis:
        if not (self.issues or self.files or self.instruction or self.changed):
            raise RNAgentError(
                "fix needs to know what to fix",
                hint="Pass --issue <id> (from health/review), --file, --about \"...\" or --changed.",
            )
        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        resolved, unknown = self._resolve_issues()

        seeds = tuple(dict.fromkeys(issue.file for issue in resolved if issue.file))
        builder = ContextBuilder(self.context)
        if self.files:
            selected = builder.select(paths=self.files)
        elif seeds:
            selected = builder.select(paths=seeds)
        elif self.changed:
            selected = builder.select(changed=True)
        else:
            selected = builder.select(query=self.instruction)
        if not selected:
            raise RNAgentError(
                "no source files could be selected to fix",
                hint="Name the file with --file, or run `rn-agent scan` first.",
            )

        engine = AIEngine(self.context)
        proposals = engine.propose(
            fix_messages(
                project=project,
                rules=ProjectRules.load(self.context.paths),
                context=selected,
                issues=[issue.as_prompt_line() for issue in resolved],
                instruction=self.instruction,
            ),
            task="fix",
        )
        return FixAnalysis(
            project=project,
            selected=selected,
            proposals=proposals,
            issues=resolved,
            unknown_issues=unknown,
            usage=engine.usage,
        )

    def plan(self, analysis: FixAnalysis) -> FixPlan:
        workflow = EditWorkflow(
            self.context,
            rules=ProjectRules.load(self.context.paths),
            task="fix",
            allow_dependencies=self.allow_dependencies,
            allow_native=self.allow_native,
            allowed_native_paths=self.files,
            keep_on_failure=self.keep_on_failure,
        )
        kept, refused = workflow.screen(analysis.proposals.proposals)

        report = self.report
        report.subject = [issue.id for issue in analysis.issues] or self._subject()
        report.unknown_issues = analysis.unknown_issues
        report.proposals = kept
        report.refused = [
            RefusedEdit(path=violation.path, rule=violation.rule, detail=violation.detail)
            for violation in refused
        ]
        report.notes = list(analysis.proposals.notes)
        if analysis.unknown_issues:
            report.notes.append(
                f"no recorded finding matches: {', '.join(analysis.unknown_issues)}"
            )
        report.provider = analysis.proposals.provider
        report.model = analysis.proposals.model
        report.usage = analysis.usage
        return FixPlan(
            proposals=analysis.proposals, kept=kept, refused=refused, workflow=workflow
        )

    def execute(self, plan: FixPlan) -> None:
        if not plan.kept:
            self.logger.info("nothing to apply: every proposal was refused or empty")
            return
        subject = ", ".join(plan.kept[0].addresses) or plan.kept[0].title
        files = sum(len(proposal.usable_edits) for proposal in plan.kept)
        self.outcome = plan.workflow.apply(
            plan.kept,
            reason=f"fix: {subject}",
            question=f"Apply {files} file change(s) to fix {subject}?",
        )
        self.report.applied = list(self.outcome.applied)
        self.report.unchanged = list(self.outcome.unchanged)

    def validate(self, plan: FixPlan) -> dict[str, Any]:
        if self.outcome is not None:
            self.validation = plan.workflow.prove(self.checks, outcome=self.outcome)
            self.report.rolled_back = plan.workflow.rolled_back
            if self.validation is not None:
                self.report.validation = self.validation
        summary: dict[str, Any] = {
            "applied": len(self.report.applied),
            "rolled_back": self.report.rolled_back,
        }
        if self.context.dry_run:
            return summary
        try:
            self.context.paths.ensure()
            path = self.context.paths.cache_dir / "fix-report.json"
            write_json(path, self.report.model_dump(mode="json"))
            summary["report"] = str(path)
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write fix report: %s", exc)
        return summary

    def render(self, analysis: FixAnalysis, plan: FixPlan) -> None:
        change_view.render_context(analysis.selected, verbose=self.verbose)
        if analysis.unknown_issues:
            ui.warning(f"no recorded finding matches: {', '.join(analysis.unknown_issues)}")
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
        self._closing_line(plan)

    def summary(self, analysis: FixAnalysis, plan: FixPlan) -> dict[str, Any]:
        return {
            "issues": [issue.id for issue in analysis.issues],
            "unknown_issues": analysis.unknown_issues,
            "proposals": len(plan.kept),
            "refused": len(plan.refused),
            "applied": len(self.report.applied),
            "rolled_back": self.report.rolled_back,
            "validated": None if self.validation is None else self.validation.ok,
            **analysis.usage,
        }

    def exit_code(self, analysis: FixAnalysis, plan: FixPlan) -> int:
        if self.validation is not None and not self.validation.ok:
            return 1
        if plan.refused and not plan.kept:
            return 1
        return 0

    # -- helpers -----------------------------------------------------------
    def _resolve_issues(self) -> tuple[list[ResolvedIssue], list[str]]:
        """Look requested ids up in what `health`/`review` recorded."""
        if not self.issues:
            return [], []
        known: dict[str, ResolvedIssue] = {}
        for kind in ("health", "review"):
            try:
                findings = self.context.store.latest_findings(kind)
            except Exception as exc:  # pragma: no cover - storage failure
                self.logger.warning("could not read %s findings: %s", kind, exc)
                continue
            for payload in findings:
                identifier = str(payload.get("id") or "")
                if not identifier or identifier in known:
                    continue
                known[identifier] = ResolvedIssue(
                    id=identifier,
                    title=str(payload.get("title") or identifier),
                    detail=str(payload.get("detail") or ""),
                    recommendation=payload.get("recommendation") or None,
                    fix=tuple(payload.get("fix") or ()),
                    file=payload.get("file") or None,
                    kind=kind,
                )
        resolved = [known[identifier] for identifier in self.issues if identifier in known]
        unknown = [identifier for identifier in self.issues if identifier not in known]
        if not resolved:
            raise RNAgentError(
                f"no recorded finding matches: {', '.join(unknown)}",
                hint="Run `rn-agent health` or `rn-agent review` first; ids come from their output.",
            )
        return resolved, unknown

    def _subject(self) -> list[str]:
        """What the developer asked for, for the report."""
        if self.files:
            return list(self.files)
        if self.instruction:
            return [self.instruction]
        return ["changed files"] if self.changed else []

    def _closing_line(self, plan: FixPlan) -> None:
        ui.blank()
        if self.context.dry_run:
            ui.note("dry run: nothing was written")
            return
        if self.report.rolled_back:
            ui.failure("validation failed; every change was rolled back")
            ui.note("re-run with --keep to inspect the failing change")
            return
        if self.validation is not None and not self.validation.ok:
            ui.failure("validation failed; the changes were kept")
            return
        if self.report.applied:
            proof = "validated" if self.validation is not None else "not validated"
            ui.success(f"{len(self.report.applied)} file(s) changed ({proof})")
            return
        if plan.refused:
            ui.warning("every proposed change was refused by your rules")
            return
        ui.note("nothing needed changing")


register(FixCommand, phase=3)
