"""The shape every write-command shares: propose, screen, apply, prove, undo.

``fix``, ``feature``, ``test`` and ``docs`` differ in what they ask the model
for and in what they consider proof. They do not differ in what happens
afterwards, so that part lives here once:

    screen against rules -> apply behind the safety gate -> validate ->
    roll back if the proof failed (CI / ``--yes``)

On a real terminal the files stay: the developer already confirmed the write,
and a follow-up repair Q&A is slower than keeping them. ``migrate`` still has
its own quiet repair loop because a version bump that does not build should
not land.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..core.context import AgentContext
from ..models.proposal import FileEdit, Proposal
from ..models.validation import ValidationReport
from ..validation.runner import ProjectValidator
from .apply import ApplyOutcome, EditApplier
from .rules import ProjectRules, RuleViolation


@dataclass(slots=True)
class EditWorkflow:
    """Applies proposals and proves the result, or restores the project."""

    context: AgentContext
    rules: ProjectRules
    task: str
    allow_dependencies: bool = False
    allow_native: bool = False
    allowed_native_paths: tuple[str, ...] = ()
    keep_on_failure: bool = False
    applier: EditApplier = field(init=False)
    rolled_back: bool = field(init=False, default=False)
    #: Set by the migrate repair loop: ask once, then apply later rounds.
    repair_allowed: bool | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.applier = EditApplier(
            self.context,
            rules=self.rules,
            allow_dependencies=self.allow_dependencies,
            allow_native=self.allow_native,
            allowed_native_paths=self.allowed_native_paths,
        )

    # -- screening ---------------------------------------------------------
    def screen(
        self, proposals: Sequence[Proposal]
    ) -> tuple[list[Proposal], list[RuleViolation]]:
        return self.applier.screen_proposals(proposals)

    # -- applying ----------------------------------------------------------
    def apply(
        self,
        proposals: Sequence[Proposal],
        *,
        reason: str,
        question: str | None = None,
        confirmed: bool = False,
        confirm_default: bool = False,
    ) -> ApplyOutcome:
        edits: list[FileEdit] = [edit for proposal in proposals for edit in proposal.usable_edits]
        if not edits:
            return ApplyOutcome(dry_run=self.context.dry_run)
        return self.applier.apply(
            edits,
            reason=reason,
            question=question,
            confirmed=confirmed,
            confirm_default=confirm_default,
        )

    # -- proving -----------------------------------------------------------
    def prove(
        self,
        checks: Sequence[str],
        *,
        outcome: ApplyOutcome,
        test_paths: Sequence[str] = (),
    ) -> ValidationReport | None:
        """Run the checks and undo the change when they fail.

        ``None`` means no validation ran at all (nothing was applied, a dry run,
        or the caller asked for no checks) - which is *not* the same as passing,
        and the renderer says so.

        Interactive sessions keep a failed apply instead of opening a repair
        Q&A: the write was already confirmed, and typecheck on a messy tree is
        not proof that change was wrong. ``--yes`` and pytest still roll back
        unless ``keep_on_failure`` is set.
        """
        if not checks or not outcome.wrote_anything or self.context.dry_run:
            return None
        validator = ProjectValidator(self.context)
        report = validator.run(list(checks), test_paths=test_paths)
        if not report.ok and not self.keep_on_failure:
            if self._keep_failed_apply():
                self.context.logger.warning(
                    "%s validation failed; keeping %s file(s)",
                    self.task,
                    len(outcome.applied),
                )
                return report
            restored = self.applier.rollback()
            self.rolled_back = bool(restored)
            self.context.logger.warning(
                "%s validation failed; rolled back %s file(s)", self.task, len(restored)
            )
        return report

    def _keep_failed_apply(self) -> bool:
        from ..cli.working import working_enabled

        return working_enabled() and not self.context.assume_yes
