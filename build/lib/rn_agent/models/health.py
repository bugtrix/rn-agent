"""Health check results.

Every check carries its *evidence* and its *source* so a developer can verify
the finding by hand. A check whose facts are unavailable reports ``SKIP`` - the
agent never converts missing data into a scary warning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ..constants import HEALTH_SCORE_MAX, SEVERITY_PENALTY


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
            Severity.INFO: 4,
        }
        return order[self]


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class Category(StrEnum):
    PROJECT = "project"
    REACT_NATIVE = "react-native"
    JAVASCRIPT = "javascript"
    ANDROID = "android"
    IOS = "ios"
    GIT = "git"


class HealthCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    category: Category
    title: str
    status: CheckStatus
    severity: Severity = Severity.INFO
    detail: str = ""
    recommendation: str | None = None
    evidence: dict[str, str] = Field(default_factory=dict)
    source: str | None = None
    docs: str | None = None

    @property
    def is_problem(self) -> bool:
        return self.status in (CheckStatus.FAIL, CheckStatus.WARN)

    @property
    def penalty(self) -> int:
        if not self.is_problem:
            return 0
        return SEVERITY_PENALTY.get(self.severity.value, 0)


class HealthReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    project_root: str = ""
    rn_version: str | None = None
    checks: list[HealthCheck] = Field(default_factory=list)
    deep: bool = False

    # -- derived -----------------------------------------------------------
    @property
    def problems(self) -> list[HealthCheck]:
        return sorted(
            (check for check in self.checks if check.is_problem),
            key=lambda check: (check.severity.rank, check.category.value, check.id),
        )

    def by_severity(self, severity: Severity) -> list[HealthCheck]:
        return [check for check in self.problems if check.severity is severity]

    @property
    def critical(self) -> list[HealthCheck]:
        return self.by_severity(Severity.CRITICAL)

    @property
    def warnings(self) -> list[HealthCheck]:
        return [
            check
            for check in self.problems
            if check.severity in (Severity.HIGH, Severity.MEDIUM, Severity.LOW)
        ]

    @property
    def passed(self) -> list[HealthCheck]:
        return [check for check in self.checks if check.status is CheckStatus.PASS]

    @property
    def skipped(self) -> list[HealthCheck]:
        return [check for check in self.checks if check.status is CheckStatus.SKIP]

    @property
    def score(self) -> int:
        """100 minus the summed severity penalties, clamped to 0.

        Deterministic and explainable: every point lost maps to a listed check.
        """
        penalty = sum(check.penalty for check in self.checks)
        return max(0, HEALTH_SCORE_MAX - penalty)

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
            "checks": len(self.checks),
            "passed": len(self.passed),
            "skipped": len(self.skipped),
            "critical": len(self.by_severity(Severity.CRITICAL)),
            "high": len(self.by_severity(Severity.HIGH)),
            "medium": len(self.by_severity(Severity.MEDIUM)),
            "low": len(self.by_severity(Severity.LOW)),
        }

    def categories(self) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for check in self.checks:
            bucket = summary.setdefault(
                check.category.value, {"total": 0, "problems": 0, "skipped": 0}
            )
            bucket["total"] += 1
            if check.is_problem:
                bucket["problems"] += 1
            if check.status is CheckStatus.SKIP:
                bucket["skipped"] += 1
        return summary

    def recommendations(self) -> list[str]:
        seen: dict[str, None] = {}
        for check in self.problems:
            if check.recommendation:
                seen.setdefault(check.recommendation, None)
        return list(seen)
