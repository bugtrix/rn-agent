"""Change tracking.

Requirement §13: every modification records file, before, after, reason,
command and risk. Phase 1 populates this through :class:`FileManager`; later
phases (fix/feature/migrate) reuse it for reporting and rollback.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
            RiskLevel.CRITICAL: 3,
        }[self]

    @property
    def auto_applicable(self) -> bool:
        """Only low-risk changes may ever be applied without confirmation."""
        return self is RiskLevel.LOW


class ChangeType(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


def digest(content: str | None) -> str | None:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class FileChange(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    change_type: ChangeType
    reason: str
    command: str
    risk: RiskLevel = RiskLevel.MEDIUM
    before_hash: str | None = None
    after_hash: str | None = None
    before_bytes: int | None = None
    after_bytes: int | None = None
    backup: str | None = None
    applied: bool = False
    dry_run: bool = False
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    @property
    def name(self) -> str:
        return Path(self.path).name


class ChangeSet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: str
    changes: list[FileChange] = Field(default_factory=list)
    dry_run: bool = False

    def add(self, change: FileChange) -> FileChange:
        self.changes.append(change)
        return change

    @property
    def applied(self) -> list[FileChange]:
        return [change for change in self.changes if change.applied]

    @property
    def created(self) -> list[FileChange]:
        return [change for change in self.changes if change.change_type is ChangeType.CREATE]

    @property
    def modified(self) -> list[FileChange]:
        return [change for change in self.changes if change.change_type is ChangeType.MODIFY]

    @property
    def highest_risk(self) -> RiskLevel:
        if not self.changes:
            return RiskLevel.LOW
        return max((change.risk for change in self.changes), key=lambda risk: risk.rank)

    @property
    def rollback_available(self) -> bool:
        return any(change.backup for change in self.applied)

    def __len__(self) -> int:
        return len(self.changes)
