"""Deterministic project analyzers used by ``rn-agent health``."""

from __future__ import annotations

from .android_analyzer import AndroidAnalyzer
from .base import Analyzer, AnalyzerInput
from .ios_analyzer import IOSAnalyzer
from .js_analyzer import JavaScriptAnalyzer
from .project_analyzer import ProjectAnalyzer
from .rn_analyzer import ReactNativeAnalyzer

ANALYZERS: tuple[type[Analyzer], ...] = (
    ProjectAnalyzer,
    ReactNativeAnalyzer,
    JavaScriptAnalyzer,
    AndroidAnalyzer,
    IOSAnalyzer,
)

__all__ = [
    "ANALYZERS",
    "Analyzer",
    "AnalyzerInput",
    "AndroidAnalyzer",
    "IOSAnalyzer",
    "JavaScriptAnalyzer",
    "ProjectAnalyzer",
    "ReactNativeAnalyzer",
]
