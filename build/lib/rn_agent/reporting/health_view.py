"""Renders the result of ``rn-agent health`` (§15)."""

from __future__ import annotations

from ..cli import ui
from ..models.health import HealthReport, Severity

CATEGORY_LABELS = {
    "project": "Project",
    "react-native": "React Native",
    "javascript": "JavaScript",
    "android": "Android",
    "ios": "iOS",
    "git": "Git",
}


def render_health(report: HealthReport, *, verbose: bool = False) -> None:
    score = report.score
    style = ui.score_style(score)
    ui.header(
        f"Health Score: {score}/100",
        f"{report.grade} \u00b7 React Native {report.rn_version or 'unknown'}",
    )

    counts = report.counts()
    ui.section("Summary")
    ui.key_values(
        [
            ("checks run", counts["checks"]),
            ("passed", f"[ok]{counts['passed']}[/ok]"),
            ("critical", _count_style(counts["critical"], "critical")),
            ("high", _count_style(counts["high"], "high")),
            ("medium", _count_style(counts["medium"], "medium")),
            ("low", _count_style(counts["low"], "low")),
            ("skipped", f"[muted]{counts['skipped']}[/muted]"),
            ("score", f"[{style}]{score}/100[/{style}]"),
        ]
    )

    categories = report.categories()
    ui.table(
        ["Area", "Checks", "Problems", "Skipped"],
        [
            [
                CATEGORY_LABELS.get(name, name),
                data["total"],
                data["problems"] or "",
                data["skipped"] or "",
            ]
            for name, data in sorted(categories.items())
        ],
        title="By area",
    )

    for severity, label in (
        (Severity.CRITICAL, "Critical"),
        (Severity.HIGH, "High"),
        (Severity.MEDIUM, "Medium"),
        (Severity.LOW, "Low"),
    ):
        problems = report.by_severity(severity)
        if not problems:
            continue
        ui.section(f"{label} ({len(problems)})")
        for check in problems:
            marker = ui.MARK_FAIL if severity in (Severity.CRITICAL, Severity.HIGH) else ui.MARK_WARN
            ui.console().print(
                f"  {marker} [{ui.SEVERITY_STYLE[severity.value]}]{check.title}[/]"
                f"  [muted]{check.id}[/muted]"
            )
            ui.indented(check.detail)
            if check.recommendation:
                ui.indented(f"{ui.ARROW} {check.recommendation}", style="info")
            for line in check.fix:
                # The exact text to paste, so nobody has to reconstruct syntax
                # from prose.
                ui.code(line)
            if verbose:
                for key, value in check.evidence.items():
                    ui.code(f"{key}: {value}", indent=6, style="muted")
                if check.source:
                    ui.code(f"source: {check.source}", indent=6, style="muted")
                if check.docs:
                    ui.code(f"docs: {check.docs}", indent=6, style="muted")

    if verbose:
        passed = report.passed
        if passed:
            ui.section(f"Passed ({len(passed)})")
            for check in passed:
                ui.console().print(f"  {ui.MARK_OK} {check.title} [muted]{check.detail}[/muted]")
        skipped = report.skipped
        if skipped:
            ui.section(f"Skipped ({len(skipped)})")
            for check in skipped:
                ui.console().print(f"  {ui.MARK_SKIP} {check.title} [muted]{check.detail}[/muted]")

    recommendations = report.recommendations()
    if recommendations:
        ui.section("Recommendations")
        for recommendation in recommendations[:10]:
            ui.bullet(recommendation)

    ui.blank()
    if counts["critical"]:
        ui.failure(f"{counts['critical']} critical issue(s) need attention before your next build")
    elif counts["high"]:
        ui.warning(f"{counts['high']} high-severity issue(s) found")
    else:
        ui.success("no critical or high-severity issues found")
    if not report.deep:
        ui.note("run `rn-agent health --deep` to also run tsc and eslint")


def _count_style(value: int, style: str) -> str:
    if value == 0:
        return "[muted]0[/muted]"
    return f"[{style}]{value}[/{style}]"
