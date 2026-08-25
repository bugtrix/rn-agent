"""Renders ``rn-agent release``.

The version table comes first and lists every file that carries a version, so a
missing Android or iOS row is visible rather than implied. The changelog is
shown with its source labelled - written by a model, or the commit subjects
themselves - because a developer signing off release notes should know which.
"""

from __future__ import annotations

from ..cli import ui
from ..models.release import ReleasePlan


def render_release(plan: ReleasePlan, *, verbose: bool = False) -> None:
    ui.header(
        f"Release {plan.current_version or '?'} \u2192 {plan.next_version or '?'}",
        f"{plan.bump.value} \u00b7 {len(plan.commits)} commit(s) since "
        f"{plan.previous_tag or 'the first commit'}",
    )

    changes = plan.effective_changes
    if changes:
        ui.table(
            ["File", "Field", "Current", "Next"],
            [
                [change.file, change.label, change.current or "-", change.next or "-"]
                for change in changes
            ],
            title="Versions",
        )
    else:
        ui.section("Versions")
        ui.warning("no version field was found to update")

    if plan.changelog:
        ui.section(f"Changelog ({plan.changelog_source})")
        for entry in plan.changelog[:20]:
            ui.bullet(entry, style="info", marker="-")
        if len(plan.changelog) > 20:
            ui.note(f"{len(plan.changelog) - 20} more entr(ies)")
    elif plan.commits:
        ui.section("Changelog")
        ui.note("changelog generation was skipped (--no-changelog)")

    if verbose and plan.commits:
        ui.section(f"Commits ({len(plan.commits)})")
        for subject in plan.commits[:30]:
            ui.note(subject)

    if plan.blockers:
        ui.section(f"Blockers ({len(plan.blockers)})")
        for blocker in plan.blockers:
            ui.console().print(f"  {ui.MARK_FAIL} [fail]{blocker}[/fail]")

    if plan.notes:
        ui.section("Notes")
        for note in plan.notes:
            ui.bullet(note, style="muted", marker="\u00b7")

    if plan.checklist:
        ui.section("Then, by hand")
        for step in plan.checklist:
            ui.bullet(step, style="info", marker="\u2192")
        ui.note("rn-agent never commits, tags or pushes for you")
