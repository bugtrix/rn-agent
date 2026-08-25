"""Analyzer contract.

An analyzer turns the shared :class:`ProjectContext` into
:class:`HealthCheck` results. It is pure: no writes, no network, no AI. The only
side effect allowed is running a read-only external tool (``tsc --noEmit``,
``eslint``) and only when the caller asks for a deep run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..knowledge.data import KnowledgeData
from ..models.health import Category, CheckStatus, HealthCheck, Severity
from ..models.project import ProjectContext
from ..runner.command_runner import CommandRunner


@dataclass(frozen=True, slots=True)
class AnalyzerInput:
    """Everything an analyzer is allowed to look at."""

    project: ProjectContext
    knowledge: KnowledgeData
    root: Path
    runner: CommandRunner
    deep: bool = False


class Analyzer(ABC):
    """Base class for the health analyzers."""

    category: Category = Category.PROJECT
    title: str = "analyzer"

    def __init__(self, data: AnalyzerInput) -> None:
        self.data = data

    @property
    def project(self) -> ProjectContext:
        return self.data.project

    @property
    def knowledge(self) -> KnowledgeData:
        return self.data.knowledge

    @property
    def root(self) -> Path:
        return self.data.root

    @abstractmethod
    def run(self) -> list[HealthCheck]:
        """Produce the checks for this category."""

    # -- helpers -----------------------------------------------------------
    def check(
        self,
        check_id: str,
        title: str,
        status: CheckStatus,
        *,
        severity: Severity = Severity.INFO,
        detail: str = "",
        recommendation: str | None = None,
        fix: Sequence[str] | None = None,
        evidence: dict[str, str] | None = None,
        source: str | None = None,
        docs: str | None = None,
    ) -> HealthCheck:
        return HealthCheck(
            id=check_id,
            category=self.category,
            title=title,
            status=status,
            severity=severity if status in (CheckStatus.FAIL, CheckStatus.WARN) else Severity.INFO,
            detail=detail,
            recommendation=recommendation,
            fix=list(fix or ()),
            evidence={k: str(v) for k, v in (evidence or {}).items() if v is not None},
            source=source,
            docs=docs,
        )

    def ok(self, check_id: str, title: str, detail: str = "", **kwargs: Any) -> HealthCheck:
        return self.check(check_id, title, CheckStatus.PASS, detail=detail, **kwargs)

    def fail(
        self,
        check_id: str,
        title: str,
        detail: str,
        *,
        severity: Severity = Severity.HIGH,
        **kwargs: Any,
    ) -> HealthCheck:
        return self.check(check_id, title, CheckStatus.FAIL, severity=severity, detail=detail, **kwargs)

    def warn(
        self,
        check_id: str,
        title: str,
        detail: str,
        *,
        severity: Severity = Severity.MEDIUM,
        **kwargs: Any,
    ) -> HealthCheck:
        return self.check(check_id, title, CheckStatus.WARN, severity=severity, detail=detail, **kwargs)

    def skip(self, check_id: str, title: str, detail: str, **kwargs: Any) -> HealthCheck:
        """Used whenever the facts are unavailable - never a fabricated warning."""
        return self.check(check_id, title, CheckStatus.SKIP, detail=detail, **kwargs)


def summarize(items: Sequence[str], *, limit: int = 3) -> str:
    """Name the first few items, then count the rest.

    A finding that says "3 packages" and hides the names forces the developer to
    re-run with ``--verbose`` to learn anything, so every problem detail names
    what it found. The cap keeps one finding from becoming a page.
    """
    ordered = sorted(items)
    if not ordered:
        return ""
    if len(ordered) <= limit:
        return "; ".join(ordered)
    shown = "; ".join(ordered[:limit])
    return f"{shown}; and {len(ordered) - limit} more"
