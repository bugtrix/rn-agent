"""``rn-agent scan`` - build the shared project brain.

Read-only with respect to project source: the only writes go into
``.rn-agent/`` (context, architecture, dependencies, rules, config), and even
those are skipped in dry-run mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.command import AgentCommand
from ..core.config import write_default_config
from ..core.context import AgentContext
from ..core.registry import register
from ..models.project import ProjectContext
from ..project.architecture import architecture_yaml_payload
from ..project.scanner import ProjectScanner, save_context
from ..reporting.scan_view import render_scan
from ..utils.io import write_json, write_yaml

RULES_HEADER = """\
# Project-specific rules the agent must respect.
# `rn-agent scan` seeds this from the detected architecture; edit freely.
# Native files stay blocked unless you pass --allow-native, name them with
# --file, or list them under rules.allow_native_paths (globs allowed).
"""


@dataclass(slots=True)
class ScanAnalysis:
    context: ProjectContext
    capabilities: list[str]


@dataclass(slots=True)
class ScanPlan:
    context: ProjectContext
    capabilities: list[str]
    write_state: bool


@register
class ScanCommand(AgentCommand[ScanAnalysis, ScanPlan]):
    name = "scan"
    description = "Detect the project and build the shared context every command uses"
    read_only = False  # writes .rn-agent state only
    requires_context = False

    def __init__(
        self, context: AgentContext, *, verbose: bool = False, probe_tools: bool = True
    ) -> None:
        super().__init__(context)
        self.verbose = verbose
        self.probe_tools = probe_tools

    # -- phases ------------------------------------------------------------
    def analyze(self) -> ScanAnalysis:
        agent = self.context
        scanner = ProjectScanner(
            agent.detected, agent.paths, agent.runner, knowledge=agent.knowledge
        )
        git_info = agent.git.describe()
        source_stats = agent.walker.stats()
        project = scanner.scan(
            probe_tools=self.probe_tools, git_info=git_info, source_stats=source_stats
        )
        agent.set_project(project)
        self.logger.info(
            "scanned %s: rn=%s deps=%s native=%s",
            project.root,
            project.rn_version,
            len(project.dependencies),
            len(project.native_modules),
        )
        return ScanAnalysis(context=project, capabilities=scanner.capabilities)

    def plan(self, analysis: ScanAnalysis) -> ScanPlan:
        return ScanPlan(
            context=analysis.context,
            capabilities=analysis.capabilities,
            write_state=not self.context.dry_run,
        )

    def execute(self, plan: ScanPlan) -> None:
        if not plan.write_state:
            self.logger.info("dry-run: skipping .rn-agent writes")
            return
        agent = self.context
        agent.paths.ensure()
        write_default_config(agent.paths)
        save_context(agent.paths, plan.context)
        write_yaml(
            agent.paths.architecture_file,
            architecture_yaml_payload(plan.context.architecture, plan.capabilities),
            header="# Inferred project architecture. rn-agent follows this; edit to correct it.",
        )
        write_json(agent.paths.dependencies_file, self._dependency_payload(plan.context))
        if not agent.paths.rules_file.exists():
            write_yaml(
                agent.paths.rules_file,
                self._default_rules(plan.context),
                header=RULES_HEADER,
            )
        if not agent.paths.decisions_file.exists():
            agent.paths.decisions_file.write_text(
                "# Decisions\n\n"
                "Architectural decisions recorded by rn-agent and by you.\n",
                encoding="utf-8",
            )
        self._ensure_agent_gitignore()
        try:
            agent.store.save_context(
                plan.context.model_dump(mode="json"), rn_version=plan.context.rn_version
            )
        except Exception as exc:  # storage must never fail a scan
            self.logger.warning("could not store context snapshot: %s", exc)

    def validate(self, plan: ScanPlan) -> dict[str, Any]:
        if self.context.dry_run:
            return {"written": False}
        return {
            "written": self.context.paths.context_file.is_file(),
            "context_file": str(self.context.paths.context_file),
        }

    def render(self, analysis: ScanAnalysis, plan: ScanPlan) -> None:
        render_scan(plan.context, verbose=self.verbose, wrote=plan.write_state)

    def summary(self, analysis: ScanAnalysis, plan: ScanPlan) -> dict[str, Any]:
        context = plan.context
        return {
            "rn_version": context.rn_version,
            "react_version": context.react_native.react_version,
            "package_manager": context.package_manager.name,
            "dependencies": len(context.dependencies),
            "native_modules": len(context.native_modules),
            "android": context.android.present,
            "ios": context.ios.present,
            "typescript": context.react_native.typescript,
            "warnings": len(context.warnings),
            "duration_ms": context.scan_duration_ms,
        }

    # -- helpers -----------------------------------------------------------
    def _dependency_payload(self, context: ProjectContext) -> dict[str, Any]:
        return {
            "generated_at": context.generated_at,
            "package_manager": context.package_manager.model_dump(mode="json"),
            "node_modules_present": context.node_modules_present,
            "counts": {
                "total": len(context.dependencies),
                "native": len(context.native_modules),
            },
            "dependencies": [
                dependency.model_dump(mode="json") for dependency in context.dependencies
            ],
        }

    def _default_rules(self, context: ProjectContext) -> dict[str, Any]:
        architecture = context.architecture
        return {
            "rules": {
                "follow_existing_architecture": True,
                "allowed_state_management": architecture.state_management or ["component-state"],
                "allowed_navigation": architecture.navigation or [],
                "api_layer": architecture.api_layer or [],
                "styling": architecture.styling or [],
                "testing": architecture.testing or [],
                "language": architecture.language,
                "forbid_new_dependencies": True,
                "forbid_native_edits_without_confirmation": True,
                "allow_native_paths": [],
            },
            "notes": [
                "rn-agent must not introduce a different state-management or data-fetching "
                "library than the ones listed above unless you ask for it.",
            ],
        }

    def _ensure_agent_gitignore(self) -> None:
        """Keep caches/logs/db out of git without touching the project's .gitignore."""
        target = self.context.paths.agent_dir / ".gitignore"
        if target.exists():
            return
        target.write_text(
            "# Written by rn-agent: local state only.\ncache/\nlogs/\nknowledge/\n",
            encoding="utf-8",
        )
