"""React Native version migration: the plan, and what happened to it.

A migration is a list of small, individually reversible steps, each carrying
where it came from (``source``) and what it will touch (``file``). Steps whose
context no longer matches the upstream template are not forced: they end up
``CONFLICT`` and are reported for the developer to do by hand, because a
half-applied ``.pbxproj`` is worse than an honest "do this yourself".
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .changes import RiskLevel
from .validation import ValidationReport


class StepKind(StrEnum):
    DEPENDENCY = "dependency"
    ANDROID = "android"
    IOS = "ios"
    JAVASCRIPT = "javascript"
    MANUAL = "manual"


class StepState(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    SKIPPED = "skipped"
    CONFLICT = "conflict"
    FAILED = "failed"


class MigrationStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    kind: StepKind
    title: str
    file: str | None = None
    detail: str = ""
    risk: RiskLevel = RiskLevel.MEDIUM
    #: Where the instruction came from: an upstream diff URL, a local rules
    #: file, or the project's own package.json.
    source: str | None = None
    state: StepState = StepState.PENDING
    #: The upstream hunk this step would apply, when there is one.
    diff: str | None = None
    #: Populated when the step could not be applied automatically.
    reason: str | None = None
    #: The machine-readable edit this step will make - dependency name to
    #: version for a dependency step, rule fields for a rule step. Kept apart
    #: from ``detail`` so applying a step never has to re-parse prose.
    payload: dict[str, str] = Field(default_factory=dict)

    @property
    def automatic(self) -> bool:
        return self.kind is not StepKind.MANUAL


class MigrationPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    from_version: str | None = None
    to_version: str | None = None
    steps: list[MigrationStep] = Field(default_factory=list)
    branch: str | None = None
    sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    #: True when the upstream diff could not be fetched, so the plan is limited
    #: to what local rules and package.json can prove.
    offline: bool = False

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for step in self.steps:
            counts[step.kind.value] = counts.get(step.kind.value, 0) + 1
        return counts

    @property
    def automatic_steps(self) -> list[MigrationStep]:
        return [step for step in self.steps if step.automatic]

    @property
    def manual_steps(self) -> list[MigrationStep]:
        return [step for step in self.steps if not step.automatic]

    @property
    def highest_risk(self) -> RiskLevel:
        if not self.steps:
            return RiskLevel.LOW
        return max((step.risk for step in self.steps), key=lambda risk: risk.rank)

    def counts(self) -> dict[str, int]:
        return {
            "steps": len(self.steps),
            "automatic": len(self.automatic_steps),
            "manual": len(self.manual_steps),
            "applied": sum(1 for step in self.steps if step.state is StepState.APPLIED),
            "conflict": sum(1 for step in self.steps if step.state is StepState.CONFLICT),
            "failed": sum(1 for step in self.steps if step.state is StepState.FAILED),
            "skipped": sum(1 for step in self.steps if step.state is StepState.SKIPPED),
        }


class MigrationOutcome(BaseModel):
    """The record written to ``.rn-agent/migration-history.json``."""

    model_config = ConfigDict(extra="ignore")

    finished_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    from_version: str | None = None
    to_version: str | None = None
    branch: str | None = None
    applied: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    manual: list[str] = Field(default_factory=list)
    validation: ValidationReport | None = None
    rolled_back: bool = False
    ai_fixes: int = 0
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        if self.rolled_back:
            return False
        return self.validation is None or self.validation.ok
