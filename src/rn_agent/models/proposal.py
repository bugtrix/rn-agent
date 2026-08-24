"""What a model proposes to change.

The agent never applies a free-form patch. A model answers with *whole files*
(:class:`FileEdit`), grouped into :class:`Proposal` units the developer can
accept or skip one at a time, and every edit travels through ``FileManager`` so
it is path-checked, backed up and recorded like any other write.

Whole content rather than a diff is a deliberate safety choice: a hunk that
fails to apply cleanly leaves a half-edited file, while a full replacement is
either written or not - and the backup makes it reversible either way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .changes import RiskLevel
from .validation import ValidationReport


class EditAction(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class FileEdit(BaseModel):
    """One file the model wants to create, rewrite or remove."""

    model_config = ConfigDict(extra="ignore")

    path: str
    action: EditAction = EditAction.MODIFY
    #: Full new content. ``None`` is only valid for a delete.
    content: str | None = None
    reason: str = ""

    @property
    def is_delete(self) -> bool:
        return self.action is EditAction.DELETE

    @property
    def usable(self) -> bool:
        """A delete needs no content; anything else does."""
        return self.is_delete or self.content is not None

    @property
    def lines(self) -> int:
        return 0 if self.content is None else self.content.count("\n") + 1


class Proposal(BaseModel):
    """One coherent change: a fix, a feature slice, a generated test file."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    summary: str = ""
    edits: list[FileEdit] = Field(default_factory=list)
    #: Commands the developer should run. The agent prints them; it never runs
    #: something a model invented.
    commands: list[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.MEDIUM
    #: Finding ids (from `health` or `review`) this proposal addresses.
    addresses: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(edit.path for edit in self.edits)

    @property
    def usable_edits(self) -> list[FileEdit]:
        return [edit for edit in self.edits if edit.usable]


class ProposalSet(BaseModel):
    """Everything one model call proposed, with its accounting."""

    model_config = ConfigDict(extra="ignore")

    task: str
    proposals: list[Proposal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def edits(self) -> list[FileEdit]:
        return [edit for proposal in self.proposals for edit in proposal.usable_edits]

    @property
    def paths(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for edit in self.edits:
            seen.setdefault(edit.path, None)
        return tuple(seen)

    @property
    def highest_risk(self) -> RiskLevel:
        if not self.proposals:
            return RiskLevel.LOW
        return max((proposal.risk for proposal in self.proposals), key=lambda risk: risk.rank)

    def __len__(self) -> int:
        return len(self.proposals)

    def counts(self) -> dict[str, int]:
        return {
            "proposals": len(self.proposals),
            "files": len(self.paths),
            "created": sum(1 for edit in self.edits if edit.action is EditAction.CREATE),
            "modified": sum(1 for edit in self.edits if edit.action is EditAction.MODIFY),
            "deleted": sum(1 for edit in self.edits if edit.action is EditAction.DELETE),
        }


class RefusedEdit(BaseModel):
    """An edit the project's rules rejected, and which rule did it."""

    model_config = ConfigDict(extra="ignore")

    path: str
    rule: str
    detail: str = ""


class EditRunReport(BaseModel):
    """The record of one write-command run (``fix``, ``feature``, ``test``, ``docs``).

    One shape for all four, so ``--json`` and the report file look the same
    whichever command produced them - and so "what was refused" and "was it
    validated" can never be dropped by a command that forgot to report them.
    """

    model_config = ConfigDict(extra="ignore")

    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    task: str
    #: What the developer asked for: issue ids, a description, target paths.
    subject: list[str] = Field(default_factory=list)
    #: Requested issue ids that no earlier run had recorded.
    unknown_issues: list[str] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    refused: list[RefusedEdit] = Field(default_factory=list)
    applied: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    #: ``None`` means no validation ran - which is not the same as passing.
    validation: ValidationReport | None = None
    rolled_back: bool = False
    dry_run: bool = False
    notes: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)

    @property
    def validated(self) -> bool | None:
        return None if self.validation is None else self.validation.ok

    def counts(self) -> dict[str, int]:
        return {
            "proposals": len(self.proposals),
            "refused": len(self.refused),
            "applied": len(self.applied),
            "unchanged": len(self.unchanged),
        }
