"""Pydantic models: the shared vocabulary of the agent."""

from __future__ import annotations

from .changes import ChangeSet, FileChange, RiskLevel
from .config import AgentConfig, AIConfig, ContextConfig, MigrationConfig, SafetyConfig
from .health import CheckStatus, HealthCheck, HealthReport, Severity
from .project import (
    AndroidInfo,
    ArchitectureInfo,
    DependencyInfo,
    DependencyKind,
    GitInfo,
    IOSInfo,
    PackageManagerInfo,
    ProjectContext,
    ReactNativeInfo,
    SourceStats,
)

__all__ = [
    "AIConfig",
    "AgentConfig",
    "AndroidInfo",
    "ArchitectureInfo",
    "ChangeSet",
    "CheckStatus",
    "ContextConfig",
    "DependencyInfo",
    "DependencyKind",
    "FileChange",
    "GitInfo",
    "HealthCheck",
    "HealthReport",
    "IOSInfo",
    "MigrationConfig",
    "PackageManagerInfo",
    "ProjectContext",
    "ReactNativeInfo",
    "RiskLevel",
    "SafetyConfig",
    "Severity",
    "SourceStats",
]
