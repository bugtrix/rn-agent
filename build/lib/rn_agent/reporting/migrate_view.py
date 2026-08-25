"""Renders ``rn-agent migrate``.

The layout answers the two questions a developer has after a migration, in
order: *what did it change?* and *what do I still have to do?* Conflicts and
manual steps are therefore not a footnote - they get their own sections, with
the hunk available under ``--verbose``, because they are the work that remains.
"""

from __future__ import annotations

from ..cli import ui
from ..models.migration import MigrationOutcome, MigrationPlan, StepState

STATE_MARK = {
    StepState.APPLIED: ui.MARK_OK,
    StepState.SKIPPED: ui.MARK_SKIP,
    StepState.CONFLICT: ui.MARK_WARN,
    StepState.FAILED: ui.MARK_FAIL,
    StepState.PENDING: ui.MARK_SKIP,
}

RISK_STYLE = {"low": "low", "medium": "medium", "high": "high", "critical": "critical"}

KIND_LABEL = {
    "dependency": "Dependencies",
    "android": "Android",
    "ios": "iOS",
    "javascript": "JavaScript",
    "manual": "Manual",
}


def render_migration(
    plan: MigrationPlan,
    *,
    outcome: MigrationOutcome | None = None,
    verbose: bool = False,
) -> None:
    ui.header(
        f"Migrate {plan.from_version or '?'} \u2192 {plan.to_version or '?'}",
        f"branch {plan.branch}" if plan.branch else "no branch",
    )

    counts = plan.counts()
    ui.section("Summary")
    ui.key_values(
        [
            ("steps", counts["steps"]),
            ("applied", _count(counts["applied"], "ok")),
            ("conflicts", _count(counts["conflict"], "warn")),
            ("manual", _count(counts["manual"], "warn")),
            ("skipped", f"[muted]{counts['skipped']}[/muted]"),
            ("failed", _count(counts["failed"], "fail")),
            ("diff", "unavailable" if plan.offline else "upstream template"),
            ("highest risk", f"[{RISK_STYLE[plan.highest_risk.value]}]{plan.highest_risk.value}[/]"),
        ]
    )

    by_kind = plan.by_kind()
    if by_kind:
        ui.table(
            ["Area", "Steps"],
            [[KIND_LABEL.get(kind, kind), count] for kind, count in sorted(by_kind.items())],
            title="By area",
        )

    for state, label in (
        (StepState.APPLIED, "Applied"),
        (StepState.CONFLICT, "Conflicts - apply these by hand"),
        (StepState.FAILED, "Failed"),
        (StepState.SKIPPED, "Already up to date"),
    ):
        steps = [step for step in plan.steps if step.state is state]
        if not steps:
            continue
        ui.section(f"{label} ({len(steps)})")
        for step in steps:
            _render_step(step, verbose=verbose or state is StepState.CONFLICT)

    manual = [step for step in plan.manual_steps if step.state is StepState.PENDING]
    if manual:
        ui.section(f"Do these yourself ({len(manual)})")
        for step in manual:
            _render_step(step, verbose=verbose)

    if plan.notes:
        ui.section("Notes")
        for note in plan.notes:
            ui.bullet(note, style="muted", marker="\u00b7")

    if plan.sources:
        ui.section("Sources")
        for source in plan.sources:
            ui.note(source)

    if outcome is not None and outcome.ai_fixes:
        ui.section("AI repair")
        ui.note(f"{outcome.ai_fixes} repair round applied after the first validation failure")


def _render_step(step, *, verbose: bool) -> None:  # type: ignore[no-untyped-def]
    mark = STATE_MARK.get(step.state, ui.MARK_SKIP)
    style = RISK_STYLE[step.risk.value]
    ui.console().print(
        f"  {mark} {step.title}  [{style}]{step.risk.value}[/{style}]"
        f"  [muted]{step.id}[/muted]"
    )
    if step.file:
        ui.console().print(f"      [muted]{step.file}[/muted]")
    if step.detail:
        ui.console().print(f"      {step.detail}")
    if step.reason:
        ui.console().print(f"      [warn]{step.reason}[/warn]")
    if verbose and step.diff:
        for line in step.diff.splitlines()[:20]:
            ui.console().print(f"      [muted]{line}[/muted]")
    if verbose and step.source:
        ui.console().print(f"      [muted]source: {step.source}[/muted]")


def _count(value: int, style: str) -> str:
    return "[muted]0[/muted]" if value == 0 else f"[{style}]{value}[/{style}]"
