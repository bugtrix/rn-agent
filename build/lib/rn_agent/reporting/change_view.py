"""Rendering proposed and applied changes.

Shared by every command that writes code (``fix``, ``feature``, ``test``,
``docs``), because a developer should see the same thing every time: what was
proposed, what the rules refused, what landed, the risk, and whether the result
still builds.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..agents.apply import ApplyOutcome
from ..agents.context_builder import PromptContext
from ..agents.rules import RuleViolation
from ..cli import ui
from ..models.proposal import EditAction, ProposalSet
from ..models.validation import StepStatus, ValidationReport

ACTION_MARK = {
    EditAction.CREATE: "+",
    EditAction.MODIFY: "~",
    EditAction.DELETE: "-",
}

RISK_STYLE = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
}


def render_context(context: PromptContext, *, verbose: bool = False) -> None:
    """What was sent to the model - the developer's audit trail."""
    ui.section("Context sent")
    ui.key_values(
        [
            ("files", len(context)),
            ("approx tokens", f"{context.approx_tokens:,}"),
            ("secrets refused", len(context.refused) or "[muted]0[/muted]"),
            ("not sent (budget)", len(context.skipped) or "[muted]0[/muted]"),
        ]
    )
    if verbose:
        for file in context.files:
            suffix = " [muted](truncated)[/muted]" if file.truncated else ""
            ui.note(f"{file.path}  {file.lines} lines{suffix}")
        for path in context.refused:
            ui.console().print(f"  [warn]refused[/warn] [muted]{path} (secret-bearing)[/muted]")


def render_proposals(proposals: ProposalSet, *, verbose: bool = False) -> None:
    """The model's answer, before anything is written."""
    counts = proposals.counts()
    ui.section(f"Proposed ({counts['proposals']})")
    for proposal in proposals.proposals:
        style = RISK_STYLE.get(proposal.risk.value, "info")
        ui.console().print(
            f"  [{style}]{proposal.risk.value}[/{style}]  [heading]{proposal.title}[/heading]"
            f"  [muted]{proposal.id}[/muted]"
        )
        if proposal.summary:
            ui.console().print(f"      {proposal.summary}")
        for edit in proposal.edits:
            mark = ACTION_MARK.get(edit.action, "~")
            detail = f"  [muted]{edit.reason}[/muted]" if verbose and edit.reason else ""
            ui.console().print(f"      {mark} {edit.path}{detail}")
        for command in proposal.commands:
            ui.console().print(f"      [info]run:[/info] {command}")
        if proposal.addresses:
            ui.note(f"addresses {', '.join(proposal.addresses)}")
    if proposals.notes:
        ui.section("Model notes")
        for note in proposals.notes:
            ui.bullet(note, style="muted", marker="·")


def render_refusals(violations: Sequence[RuleViolation]) -> None:
    """Edits the project's own rules rejected."""
    if not violations:
        return
    ui.section(f"Refused by your rules ({len(violations)})")
    for violation in violations:
        ui.console().print(
            f"  {ui.MARK_FAIL} {violation.path}  [muted]{violation.rule}[/muted]"
        )
        ui.console().print(f"      {violation.detail}")


def render_outcome(outcome: ApplyOutcome, *, dry_run: bool = False) -> None:
    """What actually changed on disk."""
    verb = "Would change" if dry_run else "Changed"
    ui.section(f"{verb} ({len(outcome.applied)})")
    for path in outcome.applied:
        ui.console().print(f"  {ui.MARK_OK} {path}")
    for path in outcome.unchanged:
        ui.console().print(f"  {ui.MARK_SKIP} {path} [muted]already correct[/muted]")
    if outcome.changes is not None and outcome.changes.rollback_available and not dry_run:
        ui.note("backups written to .rn-agent/cache/backups/")


def render_validation(report: ValidationReport | None) -> None:
    """The proof - or the honest absence of it."""
    if report is None or not report.steps:
        return
    ui.section("Validation")
    for step in report.steps:
        if step.status is StepStatus.PASS:
            mark, style = ui.MARK_OK, "ok"
        elif step.status is StepStatus.FAIL:
            mark, style = ui.MARK_FAIL, "fail"
        else:
            mark, style = ui.MARK_SKIP, "muted"
        ui.console().print(f"  {mark} {step.name.ljust(10)} [{style}]{step.detail}[/{style}]")
        if step.status is StepStatus.FAIL and step.output_tail:
            for line in step.output_tail.splitlines()[-8:]:
                ui.console().print(f"      [muted]{line}[/muted]")


def render_usage(usage: dict[str, int], *, model: str | None, provider: str | None) -> None:
    if not usage.get("calls"):
        return
    ui.section("AI usage")
    ui.key_values(
        [
            ("provider", provider or "-"),
            ("model", model or "-"),
            ("calls", usage.get("calls", 0)),
            ("input tokens", f"{usage.get('input_tokens', 0):,}"),
            ("output tokens", f"{usage.get('output_tokens', 0):,}"),
        ]
    )
