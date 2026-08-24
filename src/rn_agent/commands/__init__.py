"""Command implementations.

Importing this package registers every available command in the registry, which
is how the CLI discovers them.
"""

from __future__ import annotations

from .compatibility import CompatibilityCommand
from .docs import DocsCommand
from .feature import FeatureCommand
from .fix import FixCommand
from .health import HealthCommand
from .migrate import MigrateCommand
from .release import ReleaseCommand
from .review import ReviewCommand
from .scan import ScanCommand
from .test import TestCommand
from .upgrade import UpgradeCommand

__all__ = [
    "CompatibilityCommand",
    "DocsCommand",
    "FeatureCommand",
    "FixCommand",
    "HealthCommand",
    "MigrateCommand",
    "ReleaseCommand",
    "ReviewCommand",
    "ScanCommand",
    "TestCommand",
    "UpgradeCommand",
]
