"""React Native version migration.

Four pieces, deliberately separate so each can be tested without the others:

* :mod:`~rn_agent.migration.sources` - fetching and caching the upstream diff;
* :mod:`~rn_agent.migration.diff` - parsing it, and applying hunks strictly;
* :mod:`~rn_agent.migration.rules` - local, exact, version-pinned edits;
* :mod:`~rn_agent.migration.planner` - turning all of it into ordered steps.

Plus :mod:`~rn_agent.migration.history`, which records what was attempted.
"""

from __future__ import annotations

from .diff import DiffFile, Hunk, HunkResult, apply_hunks, parse_diff, rename_placeholder
from .history import load_history, record
from .planner import PlanInputs, build_plan
from .rules import MigrationRule, RuleOutcome, RuleSet, apply_rule, load_rules
from .sources import DIFF_BASE, DiffDocument, DiffSource

__all__ = [
    "DIFF_BASE",
    "DiffDocument",
    "DiffFile",
    "DiffSource",
    "Hunk",
    "HunkResult",
    "MigrationRule",
    "PlanInputs",
    "RuleOutcome",
    "RuleSet",
    "apply_hunks",
    "apply_rule",
    "build_plan",
    "load_history",
    "load_rules",
    "parse_diff",
    "record",
    "rename_placeholder",
]
