"""``rn-agent health`` - real diagnostics across RN, JS, Android and iOS.

Strictly read-only. It reuses the context produced by ``scan`` (refreshing it
automatically when it is missing or stale) and runs the deterministic analyzers.
No AI is involved at any point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..analyzers import ANALYZERS
from ..analyzers.base import AnalyzerInput
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..models.health import CheckStatus, HealthCheck, HealthReport, Severity
from ..models.project import ProjectContext
from ..project.scanner import ProjectScanner, save_context
from ..reporting.health_view import render_health
from ..utils.io import write_json

CONTEXT_STALE_SECONDS = 24 * 60 * 60


@dataclass(slots=True)
class HealthAnalysis:
    project: ProjectContext
    checks: list[HealthCheck] = field(default_factory=list)
    refreshed: bool = False


@dataclass(slots=True)
class HealthPlan:
    report: HealthReport


@register
class HealthCommand(AgentCommand[HealthAnalysis, HealthPlan]):
    name = "health"
    description = "Diagnose React Native, JavaScript, Android and iOS configuration"
    read_only = True
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        deep: bool = False,
        verbose: bool = False,
        refresh: bool = False,
        fail_under: int | None = None,
        categories: tuple[str, ...] = (),
    ) -> None:
        super().__init__(context)
        self.deep = deep
        self.verbose = verbose
        self.refresh = refresh
        self.fail_under = fail_under
        self.categories = categories
        #: Set in plan(); lets `--json` emit the full report even in dry-run.
        self.report: HealthReport | None = None

    # -- phases ------------------------------------------------------------
    def analyze(self) -> HealthAnalysis:
        project, refreshed = self._project_context()
        data = AnalyzerInput(
            project=project,
            knowledge=self.context.knowledge,
            root=self.context.root,
            runner=self.context.runner,
            deep=self.deep,
        )
        checks: list[HealthCheck] = []
        for analyzer_class in ANALYZERS:
            analyzer = analyzer_class(data)
            if self.categories and analyzer.category.value not in self.categories:
                continue
            try:
                checks.extend(analyzer.run())
            except Exception as exc:  # one broken analyzer must not kill health
                self.logger.exception("analyzer %s failed", analyzer_class.__name__)
                checks.append(
                    HealthCheck(
                        id=f"{analyzer.category.value}.analyzer_error",
                        category=analyzer.category,
                        title=f"{analyzer.title} analyzer failed",
                        status=CheckStatus.WARN,
                        severity=Severity.LOW,
                        detail=f"{type(exc).__name__}: {exc}",
                        recommendation="Please report this with the log in .rn-agent/logs/health.log.",
                    )
                )
        self.logger.info("health produced %s checks", len(checks))
        return HealthAnalysis(project=project, checks=checks, refreshed=refreshed)

    def plan(self, analysis: HealthAnalysis) -> HealthPlan:
        report = HealthReport(
            project_root=analysis.project.root,
            rn_version=analysis.project.rn_version,
            checks=analysis.checks,
            deep=self.deep,
        )
        self.report = report
        return HealthPlan(report=report)

    def validate(self, plan: HealthPlan) -> dict[str, Any]:
        if self.context.dry_run:
            return {}
        report_path = self.context.paths.cache_dir / "health-report.json"
        try:
            self.context.paths.ensure()
            write_json(report_path, plan.report.model_dump(mode="json"))
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write health report: %s", exc)
            return {}
        if self.context.run_id is not None:
            try:
                self.context.store.record_findings(
                    self.context.run_id,
                    "health",
                    [check.model_dump(mode="json") for check in plan.report.problems],
                )
            except Exception as exc:  # pragma: no cover - storage failure
                self.logger.warning("could not record findings: %s", exc)
        return {"report": str(report_path)}

    def render(self, analysis: HealthAnalysis, plan: HealthPlan) -> None:
        render_health(plan.report, verbose=self.verbose)

    def summary(self, analysis: HealthAnalysis, plan: HealthPlan) -> dict[str, Any]:
        report = plan.report
        return {
            "score": report.score,
            "grade": report.grade,
            "deep": self.deep,
            "refreshed_context": analysis.refreshed,
            **report.counts(),
        }

    def exit_code(self, analysis: HealthAnalysis, plan: HealthPlan) -> int:
        report = plan.report
        if self.fail_under is not None and report.score < self.fail_under:
            return 1
        if report.by_severity(Severity.CRITICAL):
            return 1
        return 0

    # -- helpers -----------------------------------------------------------
    def _project_context(self) -> tuple[ProjectContext, bool]:
        """Use the stored brain; rescan when missing, stale or requested."""
        from ..project.scanner import context_age_seconds

        agent = self.context
        age = context_age_seconds(agent.paths)
        needs_refresh = self.refresh or not agent.has_project_context() or (
            age is not None and age > CONTEXT_STALE_SECONDS
        )
        if not needs_refresh:
            try:
                return agent.project, False
            except Exception as exc:
                self.logger.warning("stored context unusable (%s); rescanning", exc)

        scanner = ProjectScanner(agent.detected, agent.paths, agent.runner, knowledge=agent.knowledge)
        project = scanner.scan(
            probe_tools=True, git_info=agent.git.describe(), source_stats=agent.walker.stats()
        )
        agent.set_project(project)
        if not agent.dry_run:
            try:
                save_context(agent.paths, project)
            except OSError as exc:  # pragma: no cover
                self.logger.warning("could not persist refreshed context: %s", exc)
        return project, True
