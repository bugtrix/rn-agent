"""Flags shared by every command.

Lives in its own module so command groups (``scan``/``health`` in ``app.py``,
authentication in ``auth.py``) can read the root callback's flags without
importing each other.
"""

from __future__ import annotations

from pathlib import Path


class GlobalOptions:
    """Flags shared by every command, captured by the root callback."""

    __slots__ = ("path", "dry_run", "yes", "verbose", "json_output")

    def __init__(self) -> None:
        self.path: Path | None = None
        self.dry_run: bool = False
        self.yes: bool = False
        self.verbose: bool = False
        self.json_output: bool = False


OPTIONS = GlobalOptions()
