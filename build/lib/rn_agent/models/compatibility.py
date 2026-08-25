"""Can this project run on that React Native version?

The report is a matrix of requirements against what the project actually has.
Each row records where the requirement came from - the target's own
``peerDependencies`` when a registry answered, the curated offline table
otherwise - and rows the agent cannot decide are ``UNKNOWN``, never a guess.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CompatStatus(StrEnum):
    OK = "ok"
    CONFLICT = "conflict"
    WARN = "warn"
    UNKNOWN = "unknown"


class CompatArea(StrEnum):
    RUNTIME = "runtime"
    TOOLING = "tooling"
    PLATFORM = "platform"
    DEPENDENCY = "dependency"


class CompatibilityEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    area: CompatArea = CompatArea.DEPENDENCY
    required: str | None = None
    current: str | None = None
    status: CompatStatus = CompatStatus.UNKNOWN
    detail: str = ""
    source: str | None = None
    confidence: str = "medium"

    @property
    def blocking(self) -> bool:
        return self.status is CompatStatus.CONFLICT


class CompatibilityReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    current_rn: str | None = None
    target_rn: str | None = None
    target_source: str | None = None
    entries: list[CompatibilityEntry] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    registry_available: bool = False

    def by_area(self, area: CompatArea) -> list[CompatibilityEntry]:
        return [entry for entry in self.entries if entry.area is area]

    @property
    def blockers(self) -> list[CompatibilityEntry]:
        return [entry for entry in self.entries if entry.blocking]

    @property
    def warnings(self) -> list[CompatibilityEntry]:
        return [entry for entry in self.entries if entry.status is CompatStatus.WARN]

    @property
    def unknowns(self) -> list[CompatibilityEntry]:
        return [entry for entry in self.entries if entry.status is CompatStatus.UNKNOWN]

    @property
    def ready(self) -> bool:
        """No conflict found. Unknowns do not block - they are reported."""
        return not self.blockers

    def counts(self) -> dict[str, int]:
        return {
            "checked": len(self.entries),
            "ok": sum(1 for entry in self.entries if entry.status is CompatStatus.OK),
            "conflicts": len(self.blockers),
            "warnings": len(self.warnings),
            "unknown": len(self.unknowns),
        }
