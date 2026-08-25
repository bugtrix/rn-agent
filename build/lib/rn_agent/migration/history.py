"""What was migrated, when, and whether it stuck.

``.rn-agent/migration-history.json`` is append-only and local. It exists so the
next person - or the same person in six months - can see that 0.79 -> 0.81 was
attempted, on which branch, what was left manual, and whether it was rolled
back. A rolled-back attempt is recorded exactly like a successful one: the
failure is the useful part.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.paths import AgentPaths
from ..models.migration import MigrationOutcome
from ..utils.io import read_json, write_json

MAX_ENTRIES = 50


def load_history(paths: AgentPaths) -> list[dict[str, Any]]:
    """Previous migration attempts, oldest first."""
    payload = read_json(paths.migration_history_file, default=[])
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def record(paths: AgentPaths, outcome: MigrationOutcome) -> Path:
    """Append one attempt and return the history file."""
    history = load_history(paths)
    history.append(outcome.model_dump(mode="json"))
    paths.ensure()
    return write_json(paths.migration_history_file, history[-MAX_ENTRIES:])


def previous_attempt(paths: AgentPaths, *, to_version: str) -> dict[str, Any] | None:
    """The last attempt at this target, if there was one."""
    for entry in reversed(load_history(paths)):
        if entry.get("to_version") == to_version:
            return entry
    return None
