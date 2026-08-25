"""``rn-agent docs`` - documentation from the facts, not from imagination.

The model is handed the scanned project and the current file, and it may write
exactly one path: the one the developer named. An edit anywhere else is refused,
because "document my project" must never turn into "rewrite my project".

Updating beats replacing: when the file exists it is included in the prompt with
an instruction to keep what is still accurate, so a hand-written paragraph is not
silently thrown away by the next run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..agents.apply import ApplyOutcome
from ..agents.context_builder import ContextBuilder, PromptContext
from ..agents.engine import AIEngine
from ..agents.prompts import docs_messages
from ..agents.rules import ProjectRules, RuleViolation
from ..agents.workflow import EditWorkflow
from ..cli import ui
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..errors import RNAgentError
from ..models.project import ProjectContext
from ..models.proposal import EditRunReport, Proposal, ProposalSet, RefusedEdit
from ..reporting import change_view
from ..utils.io import read_text, write_json
from .health import CONTEXT_STALE_SECONDS

SECTIONS: tuple[str, ...] = (
    "overview",
    "architecture",
    "setup",
    "scripts",
    "dependencies",
    "platforms",
    "testing",
    "troubleshooting",
)

DEFAULT_SECTIONS: tuple[str, ...] = (
    "overview",
    "architecture",
    "setup",
    "scripts",
    "dependencies",
    "platforms",
)


@dataclass(slots=True)
class DocsAnalysis:
    project: ProjectContext
    selected: PromptContext
    proposals: ProposalSet
    sections: tuple[str, ...]
    existing: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class DocsPlan:
    kept: list[Proposal]
    refused: list[RuleViolation]
    workflow: EditWorkflow


class DocsCommand(AgentCommand[DocsAnalysis, DocsPlan]):
    name = "docs"
    description = "Write project documentation from the scanned facts"
    read_only = False
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        sections: tuple[str, ...] = (),
        output: str = "docs/PROJECT.md",
        files: tuple[str, ...] = (),
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.sections = sections
        self.output = output
        self.files = files
        self.verbose = verbose
        self.report = EditRunReport(task="docs", dry_run=context.dry_run)
        self.outcome: ApplyOutcome | None = None
        #: Set when the model produced a file with nothing in it.
        self.empty = False

    # -- phases ------------------------------------------------------------
    def analyze(self) -> DocsAnalysis:
        unknown = [name for name in self.sections if name not in SECTIONS]
        if unknown:
            raise RNAgentError(
                f"unknown documentation section(s): {', '.join(unknown)}",
                hint=f"Known sections: {', '.join(SECTIONS)}.",
            )
        sections = self.sections or DEFAULT_SECTIONS
        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        existing = read_text(self.context.files.resolve(self.output))
        selected = ContextBuilder(self.context).select(
            paths=self.files, query=" ".join(sections)
        )
        engine = AIEngine(self.context)
        proposals = engine.propose(
            docs_messages(
                project=project,
                rules=ProjectRules.load(self.context.paths),
                context=selected,
                sections=sections,
                target=self.output,
                existing=existing,
            ),
            task="docs",
        )
        return DocsAnalysis(
            project=project,
            selected=selected,
            proposals=proposals,
            sections=sections,
            existing=existing,
            usage=engine.usage,
        )

    def plan(self, analysis: DocsAnalysis) -> DocsPlan:
        workflow = EditWorkflow(
            self.context, rules=ProjectRules.load(self.context.paths), task="docs"
        )
        off_target: list[RuleViolation] = []
        narrowed: list[Proposal] = []
        for proposal in analysis.proposals.proposals:
            allowed = [edit for edit in proposal.usable_edits if edit.path == self.output]
            off_target.extend(
                RuleViolation(
                    "docs.single-output",
                    edit.path,
                    f"`rn-agent docs` only writes {self.output}",
                )
                for edit in proposal.usable_edits
                if edit.path != self.output
            )
            if allowed:
                narrowed.append(proposal.model_copy(update={"edits": allowed}))
        kept, refused = workflow.screen(narrowed)
        refused = [*off_target, *refused]

        report = self.report
        report.subject = list(analysis.sections)
        report.proposals = kept
        report.refused = [
            RefusedEdit(path=violation.path, rule=violation.rule, detail=violation.detail)
            for violation in refused
        ]
        report.notes = list(analysis.proposals.notes)
        report.provider = analysis.proposals.provider
        report.model = analysis.proposals.model
        report.usage = analysis.usage
        return DocsPlan(kept=kept, refused=refused, workflow=workflow)

    def execute(self, plan: DocsPlan) -> None:
        if not plan.kept:
            self.logger.info("no documentation content was produced")
            return
        self.outcome = plan.workflow.apply(
            plan.kept,
            reason=f"docs: {self.output}",
            question=f"Write {self.output}?",
        )
        self.report.applied = list(self.outcome.applied)
        self.report.unchanged = list(self.outcome.unchanged)

    def validate(self, plan: DocsPlan) -> dict[str, Any]:
        summary: dict[str, Any] = {"applied": len(self.report.applied)}
        if self.context.dry_run:
            return summary
        written = self.context.files.resolve(self.output)
        if self.report.applied:
            content = read_text(written)
            if not content or not content.strip():
                # An empty file is not documentation; say so and fail.
                self.report.notes.append(f"{self.output} was written but is empty")
                self.empty = True
            else:
                summary["bytes"] = len(content.encode("utf-8"))
                summary["path"] = str(written)
        try:
            self.context.paths.ensure()
            path = self.context.paths.cache_dir / "docs-report.json"
            write_json(path, self.report.model_dump(mode="json"))
            summary["report"] = str(path)
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write docs report: %s", exc)
        return summary

    def render(self, analysis: DocsAnalysis, plan: DocsPlan) -> None:
        ui.header("Documentation", f"{self.output} \u00b7 {', '.join(analysis.sections)}")
        change_view.render_context(analysis.selected, verbose=self.verbose)
        change_view.render_proposals(analysis.proposals, verbose=self.verbose)
        change_view.render_refusals(plan.refused)
        if self.outcome is not None:
            change_view.render_outcome(self.outcome, dry_run=self.context.dry_run)
        change_view.render_usage(
            analysis.usage,
            model=analysis.proposals.model,
            provider=analysis.proposals.provider,
        )
        ui.blank()
        if self.context.dry_run:
            ui.note("dry run: nothing was written")
        elif self.report.applied:
            verb = "updated" if analysis.existing else "written"
            ui.success(f"{self.output} {verb}")
        elif self.report.unchanged:
            ui.note(f"{self.output} was already up to date")
        else:
            ui.warning("no documentation was written")

    def summary(self, analysis: DocsAnalysis, plan: DocsPlan) -> dict[str, Any]:
        return {
            "output": self.output,
            "sections": list(analysis.sections),
            "updated": bool(analysis.existing),
            "applied": len(self.report.applied),
            "refused": len(plan.refused),
            **analysis.usage,
        }

    def exit_code(self, analysis: DocsAnalysis, plan: DocsPlan) -> int:
        if self.empty:
            return 1
        if self.report.applied or self.report.unchanged:
            return 0
        return 1


register(DocsCommand, phase=6)
