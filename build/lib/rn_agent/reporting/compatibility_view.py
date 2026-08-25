"""Renders ``rn-agent compatibility``.

One table per area, and the status column carries the whole message: ``ok``,
``conflict``, ``unknown``. Unknowns are counted in the summary and listed
compactly rather than hidden, because "we could not tell" is a result the
developer needs to see - it is where the manual checking goes.
"""

from __future__ import annotations

from ..cli import ui
from ..models.compatibility import CompatArea, CompatibilityReport, CompatStatus

AREA_TITLE = {
    CompatArea.RUNTIME: "Runtime",
    CompatArea.TOOLING: "Tooling",
    CompatArea.PLATFORM: "Platforms",
    CompatArea.DEPENDENCY: "Dependencies",
}

STATUS_STYLE = {
    CompatStatus.OK: "ok",
    CompatStatus.CONFLICT: "fail",
    CompatStatus.WARN: "warn",
    CompatStatus.UNKNOWN: "muted",
}


def render_compatibility(report: CompatibilityReport, *, verbose: bool = False) -> None:
    ui.header(
        f"Compatibility: React Native {report.current_rn or '?'} "
        f"\u2192 {report.target_rn or '?'}",
        report.target_source or "",
    )

    counts = report.counts()
    ui.section("Summary")
    ui.key_values(
        [
            ("checked", counts["checked"]),
            ("ok", f"[ok]{counts['ok']}[/ok]"),
            ("conflicts", _count(counts["conflicts"], "fail")),
            ("warnings", _count(counts["warnings"], "warn")),
            ("unknown", f"[muted]{counts['unknown']}[/muted]"),
            ("registry", "reachable" if report.registry_available else "[warn]not used[/warn]"),
        ]
    )

    for area in (CompatArea.RUNTIME, CompatArea.PLATFORM, CompatArea.TOOLING, CompatArea.DEPENDENCY):
        entries = report.by_area(area)
        if not entries:
            continue
        shown = entries if (verbose or area is not CompatArea.DEPENDENCY) else [
            entry for entry in entries if entry.status is not CompatStatus.OK
        ] or entries[:10]
        ui.table(
            ["Requirement", "Needs", "Have", "Status"],
            [
                [
                    entry.name,
                    entry.required or "[muted]unknown[/muted]",
                    entry.current or "[muted]unknown[/muted]",
                    f"[{STATUS_STYLE[entry.status]}]{entry.status.value}[/]",
                ]
                for entry in shown
            ],
            title=AREA_TITLE[area],
        )
        if len(entries) > len(shown):
            ui.note(f"{len(entries) - len(shown)} more row(s); --verbose shows them all")

    blockers = report.blockers
    if blockers:
        ui.section(f"Blockers ({len(blockers)})")
        for entry in blockers:
            ui.console().print(f"  {ui.MARK_FAIL} [fail]{entry.name}[/fail]  {entry.detail}")
            if entry.source:
                ui.console().print(f"      [muted]source: {entry.source}[/muted]")

    unknowns = report.unknowns
    if unknowns:
        ui.section(f"Could not be decided ({len(unknowns)})")
        listing = unknowns if verbose else unknowns[:8]
        for entry in listing:
            ui.console().print(f"  {ui.MARK_SKIP} {entry.name} [muted]{entry.detail}[/muted]")
        if len(unknowns) > len(listing):
            ui.note(f"{len(unknowns) - len(listing)} more; --verbose shows them all")

    if report.notes:
        ui.section("Notes")
        for note in report.notes:
            ui.bullet(note, style="muted", marker="\u00b7")


def _count(value: int, style: str) -> str:
    return "[muted]0[/muted]" if value == 0 else f"[{style}]{value}[/{style}]"
