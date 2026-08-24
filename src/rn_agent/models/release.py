"""Preparing a release: version numbers, in every place that carries one.

A React Native app states its version in three places that drift apart -
``package.json``, ``android/app/build.gradle`` (``versionName`` /
``versionCode``) and the iOS project (``MARKETING_VERSION`` /
``CURRENT_PROJECT_VERSION``). The plan lists every one it found, with the
current and next value, so the developer sees the whole set before anything is
written and can tell when a platform was silently left behind.

Tagging and publishing are deliberately absent: ``GitManager`` implements no
destructive or history-writing operation, so ``release`` prepares the commit and
prints the git commands rather than running them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class BumpKind(StrEnum):
    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"
    EXPLICIT = "explicit"


class VersionChange(BaseModel):
    """One version field, in one file."""

    model_config = ConfigDict(extra="ignore")

    file: str
    label: str
    current: str | None = None
    next: str | None = None

    @property
    def changed(self) -> bool:
        return self.next is not None and self.next != self.current


class ReleasePlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    bump: BumpKind = BumpKind.PATCH
    current_version: str | None = None
    next_version: str | None = None
    changes: list[VersionChange] = Field(default_factory=list)
    #: Commits since the previous release tag (subject lines).
    commits: list[str] = Field(default_factory=list)
    previous_tag: str | None = None
    #: Changelog lines: written by the model when AI is configured, otherwise
    #: the commit subjects themselves - and the report says which.
    changelog: list[str] = Field(default_factory=list)
    changelog_source: str = "commits"
    #: Health/git problems that should stop a release.
    blockers: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.blockers and self.next_version is not None

    @property
    def effective_changes(self) -> list[VersionChange]:
        return [change for change in self.changes if change.changed]

    def counts(self) -> dict[str, int]:
        return {
            "files": len(self.effective_changes),
            "commits": len(self.commits),
            "changelog": len(self.changelog),
            "blockers": len(self.blockers),
        }
