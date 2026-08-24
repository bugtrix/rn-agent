"""Renders ``rn-agent upgrade``.

Risk first, alphabetical second: the table is sorted so the change most likely
to break a build is the first thing read, and every row carries *why* - the
version jump, the native code, the peer conflict. A blocked candidate is shown
with its reason rather than hidden, because "why did it not upgrade X" is the
question this command exists to answer.
"""

from __future__ import annotations

from ..cli import ui
from ..models.upgrade import ChangeKind, UpgradePlan

RISK_STYLE = {"low": "low", "medium": "medium", "high": "high", "critical": "critical"}
CHANGE_STYLE = {
    ChangeKind.MAJOR: "high",
    ChangeKind.MINOR: "medium",
    ChangeKind.PATCH: "low",
    ChangeKind.NONE: "muted",
    ChangeKind.UNKNOWN: "muted",
}


def render_upgrade(plan: UpgradePlan, *, verbose: bool = False) -> None:
    counts = plan.counts()
    ui.header(
        f"Upgrade ({plan.policy})",
        f"{counts['selected']} of {counts['candidates']} package(s) would change",
    )

    ui.section("Summary")
    ui.key_values(
        [
            ("packages checked", counts["candidates"]),
            ("would upgrade", counts["selected"]),
            ("major", _count(counts["major"], "high")),
            ("minor", _count(counts["minor"], "medium")),
            ("patch", _count(counts["patch"], "low")),
            ("native", _count(counts["native"], "high")),
            ("blocked", _count(counts["blocked"], "warn")),
            ("registry", "reachable" if plan.registry_available else "[warn]unreachable[/warn]"),
        ]
    )

    selected = plan.selected
    if selected:
        ui.table(
            ["Package", "Current", "Target", "Change", "Risk", "Native", "Why"],
            [
                [
                    candidate.name,
                    candidate.installed or candidate.declared or "-",
                    candidate.spec or candidate.target or "-",
                    f"[{CHANGE_STYLE[candidate.change]}]{candidate.change.value}[/]",
                    f"[{RISK_STYLE[candidate.risk.value]}]{candidate.risk.value}[/]",
                    "yes" if candidate.native else "",
                    candidate.reasons[0] if candidate.reasons else "",
                ]
                for candidate in sorted(
                    selected,
                    key=lambda item: (-item.risk.rank, -item.change.rank, item.name),
                )
            ],
            title="Would upgrade",
        )

    blocked = plan.blocked
    if blocked:
        ui.section(f"Not upgraded ({len(blocked)})")
        shown = blocked if verbose else blocked[:12]
        for candidate in shown:
            ui.console().print(
                f"  {ui.MARK_SKIP} {candidate.name} "
                f"[muted]{candidate.installed or candidate.declared or '?'}[/muted]"
            )
            ui.console().print(f"      [warn]{candidate.blocked_reason or 'blocked'}[/warn]")
            for conflict in candidate.peer_conflicts:
                ui.console().print(f"      [muted]{conflict}[/muted]")
        if len(blocked) > len(shown):
            ui.note(f"{len(blocked) - len(shown)} more; run with --verbose to see them")

    if verbose and selected:
        ui.section("Reasons")
        for candidate in selected:
            for reason in candidate.reasons:
                ui.console().print(f"  [muted]{candidate.name}: {reason}[/muted]")

    if plan.notes:
        ui.section("Notes")
        for note in plan.notes:
            ui.bullet(note, style="muted", marker="\u00b7")


def _count(value: int, style: str) -> str:
    return "[muted]0[/muted]" if value == 0 else f"[{style}]{value}[/{style}]"
