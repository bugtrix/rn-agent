"""Project discovery and the scanner that builds the shared brain."""

from __future__ import annotations

from .detector import DetectedProject, detect_project, find_project_root
from .scanner import ProjectScanner, load_context, save_context

__all__ = [
    "DetectedProject",
    "ProjectScanner",
    "detect_project",
    "find_project_root",
    "load_context",
    "save_context",
]
