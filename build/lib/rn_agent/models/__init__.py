"""Pydantic models: the shared vocabulary of the agent."""

from __future__ import annotations

from .changes import ChangeSet, ChangeType, FileChange, RiskLevel
from .compatibility import CompatArea, CompatibilityEntry, CompatibilityReport, CompatStatus
from .config import AgentConfig, AIConfig, ContextConfig, MigrationConfig, SafetyConfig
from .health import CheckStatus, HealthCheck, HealthReport, Severity
from .migration import MigrationOutcome, MigrationPlan, MigrationStep, StepKind, StepState
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
from .proposal import EditAction, FileEdit, Proposal, ProposalSet
from .release import BumpKind, ReleasePlan, VersionChange
from .review import ReviewFinding, ReviewReport
from .upgrade import ChangeKind, UpgradeCandidate, UpgradePlan
from .validation import StepStatus, ValidationReport, ValidationStep

__all__ = [
    "AIConfig",
    "AgentConfig",
    "AndroidInfo",
    "ArchitectureInfo",
    "BumpKind",
    "ChangeKind",
    "ChangeSet",
    "ChangeType",
    "CheckStatus",
    "CompatArea",
    "CompatStatus",
    "CompatibilityEntry",
    "CompatibilityReport",
    "ContextConfig",
    "DependencyInfo",
    "DependencyKind",
    "EditAction",
    "FileChange",
    "FileEdit",
    "GitInfo",
    "HealthCheck",
    "HealthReport",
    "IOSInfo",
    "MigrationConfig",
    "MigrationOutcome",
    "MigrationPlan",
    "MigrationStep",
    "PackageManagerInfo",
    "ProjectContext",
    "Proposal",
    "ProposalSet",
    "ReactNativeInfo",
    "ReleasePlan",
    "ReviewFinding",
    "ReviewReport",
    "RiskLevel",
    "SafetyConfig",
    "Severity",
    "SourceStats",
    "StepKind",
    "StepState",
    "StepStatus",
    "UpgradeCandidate",
    "UpgradePlan",
    "ValidationReport",
    "ValidationStep",
    "VersionChange",
]
