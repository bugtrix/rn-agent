"""What ``rn-agent review`` found.

Findings reuse the health :class:`~rn_agent.models.health.Severity` and the same
penalty table, so "review score" and "health score" mean the same thing to a
developer: every lost point maps to one listed finding.

A finding carries the file (and line, when the model gave one) so it can be
verified by hand, plus the model's own confidence - a review is an opinion, and
the report says so.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from ..constants import HEALTH_SCORE_MAX, SEVERITY_PENALTY
from .health import Severity

#: Areas a review may report on. Anything else is normalised to "other", so a
#: creative model cannot invent a new taxonomy in the report.
REVIEW_AREAS: tuple[str, ...] = (
    "architecture",
    "components",
    "hooks",
    "state",
    "navigation",
    "performance",
    "types",
    "native",
    "testing",
    "security",
    "accessibility",
    "other",
)

CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    severity: Severity = Severity.MEDIUM
    area: str = "other"
    file: str | None = None
    line: int | None = None
    detail: str = ""
    recommendation: str | None = None
    #: The exact code the finding is about, when the model quoted it.
    snippet: str | None = None
    confidence: str = "medium"

    @property
    def penalty(self) -> int:
        return SEVERITY_PENALTY.get(self.severity.value, 0)

    @property
    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file or "project"


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    project_root: str = ""
    rn_version: str | None = None
    provider: str | None = None
    model: str | None = None
    files_reviewed: list[str] = Field(default_factory=list)
    files_skipped: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def sorted_findings(self) -> list[ReviewFinding]:
        return sorted(
            self.findings, key=lambda finding: (finding.severity.rank, finding.area, finding.id)
        )

    def by_severity(self, severity: Severity) -> list[ReviewFinding]:
        return [finding for finding in self.findings if finding.severity is severity]

    def by_area(self) -> dict[str, int]:
        areas: dict[str, int] = {}
        for finding in self.findings:
            areas[finding.area] = areas.get(finding.area, 0) + 1
        return dict(sorted(areas.items()))

    @property
    def score(self) -> int:
        """Same rule as ``health``: 100 minus the summed severity penalties."""
        return max(0, HEALTH_SCORE_MAX - sum(finding.penalty for finding in self.findings))

    @property
    def grade(self) -> str:
        score = self.score
        if score >= 90:
            return "excellent"
        if score >= 75:
            return "good"
        if score >= 50:
            return "needs attention"
        return "at risk"

    def counts(self) -> dict[str, int]:
        return {
            "findings": len(self.findings),
            "files": len(self.files_reviewed),
            "critical": len(self.by_severity(Severity.CRITICAL)),
            "high": len(self.by_severity(Severity.HIGH)),
            "medium": len(self.by_severity(Severity.MEDIUM)),
            "low": len(self.by_severity(Severity.LOW)),
            "info": len(self.by_severity(Severity.INFO)),
        }
