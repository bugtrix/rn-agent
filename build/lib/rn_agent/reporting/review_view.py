"""Renders ``rn-agent review``.

Deliberately shaped like the health report: same score meaning, same severity
colours, same "evidence under the finding" layout. A developer who has read one
can read the other, and the two scores are comparable because they use the same
penalty table.
"""

from __future__ import annotations

from ..agents.context_builder import PromptContext
from ..cli import ui
from ..models.health import Severity
from ..models.review import ReviewReport
from .change_view import render_context, render_usage


def render_review(
    report: ReviewReport,
    *,
    context: PromptContext | None = None,
    usage: dict[str, int] | None = None,
    verbose: bool = False,
) -> None:
    score = report.score
    ui.header(
        f"Review: {score}/100",
        f"{report.grade} \u00b7 {len(report.files_reviewed)} file(s) \u00b7 "
        f"{report.model or 'model unknown'}",
    )

    counts = report.counts()
    ui.section("Summary")
    ui.key_values(
        [
            ("findings", counts["findings"]),
            ("critical", _count(counts["critical"], "critical")),
            ("high", _count(counts["high"], "high")),
            ("medium", _count(counts["medium"], "medium")),
            ("low", _count(counts["low"], "low")),
            ("info", _count(counts["info"], "info")),
            ("files reviewed", counts["files"]),
            ("score", f"[{ui.score_style(score)}]{score}/100[/{ui.score_style(score)}]"),
        ]
    )

    areas = report.by_area()
    if areas:
        ui.table(
            ["Area", "Findings"],
            [[name, count] for name, count in areas.items()],
            title="By area",
        )

    for severity, label in (
        (Severity.CRITICAL, "Critical"),
        (Severity.HIGH, "High"),
        (Severity.MEDIUM, "Medium"),
        (Severity.LOW, "Low"),
        (Severity.INFO, "Info"),
    ):
        findings = [
            finding for finding in report.sorted_findings if finding.severity is severity
        ]
        if not findings:
            continue
        ui.section(f"{label} ({len(findings)})")
        for finding in findings:
            mark = (
                ui.MARK_FAIL
                if severity in (Severity.CRITICAL, Severity.HIGH)
                else ui.MARK_WARN
            )
            style = ui.SEVERITY_STYLE[severity.value]
            ui.console().print(
                f"  {mark} [{style}]{finding.title}[/{style}]  [muted]{finding.id}[/muted]"
            )
            ui.console().print(f"      [muted]{finding.location} \u00b7 {finding.area}[/muted]")
            if finding.detail:
                ui.console().print(f"      {finding.detail}")
            if finding.recommendation:
                ui.console().print(f"      [info]{ui.ARROW} {finding.recommendation}[/info]")
            if verbose:
                if finding.snippet:
                    for line in finding.snippet.splitlines()[:6]:
                        ui.console().print(f"      [muted]| {line}[/muted]")
                ui.console().print(f"      [muted]confidence: {finding.confidence}[/muted]")

    if report.notes:
        ui.section("Notes")
        for note in report.notes:
            ui.bullet(note, style="muted", marker="\u00b7")

    if context is not None:
        render_context(context, verbose=verbose)
    if usage:
        render_usage(usage, model=report.model, provider=report.provider)

    ui.blank()
    if counts["critical"]:
        ui.failure(f"{counts['critical']} critical finding(s) to address")
    elif counts["high"]:
        ui.warning(f"{counts['high']} high-severity finding(s) found")
    elif counts["findings"]:
        ui.success(f"{counts['findings']} finding(s), none critical")
    else:
        ui.success("no findings in the reviewed files")
    ui.note("`rn-agent fix --issue <id>` fixes a finding by id")


def _count(value: int, style: str) -> str:
    return "[muted]0[/muted]" if value == 0 else f"[{style}]{value}[/{style}]"
