"""Rich rendering. Commands compute; this package presents."""

from __future__ import annotations

from .health_view import render_health
from .scan_view import render_scan

__all__ = ["render_health", "render_scan"]
