"""``rn-agent upgrade`` - move dependencies, with the cost stated first.

The plan is computed before anything is written, and it is the same code path
whether or not you apply it: ``--dry-run`` prints the table and stops. What
lands on disk is one edit to ``package.json``, applied through the same
``FileManager`` envelope as everything else, so a failed typecheck afterwards
can restore the previous file byte-for-byte.

``package.json`` is exactly the file the project's rules forbid touching, which
is why this command - and only this command - passes
``allow_dependencies=True``: the developer asked for a dependency change by
name. The confirmation gate and the backup still apply.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..agents.apply import ApplyOutcome
from ..agents.rules import ProjectRules
from ..agents.workflow import EditWorkflow
from ..cli import ui
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..errors import RNAgentError
from ..models.project import DependencyKind, ProjectContext
from ..models.proposal import EditAction, FileEdit, Proposal
from ..models.upgrade import UpgradePlan
from ..models.validation import ValidationReport
from ..reporting import change_view
from ..reporting.upgrade_view import render_upgrade
from ..upgrade.planner import POLICIES, plan_upgrades
from ..upgrade.registry import NpmRegistry
from ..utils.io import read_text, write_json
from .health import CONTEXT_STALE_SECONDS

SECTION_BY_KIND: dict[DependencyKind, str] = {
    DependencyKind.PROD: "dependencies",
    DependencyKind.DEV: "devDependencies",
}

_INDENT_RE = re.compile(r"^\{\r?\n(?P<indent>[ \t]+)\"", re.MULTILINE)


@dataclass(slots=True)
class UpgradeAnalysis:
    project: ProjectContext
    registry: NpmRegistry | None


@dataclass(slots=True)
class UpgradeReadyPlan:
    plan: UpgradePlan
    edit: FileEdit | None
    workflow: EditWorkflow
    notes: list[str] = field(default_factory=list)


class UpgradeCommand(AgentCommand[UpgradeAnalysis, UpgradeReadyPlan]):
    name = "upgrade"
    description = "Upgrade React Native to a chosen version, or bump JavaScript dependencies"
    read_only = False
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        policy: str = "minor",
        only: tuple[str, ...] = (),
        skip: tuple[str, ...] = (),
        include_native: bool = False,
        install: bool = True,
        checks: tuple[str, ...] = ("typecheck", "tests"),
        offline: bool = False,
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.policy = policy
        self.only = only
        self.skip = skip
        self.include_native = include_native
        self.install = install
        self.checks = checks
        self.offline = offline
        self.verbose = verbose
        self.report: UpgradePlan | None = None
        self.outcome: ApplyOutcome | None = None
        self.validation: ValidationReport | None = None
        self.installed: ValidationReport | None = None

    # -- phases ------------------------------------------------------------
    def analyze(self) -> UpgradeAnalysis:
        if self.policy not in POLICIES:
            raise RNAgentError(
                f"unknown upgrade target: {self.policy}",
                hint=f"Use one of: {', '.join(POLICIES)}.",
            )
        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        registry = None if self.offline else NpmRegistry()
        return UpgradeAnalysis(project=project, registry=registry)

    def plan(self, analysis: UpgradeAnalysis) -> UpgradeReadyPlan:
        plan = plan_upgrades(
            project=analysis.project,
            registry=analysis.registry,
            policy=self.policy,
            only=self.only,
            skip=self.skip,
            include_native=self.include_native,
        )
        self.report = plan
        edit, notes = self._package_json_edit(plan)
        workflow = EditWorkflow(
            self.context,
            rules=ProjectRules.load(self.context.paths),
            task="upgrade",
            allow_dependencies=True,
        )
        return UpgradeReadyPlan(plan=plan, edit=edit, workflow=workflow, notes=notes)

    def execute(self, plan: UpgradeReadyPlan) -> None:
        if plan.edit is None:
            self.logger.info("nothing to upgrade under policy %s", self.policy)
            return
        selected = plan.plan.selected
        proposal = Proposal(
            id="dependency-upgrade",
            title=f"upgrade {len(selected)} dependency/ies ({self.policy})",
            summary=", ".join(f"{item.name} -> {item.target}" for item in selected),
            edits=[plan.edit],
            risk=plan.plan.highest_risk,
        )
        self.outcome = plan.workflow.apply(
            [proposal],
            reason=f"upgrade: {proposal.summary}",
            question=(
                f"Update {len(selected)} dependency range(s) in package.json "
                f"(highest risk: {plan.plan.highest_risk.value})?"
            ),
        )
        if self.outcome.wrote_anything and self.install and not self.context.dry_run:
            from ..validation.runner import ProjectValidator

            step = ProjectValidator(self.context).install()
            self.installed = ValidationReport(steps=[step])
            if step.failed:
                self.logger.warning("install failed after the upgrade")

    def validate(self, plan: UpgradeReadyPlan) -> dict[str, Any]:
        if self.outcome is not None:
            failed_install = self.installed is not None and not self.installed.ok
            if failed_install:
                self.validation = self.installed
                if self.outcome.wrote_anything:
                    plan.workflow.applier.rollback()
                    plan.workflow.rolled_back = True
            else:
                self.validation = plan.workflow.prove(self.checks, outcome=self.outcome)
        summary: dict[str, Any] = {
            "selected": len(plan.plan.selected),
            "applied": bool(self.outcome and self.outcome.wrote_anything),
            "rolled_back": plan.workflow.rolled_back,
        }
        if self.context.dry_run:
            return summary
        try:
            self.context.paths.ensure()
            path = self.context.paths.cache_dir / "upgrade-report.json"
            payload = plan.plan.model_dump(mode="json")
            payload["applied"] = list(self.outcome.applied) if self.outcome else []
            payload["rolled_back"] = plan.workflow.rolled_back
            payload["validation"] = (
                self.validation.model_dump(mode="json") if self.validation else None
            )
            write_json(path, payload)
            summary["report"] = str(path)
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write upgrade report: %s", exc)
        return summary

    def render(self, analysis: UpgradeAnalysis, plan: UpgradeReadyPlan) -> None:
        render_upgrade(plan.plan, verbose=self.verbose)
        for note in plan.notes:
            ui.note(note)
        if self.outcome is not None:
            change_view.render_outcome(self.outcome, dry_run=self.context.dry_run)
        change_view.render_validation(self.installed)
        change_view.render_validation(self.validation)
        self._closing_line(plan)

    def summary(self, analysis: UpgradeAnalysis, plan: UpgradeReadyPlan) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "registry_available": plan.plan.registry_available,
            "highest_risk": plan.plan.highest_risk.value,
            "applied": bool(self.outcome and self.outcome.wrote_anything),
            "rolled_back": plan.workflow.rolled_back,
            "validated": None if self.validation is None else self.validation.ok,
            **plan.plan.counts(),
        }

    def exit_code(self, analysis: UpgradeAnalysis, plan: UpgradeReadyPlan) -> int:
        if self.validation is not None and not self.validation.ok:
            return 1
        if not plan.plan.registry_available and not self.offline:
            return 1
        if self.only and not plan.plan.selected:
            return 1
        return 0

    # -- helpers -----------------------------------------------------------
    def _package_json_edit(self, plan: UpgradePlan) -> tuple[FileEdit | None, list[str]]:
        """Rewrite the declared ranges, preserving the file's own indentation."""
        selected = plan.selected
        if not selected:
            return None, []
        source = read_text(self.context.root / "package.json")
        if source is None:  # pragma: no cover - detector guarantees the file
            raise RNAgentError("package.json could not be read")
        try:
            payload = json.loads(source)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RNAgentError(
                f"package.json is not valid JSON: {exc}",
                hint="Fix the file, then re-run the upgrade.",
            ) from exc

        notes: list[str] = []
        changed = 0
        for candidate in selected:
            section = SECTION_BY_KIND.get(candidate.kind)
            spec = candidate.spec
            if section is None or spec is None:
                continue
            block = payload.get(section)
            if not isinstance(block, dict) or candidate.name not in block:
                notes.append(f"{candidate.name} is not declared in {section}; left alone")
                continue
            block[candidate.name] = spec
            changed += 1
        if not changed:
            return None, notes

        indent = _detect_indent(source)
        content = json.dumps(payload, indent=indent, ensure_ascii=False)
        if source.endswith("\n"):
            content += "\n"
        return (
            FileEdit(
                path="package.json",
                action=EditAction.MODIFY,
                content=content,
                reason=f"{changed} dependency range(s) updated",
            ),
            notes,
        )

    def _closing_line(self, plan: UpgradeReadyPlan) -> None:
        ui.blank()
        if not plan.plan.registry_available and not self.offline:
            ui.failure("the npm registry could not be reached; no upgrade was planned")
            ui.note("retry when you are online, or use --offline to see drift only")
            return
        if self.context.dry_run:
            if plan.plan.selected:
                ui.note("dry run: package.json was not modified")
            return
        if plan.workflow.rolled_back:
            ui.failure("validation failed after the upgrade; package.json was restored")
            ui.note(f"run `{plan.plan.install_command}` to bring node_modules back in step")
            return
        if self.validation is not None and not self.validation.ok:
            ui.failure("validation failed after the upgrade")
            return
        if self.outcome is not None and self.outcome.wrote_anything:
            ui.success(f"{len(plan.plan.selected)} dependency range(s) updated")
            if not self.install:
                ui.note(f"now run `{plan.plan.install_command}`")
            return
        ui.success("every dependency is already current for this policy")


def _detect_indent(source: str) -> int:
    """Match the file's existing indentation instead of imposing ours."""
    match = _INDENT_RE.search(source)
    if match is None:
        return 2
    indent = match.group("indent")
    return len(indent.expandtabs(2))


register(UpgradeCommand, phase=4)
