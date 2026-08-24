"""File manager: the only writer in the agent.

Guarantees:

* every write stays inside the project root (path-traversal proof);
* every modification is backed up under ``.rn-agent/cache/backups/<run>/``
  before the new bytes land, so ``rollback()`` restores byte-for-byte;
* every modification is recorded as a :class:`FileChange` (file, before,
  after, reason, command, risk) per requirement §13;
* ``dry_run`` records the intent and writes nothing.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..core.logging import get_logger
from ..core.paths import AgentPaths
from ..errors import UnsafePathError
from ..models.changes import ChangeSet, ChangeType, FileChange, RiskLevel, digest
from ..utils.io import atomic_write_text, read_text


def _run_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S")


@dataclass(slots=True)
class FileManager:
    """Reads and writes project files under a safety envelope."""

    paths: AgentPaths
    command: str = "unknown"
    dry_run: bool = False
    create_backups: bool = True
    logger: logging.Logger = field(default_factory=lambda: get_logger("filesystem"))
    changes: ChangeSet = field(init=False)
    _run_id: str = field(init=False, default_factory=_run_stamp)

    def __post_init__(self) -> None:
        self.changes = ChangeSet(command=self.command, dry_run=self.dry_run)

    # -- path safety -------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.paths.project_root

    def resolve(self, path: str | os.PathLike[str]) -> Path:
        """Absolute path inside the project, or :class:`UnsafePathError`."""
        candidate = Path(path)
        absolute = candidate if candidate.is_absolute() else self.root / candidate
        normalised = Path(os.path.normpath(absolute))
        if normalised != self.root and self.root not in normalised.parents:
            raise UnsafePathError(
                f"refusing to touch {normalised}: outside the project root",
                hint=f"Project root is {self.root}",
            )
        return normalised

    def relative(self, path: Path) -> str:
        return self.paths.relative(path)

    # -- reads -------------------------------------------------------------
    def read(self, path: str | os.PathLike[str]) -> str | None:
        return read_text(self.resolve(path))

    def exists(self, path: str | os.PathLike[str]) -> bool:
        try:
            return self.resolve(path).exists()
        except UnsafePathError:
            return False

    # -- writes ------------------------------------------------------------
    def write(
        self,
        path: str | os.PathLike[str],
        content: str,
        *,
        reason: str,
        risk: RiskLevel = RiskLevel.MEDIUM,
    ) -> FileChange:
        """Create or modify a file, recording (and backing up) the change."""
        target = self.resolve(path)
        before = read_text(target) if target.exists() else None
        change_type = ChangeType.MODIFY if before is not None else ChangeType.CREATE

        if before is not None and before == content:
            change = FileChange(
                path=self.relative(target),
                change_type=change_type,
                reason=f"{reason} (no change needed)",
                command=self.command,
                risk=RiskLevel.LOW,
                before_hash=digest(before),
                after_hash=digest(content),
                before_bytes=len(before.encode()),
                after_bytes=len(content.encode()),
                applied=False,
                dry_run=self.dry_run,
            )
            return self.changes.add(change)

        backup: Path | None = None
        if not self.dry_run:
            if before is not None and self.create_backups:
                backup = self._backup(target)
            atomic_write_text(target, content)

        change = FileChange(
            path=self.relative(target),
            change_type=change_type,
            reason=reason,
            command=self.command,
            risk=risk,
            before_hash=digest(before),
            after_hash=digest(content),
            before_bytes=len(before.encode()) if before is not None else None,
            after_bytes=len(content.encode()),
            backup=str(backup) if backup else None,
            applied=not self.dry_run,
            dry_run=self.dry_run,
        )
        self.logger.info(
            "%s %s (%s, risk=%s)",
            "would write" if self.dry_run else "wrote",
            change.path,
            change_type.value,
            risk.value,
        )
        return self.changes.add(change)

    def write_state(
        self, path: Path, content: str, *, reason: str
    ) -> FileChange | None:
        """Write agent-owned state under ``.rn-agent`` (not project source).

        These writes are not part of the developer-facing change set: they are
        the agent's own bookkeeping, and they are skipped entirely in dry-run.
        """
        if self.dry_run:
            self.logger.info("dry-run: would update agent state %s", self.relative(path))
            return None
        self.paths.ensure()
        atomic_write_text(path, content)
        self.logger.debug("updated agent state %s (%s)", self.relative(path), reason)
        return None

    def _backup(self, target: Path) -> Path:
        destination = self.paths.backup_dir / self._run_id / self.relative(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, destination)
        return destination

    # -- rollback ----------------------------------------------------------
    def rollback(self) -> list[str]:
        """Restore every applied change that has a backup. Returns paths."""
        restored: list[str] = []
        for change in reversed(self.changes.applied):
            target = self.resolve(change.path)
            if change.change_type is ChangeType.CREATE:
                if target.exists():
                    target.unlink()
                    restored.append(change.path)
                continue
            if change.backup and Path(change.backup).exists():
                shutil.copy2(change.backup, target)
                restored.append(change.path)
        if restored:
            self.logger.warning("rolled back %s file(s)", len(restored))
        return restored

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict[str, int]:
        return {
            "created": len(self.changes.created),
            "modified": len(self.changes.modified),
            "applied": len(self.changes.applied),
            "total": len(self.changes),
        }
