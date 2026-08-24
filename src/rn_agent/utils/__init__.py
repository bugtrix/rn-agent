"""Small, dependency-free helpers shared by every layer."""

from __future__ import annotations

from .io import (
    atomic_write_text,
    ensure_dir,
    read_json,
    read_text,
    read_yaml,
    write_json,
    write_yaml,
)
from .redaction import is_secret_path, redact
from .semver import Version, coerce, compare, is_undecidable_range, parse, satisfies

__all__ = [
    "Version",
    "atomic_write_text",
    "coerce",
    "compare",
    "ensure_dir",
    "is_secret_path",
    "is_undecidable_range",
    "parse",
    "read_json",
    "read_text",
    "read_yaml",
    "redact",
    "satisfies",
    "write_json",
    "write_yaml",
]
