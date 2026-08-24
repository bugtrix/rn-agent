"""Local knowledge: SQLite run history plus curated offline advisory data."""

from __future__ import annotations

from .data import (
    DeprecatedPackage,
    KnowledgeData,
    LibrarySignature,
    load_knowledge_data,
)
from .store import KnowledgeStore

__all__ = [
    "DeprecatedPackage",
    "KnowledgeData",
    "KnowledgeStore",
    "LibrarySignature",
    "load_knowledge_data",
]
