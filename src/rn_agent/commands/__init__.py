"""Command implementations.

Importing this package registers every available command in the registry, which
is how the CLI discovers them.
"""

from __future__ import annotations

from .health import HealthCommand
from .scan import ScanCommand

__all__ = ["HealthCommand", "ScanCommand"]
