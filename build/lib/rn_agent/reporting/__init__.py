"""Rich rendering. Commands compute; this package presents."""

from __future__ import annotations

from .change_view import (
    render_context,
    render_outcome,
    render_proposals,
    render_refusals,
    render_usage,
    render_validation,
)
from .compatibility_view import render_compatibility
from .health_view import render_health
from .migrate_view import render_migration
from .release_view import render_release
from .review_view import render_review
from .scan_view import render_scan
from .upgrade_view import render_upgrade

__all__ = [
    "render_compatibility",
    "render_context",
    "render_health",
    "render_migration",
    "render_outcome",
    "render_proposals",
    "render_refusals",
    "render_release",
    "render_review",
    "render_scan",
    "render_upgrade",
    "render_usage",
    "render_validation",
]
