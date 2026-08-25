"""From a proposal to bytes on disk - through every gate, in order.

The order is the point:

1. **Rules.** ``rules.yaml`` is enforced before anything else, so a model that
   ignored the constraints in the prompt still cannot write a lockfile, a native
   file or a new dependency.
2. **Risk.** ``SafetyManager.risk_of`` classifies the paths; native and lockfile
   paths are never low risk, so they can never be auto-applied.
3. **Consent.** ``SafetyManager.evaluate`` decides whether to ask, and the
   developer's answer is final. Declining raises ``ConfirmationDeclined`` before
   anything has been written.
4. **Write.** ``FileManager`` resolves each path inside the project root, backs
   up the previous bytes and records a ``FileChange``.
5. **Undo.** ``rollback()`` restores every applied change byte-for-byte, which is
   what lets a command apply first and validate second.

``dry_run`` reaches step 4 and records the intent without writing, so a preview
is the same code path minus the bytes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..core.context import AgentContext
from ..errors import ConfirmationDeclined
from ..models.changes import ChangeSet, FileChange, RiskLevel
from ..models.proposal import EditAction, FileEdit, Proposal
from .rules import ProjectRules, RuleViolation, dependency_delta, is_native_path

NO_CHANGE_MARKER = "(no change needed)"


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """What happened when a set of edits met the safety envelope."""

    changes: ChangeSet | None = None
    applied: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    refused: tuple[RuleViolation, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    asked: bool = False
    rolled_back: bool = False
    dry_run: bool = False

    @property
    def wrote_anything(self) -> bool:
        return bool(self.applied)

    def summary(self) -> dict[str, object]:
        return {
            "applied": len(self.applied),
            "unchanged": len(self.unchanged),
            "refused": len(self.refused),
            "risk": self.risk.value,
            "rolled_back": self.rolled_back,
            "dry_run": self.dry_run,
        }


@dataclass(slots=True)
class EditApplier:
    """Applies model-proposed edits under the project's safety policy."""

    context: AgentContext
    rules: ProjectRules
    allow_dependencies: bool = False
    allow_native: bool = False

    # -- screening ---------------------------------------------------------
    def screen(self, edits: Sequence[FileEdit]) -> tuple[list[FileEdit], list[RuleViolation]]:
        """Split edits into the allowed ones and the refusals, with reasons."""
        violations = self.rules.violations(
            edits,
            allow_dependencies=self.allow_dependencies,
            allow_native=self.allow_native,
        )
        blocked = {violation.path for violation in violations}
        if not self.context.config.safety.allow_native_edits:
            for edit in edits:
                if edit.path not in blocked and is_native_path(edit.path):
                    blocked.add(edit.path)
                    violations.append(
                        RuleViolation(
                            "safety.allow_native_edits",
                            edit.path,
                            "native edits are disabled in .rn-agent/config.yaml",
                        )
                    )
        return [edit for edit in edits if edit.path not in blocked], violations

    def screen_proposals(
        self, proposals: Sequence[Proposal]
    ) -> tuple[list[Proposal], list[RuleViolation]]:
        """Screen whole proposals; a proposal keeps only its allowed edits."""
        kept: list[Proposal] = []
        violations: list[RuleViolation] = []
        for proposal in proposals:
            allowed, refused = self.screen(proposal.usable_edits)
            violations.extend(refused)
            if allowed:
                kept.append(proposal.model_copy(update={"edits": allowed}))
        return kept, violations

    # -- applying ----------------------------------------------------------
    def apply(
        self,
        edits: Sequence[FileEdit],
        *,
        reason: str,
        question: str | None = None,
    ) -> ApplyOutcome:
        """Screen, ask, write. Raises ``ConfirmationDeclined`` if refused."""
        allowed, violations = self.screen(edits)
        if not allowed:
            return ApplyOutcome(refused=tuple(violations), dry_run=self.context.dry_run)

        agent = self.context
        paths = [edit.path for edit in allowed]
        risk = agent.safety.risk_of(paths)
        decision = agent.safety.evaluate(
            risk=risk, file_count=len(paths), rollback_available=True
        )
        if decision.blocked:
            raise ConfirmationDeclined(decision.reason)
        if decision.requires_confirmation:
            prompt = question or f"Apply {len(paths)} file change(s) (risk: {risk.value})?"
            agent.safety.require(prompt, default=False)

        applied: list[str] = []
        unchanged: list[str] = []
        for edit in allowed:
            change = self._write(edit, reason=reason, risk=risk)
            if change is None:
                continue
            if NO_CHANGE_MARKER in change.reason:
                unchanged.append(change.path)
            else:
                applied.append(change.path)

        agent.logger.info(
            "%s %s file(s) at risk=%s",
            "would apply" if agent.dry_run else "applied",
            len(applied),
            risk.value,
        )
        return ApplyOutcome(
            changes=agent.files.changes,
            applied=tuple(applied),
            unchanged=tuple(unchanged),
            refused=tuple(violations),
            risk=risk,
            asked=decision.requires_confirmation,
            dry_run=agent.dry_run,
        )

    def rollback(self) -> list[str]:
        """Undo everything written through this applier's FileManager."""
        return self.context.files.rollback()

    # -- internals ---------------------------------------------------------
    def _write(self, edit: FileEdit, *, reason: str, risk: RiskLevel) -> FileChange | None:
        files = self.context.files
        detail = f"{reason}: {edit.reason}" if edit.reason else reason
        if edit.action is EditAction.DELETE:
            return files.delete(edit.path, reason=detail, risk=risk)
        if edit.content is None:  # pragma: no cover - dropped during parsing
            return None
        return files.write(edit.path, edit.content, reason=detail, risk=risk)


def describe_dependency_change(before: str | None, after: str | None) -> str:
    """Human summary of a ``package.json`` rewrite, for a refusal message."""
    delta = dependency_delta(before, after)
    parts = [
        f"{label} {', '.join(names)}"
        for label, names in (
            ("adds", delta["added"]),
            ("removes", delta["removed"]),
            ("changes", delta["changed"]),
        )
        if names
    ]
    return "; ".join(parts) if parts else "no dependency change"
