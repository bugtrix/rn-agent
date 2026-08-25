"""Risk-ranked dependency upgrades.

Every field here is a *fact plus its provenance*: the declared range comes from
``package.json``, the installed version from ``node_modules``, the available
versions from the registry, and the peer conflicts from real package metadata
run through the semver engine. Where a fact is missing the field is ``None`` and
the candidate is reported as unknown rather than guessed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .changes import RiskLevel
from .project import DependencyKind


class ChangeKind(StrEnum):
    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"
    NONE = "none"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return {
            ChangeKind.NONE: 0,
            ChangeKind.PATCH: 1,
            ChangeKind.MINOR: 2,
            ChangeKind.MAJOR: 3,
            ChangeKind.UNKNOWN: 4,
        }[self]


class UpgradeCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    kind: DependencyKind = DependencyKind.PROD
    declared: str | None = None
    installed: str | None = None
    #: Newest version the registry offers at all.
    latest: str | None = None
    #: What this run would write, given the target policy.
    target: str | None = None
    change: ChangeKind = ChangeKind.UNKNOWN
    native: bool = False
    risk: RiskLevel = RiskLevel.LOW
    reasons: list[str] = Field(default_factory=list)
    peer_conflicts: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_reason: str | None = None
    source: str | None = None

    @property
    def actionable(self) -> bool:
        return bool(self.target) and not self.blocked and self.change is not ChangeKind.NONE

    @property
    def spec(self) -> str | None:
        """The range to write, preserving the declared operator (``^``/``~``)."""
        if self.target is None:
            return None
        prefix = ""
        if self.declared and self.declared[:1] in {"^", "~"}:
            prefix = self.declared[0]
        return f"{prefix}{self.target}"


class UpgradePlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    policy: str = "minor"
    candidates: list[UpgradeCandidate] = Field(default_factory=list)
    #: False when the registry could not be reached; the plan then reports only
    #: what installed metadata proves, instead of inventing target versions.
    registry_available: bool = True
    install_command: str | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def selected(self) -> list[UpgradeCandidate]:
        return [candidate for candidate in self.candidates if candidate.actionable]

    @property
    def blocked(self) -> list[UpgradeCandidate]:
        return [candidate for candidate in self.candidates if candidate.blocked]

    @property
    def sorted_candidates(self) -> list[UpgradeCandidate]:
        return sorted(
            self.candidates,
            key=lambda candidate: (-candidate.risk.rank, -candidate.change.rank, candidate.name),
        )

    @property
    def highest_risk(self) -> RiskLevel:
        selected = self.selected
        if not selected:
            return RiskLevel.LOW
        return max((candidate.risk for candidate in selected), key=lambda risk: risk.rank)

    def counts(self) -> dict[str, int]:
        selected = self.selected
        return {
            "candidates": len(self.candidates),
            "selected": len(selected),
            "blocked": len(self.blocked),
            "major": sum(1 for item in selected if item.change is ChangeKind.MAJOR),
            "minor": sum(1 for item in selected if item.change is ChangeKind.MINOR),
            "patch": sum(1 for item in selected if item.change is ChangeKind.PATCH),
            "native": sum(1 for item in selected if item.native),
        }
