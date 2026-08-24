"""``rn-agent migrate`` - move React Native versions without losing the project.

The shape of a safe migration, and why each part is there:

* **A branch first.** Everything after this writes native files; the developer
  must be able to walk away with ``git checkout -``.
* **A clean tree.** The rollback restores what the agent wrote, not what you had
  half-finished, so a dirty tree is refused unless you insist.
* **Steps, not a patch.** Each file is applied on its own and marked
  ``applied``/``conflict``/``manual``, so one drifted ``.pbxproj`` does not sink
  the rest of the migration - it becomes a task with the hunk attached.
* **Proof, then one repair attempt.** Install, typecheck, tests (and builds when
  asked). If that fails and AI is configured, the failure is handed to the model
  **once**; if it still fails, everything the agent wrote is rolled back.
* **A record either way.** ``.rn-agent/migration-history.json`` keeps the
  attempt, including the failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.apply import ApplyOutcome
from ..agents.context_builder import ContextBuilder
from ..agents.engine import AIEngine
from ..agents.prompts import error_fix_messages
from ..agents.rules import ProjectRules
from ..agents.workflow import EditWorkflow
from ..cli import ui
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..errors import ModelOutputError, ProviderError, RNAgentError
from ..migration.diff import HunkResult, apply_hunks, parse_diff, rename_placeholder
from ..migration.history import record as record_history
from ..migration.planner import PlanInputs, build_plan
from ..migration.rules import MigrationRule, RuleAction, RuleOutcome, apply_rule, load_rules
from ..migration.sources import DiffSource
from ..models.changes import RiskLevel
from ..models.migration import (
    MigrationOutcome,
    MigrationPlan,
    MigrationStep,
    StepKind,
    StepState,
)
from ..models.project import ProjectContext
from ..models.proposal import EditAction, FileEdit
from ..models.validation import ValidationReport
from ..reporting import change_view
from ..reporting.migrate_view import render_migration
from ..upgrade.registry import NpmRegistry
from ..utils.io import read_text, write_json
from ..utils.semver import compare, parse
from .health import CONTEXT_STALE_SECONDS

#: Where local rule files live, relative to the repository the agent runs in.
DEFAULT_RULES_DIR = Path("migration-rules")

SECTION_FOR_DEPENDENCY: dict[str, str] = {
    "react-native": "dependencies",
    "react": "dependencies",
    "@types/react": "devDependencies",
}


@dataclass(slots=True)
class MigrateAnalysis:
    project: ProjectContext
    from_version: str
    to_version: str
    plan: MigrationPlan


@dataclass(slots=True)
class MigratePlan:
    plan: MigrationPlan
    workflow: EditWorkflow
    branch: str | None = None
    edits: list[FileEdit] = field(default_factory=list)


class MigrateCommand(AgentCommand[MigrateAnalysis, MigratePlan]):
    name = "migrate"
    description = "Migrate React Native to a newer version, step by step"
    read_only = False
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        to_version: str | None = None,
        kinds: tuple[str, ...] = (),
        skip_native: bool = False,
        install: bool = True,
        build: bool = False,
        use_ai: bool = True,
        offline: bool = False,
        allow_dirty: bool = False,
        branch: bool | None = None,
        rules_dir: Path | None = None,
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.to_version = to_version
        self.kinds = kinds
        self.skip_native = skip_native
        self.install = install
        self.build = build
        self.use_ai = use_ai
        self.offline = offline
        self.allow_dirty = allow_dirty
        self.branch = branch
        self.rules_dir = rules_dir
        self.verbose = verbose
        self.report: MigrationPlan | None = None
        self.outcome: MigrationOutcome | None = None
        self.validation: ValidationReport | None = None
        self.ai_fixes = 0
        self._diff_source: DiffSource | None = None
        self._applied: list[str] = []

    # -- phases ------------------------------------------------------------
    def analyze(self) -> MigrateAnalysis:
        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        current = project.rn_version
        if not current or parse(current) is None:
            raise RNAgentError(
                "the project's React Native version could not be established",
                hint="Run your package manager's install, then `rn-agent scan`.",
            )

        registry = None if self.offline else NpmRegistry()
        target = self.to_version or self._newest(registry)
        if target is None:
            raise RNAgentError(
                "no target React Native version was given and none could be resolved",
                hint="Pass --to <version>, or run without --offline.",
            )
        if parse(target) is None:
            raise RNAgentError(
                f"{target} is not a React Native version",
                hint="Use a released version, for example --to 0.82.1.",
            )
        order = compare(current, target)
        if order is None or order >= 0:
            raise RNAgentError(
                f"this project is already on React Native {current}",
                hint=f"{target} is not newer. Pass a newer --to version.",
            )

        source = DiffSource(cache_dir=self.context.paths.cache_dir)
        self._diff_source = source
        document = source.fetch(current, target, offline=self.offline)
        rules = load_rules(
            self.rules_dir or DEFAULT_RULES_DIR,
            from_version=current,
            to_version=target,
            logger=self.logger,
        )
        plan = build_plan(
            PlanInputs(
                project=project,
                root=self.context.root,
                from_version=current,
                to_version=target,
                diff=document,
                rules=rules,
                registry=registry,
                knowledge=self.context.knowledge,
                skip_native=self.skip_native,
                kinds=self.kinds,
                diff_reason=source.reason,
                docs_url=self.context.config.migration.docs_base,
            )
        )
        self.report = plan
        return MigrateAnalysis(
            project=project, from_version=current, to_version=target, plan=plan
        )

    def plan(self, analysis: MigrateAnalysis) -> MigratePlan:
        workflow = EditWorkflow(
            self.context,
            rules=ProjectRules.load(self.context.paths),
            task="migration",
            allow_dependencies=True,
            allow_native=not self.skip_native,
        )
        return MigratePlan(plan=analysis.plan, workflow=workflow)

    def execute(self, plan: MigratePlan) -> None:
        agent = self.context
        agent.git.require_repository()
        if not self.allow_dirty:
            agent.git.require_clean()

        wants_branch = self.branch if self.branch is not None else agent.config.migration.create_git_branch
        if wants_branch and not agent.dry_run:
            plan.branch = agent.git.create_branch(
                f"{agent.config.migration.branch_prefix}-{plan.plan.to_version}"
            )
            plan.plan.branch = plan.branch
            self.logger.info("migrating on branch %s", plan.branch)

        edits = self._edits_for(plan.plan)
        if not edits:
            self.logger.info("no automatic step could be prepared")
            return

        outcome = plan.workflow.apply(
            [
                _proposal(
                    edits,
                    title=f"React Native {plan.plan.from_version} -> {plan.plan.to_version}",
                    risk=plan.plan.highest_risk,
                )
            ],
            reason=f"migrate: {plan.plan.from_version} -> {plan.plan.to_version}",
            question=(
                f"Apply {len(edits)} migration change(s) "
                f"(highest risk: {plan.plan.highest_risk.value})?"
            ),
        )
        self._applied = list(outcome.applied)
        self._run_validation(plan, outcome)

    def validate(self, plan: MigratePlan) -> dict[str, Any]:
        counts = plan.plan.counts()
        outcome = MigrationOutcome(
            from_version=plan.plan.from_version,
            to_version=plan.plan.to_version,
            branch=plan.branch,
            applied=[step.id for step in plan.plan.steps if step.state is StepState.APPLIED],
            conflicts=[step.id for step in plan.plan.steps if step.state is StepState.CONFLICT],
            manual=[step.id for step in plan.plan.manual_steps],
            validation=self.validation,
            rolled_back=plan.workflow.rolled_back,
            ai_fixes=self.ai_fixes,
            dry_run=self.context.dry_run,
        )
        self.outcome = outcome

        summary: dict[str, Any] = {**counts, "rolled_back": outcome.rolled_back}
        if self.context.dry_run:
            return summary
        try:
            self.context.paths.ensure()
            path = self.context.paths.cache_dir / "migration-report.json"
            write_json(
                path,
                {
                    "plan": plan.plan.model_dump(mode="json"),
                    "outcome": outcome.model_dump(mode="json"),
                },
            )
            summary["report"] = str(path)
            summary["history"] = str(record_history(self.context.paths, outcome))
        except OSError as exc:  # pragma: no cover - read-only project
            self.logger.warning("could not write the migration record: %s", exc)
        return summary

    def render(self, analysis: MigrateAnalysis, plan: MigratePlan) -> None:
        render_migration(plan.plan, outcome=self.outcome, verbose=self.verbose)
        change_view.render_validation(self.validation)
        self._closing_line(plan)

    def summary(self, analysis: MigrateAnalysis, plan: MigratePlan) -> dict[str, Any]:
        return {
            "from": analysis.from_version,
            "to": analysis.to_version,
            "branch": plan.branch,
            "offline": plan.plan.offline,
            "ai_fixes": self.ai_fixes,
            "validated": None if self.validation is None else self.validation.ok,
            "rolled_back": plan.workflow.rolled_back,
            **plan.plan.counts(),
        }

    def exit_code(self, analysis: MigrateAnalysis, plan: MigratePlan) -> int:
        if plan.workflow.rolled_back:
            return 1
        if self.validation is not None and not self.validation.ok:
            return 1
        return 0

    # -- step preparation --------------------------------------------------
    def _edits_for(self, plan: MigrationPlan) -> list[FileEdit]:
        """Turn every automatic step into a concrete file edit, or mark it."""
        edits: list[FileEdit] = []
        pending: dict[str, str] = {}
        for step in plan.steps:
            if not step.automatic:
                continue
            if step.kind is StepKind.DEPENDENCY:
                edit = self._dependency_edit(step, pending)
            elif step.diff is not None:
                edit = self._diff_edit(step, pending, plan=plan)
            else:
                edit = self._rule_edit(step, pending)
            if edit is not None:
                edits.append(edit)
                pending[edit.path] = edit.content or ""
        return edits

    def _dependency_edit(
        self, step: MigrationStep, pending: dict[str, str]
    ) -> FileEdit | None:
        source = pending.get("package.json") or read_text(self.context.root / "package.json")
        if source is None:  # pragma: no cover - detector guarantees the file
            step.state = StepState.FAILED
            step.reason = "package.json could not be read"
            return None
        try:
            payload = json.loads(source)
        except (json.JSONDecodeError, ValueError) as exc:
            step.state = StepState.FAILED
            step.reason = f"package.json is not valid JSON: {exc}"
            return None
        changed = False
        for name, version in step.payload.items():
            section = SECTION_FOR_DEPENDENCY.get(name, "dependencies")
            block = payload.setdefault(section, {})
            if not isinstance(block, dict):  # pragma: no cover - malformed manifest
                continue
            if name not in block and name != "react-native":
                continue
            block[name] = version
            changed = True
        if not changed:
            step.state = StepState.SKIPPED
            step.reason = "no dependency in this project needed changing"
            return None
        step.state = StepState.APPLIED
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        return FileEdit(
            path="package.json",
            action=EditAction.MODIFY,
            content=content + ("\n" if source.endswith("\n") else ""),
            reason=step.detail,
        )

    def _diff_edit(
        self, step: MigrationStep, pending: dict[str, str], *, plan: MigrationPlan
    ) -> FileEdit | None:
        assert step.file is not None and step.diff is not None
        current = pending.get(step.file) or read_text(self.context.root / step.file)
        if current is None:
            step.state = StepState.CONFLICT
            step.reason = "the file is not in this project any more"
            return None
        name = self._project_name(plan)
        diff_text, decided = rename_placeholder(step.diff, project_name=name)
        if not decided:  # pragma: no cover - planner filters these out
            step.state = StepState.CONFLICT
            step.reason = "the hunk renames the template app and the project name is unknown"
            return None
        files = parse_diff(f"diff --git a/{step.file} b/{step.file}\n--- a/{step.file}\n+++ b/{step.file}\n{diff_text}")
        hunks = files[0].hunks if files else []
        if not hunks:
            step.state = StepState.CONFLICT
            step.reason = "the diff could not be parsed"
            return None
        patched, result, applied = apply_hunks(current, hunks)
        if result is HunkResult.ALREADY:
            step.state = StepState.SKIPPED
            step.reason = "already applied"
            return None
        if result is HunkResult.CONFLICT or patched is None:
            step.state = StepState.CONFLICT
            step.reason = (
                "the surrounding code has changed; apply this hunk by hand "
                f"({applied} of {len(hunks)} hunk(s) matched)"
            )
            return None
        step.state = StepState.APPLIED
        return FileEdit(
            path=step.file,
            action=EditAction.MODIFY,
            content=patched,
            reason=f"{applied} hunk(s) from the upstream template",
        )

    def _rule_edit(self, step: MigrationStep, pending: dict[str, str]) -> FileEdit | None:
        if step.file is None:  # pragma: no cover - rules always carry a file
            return None
        action = step.payload.get("action")
        if action is None:
            step.state = StepState.SKIPPED
            step.reason = "nothing to do"
            return None
        rule = MigrationRule(
            id=step.id,
            kind=step.kind,
            file=step.file,
            action=RuleAction(action),
            risk=step.risk,
            key=step.payload.get("key"),
            value=step.payload.get("value"),
            old=step.payload.get("old"),
            new=step.payload.get("new"),
            line=step.payload.get("line"),
            source=step.source,
        )
        current = pending.get(step.file)
        if current is None:
            current = read_text(self.context.root / step.file)
        patched, outcome = apply_rule(current, rule)
        if outcome is RuleOutcome.APPLIED and patched is not None:
            step.state = StepState.APPLIED
            return FileEdit(
                path=step.file,
                action=EditAction.MODIFY,
                content=patched,
                reason=step.detail or step.title,
            )
        step.state = (
            StepState.SKIPPED if outcome is RuleOutcome.ALREADY else StepState.CONFLICT
        )
        step.reason = {
            RuleOutcome.ALREADY: "already applied",
            RuleOutcome.MISSING: "the file is not in this project",
            RuleOutcome.CONFLICT: "the expected text was not found; apply it by hand",
        }.get(outcome, "could not be applied")
        return None

    def _project_name(self, plan: MigrationPlan) -> str | None:
        _ = plan
        project = self.context.project
        return project.ios.project_name or project.name

    # -- validation and repair --------------------------------------------
    def _run_validation(self, plan: MigratePlan, outcome: ApplyOutcome) -> None:
        if self.context.dry_run or not outcome.wrote_anything:
            return
        from ..validation.runner import ProjectValidator

        validator = ProjectValidator(self.context)
        steps: list[str] = []
        migration = self.context.config.migration
        if self.install and migration.run_install:
            steps.append("install")
        if self.context.project.ios.present and migration.run_pod_install:
            steps.append("pods")
        steps.append("typecheck")
        if migration.run_tests:
            steps.append("tests")
        if self.build:
            if migration.run_android_build and self.context.project.android.present:
                steps.append("android")
            if migration.run_ios_build and self.context.project.ios.present:
                steps.append("ios")

        report = validator.run(steps)
        self.validation = report
        if report.ok:
            return

        if self._repair(plan, report):
            self.validation = validator.run(steps)
            if self.validation.ok:
                return

        restored = plan.workflow.applier.rollback()
        plan.workflow.rolled_back = bool(restored)
        for step in plan.plan.steps:
            if step.state is StepState.APPLIED:
                step.state = StepState.FAILED
                step.reason = "rolled back: the project did not build after the migration"
        self.logger.warning("migration rolled back (%s file(s) restored)", len(restored))

    def _repair(self, plan: MigratePlan, report: ValidationReport) -> bool:
        """One AI repair round. Returns whether anything was applied."""
        migration = self.context.config.migration
        if not (self.use_ai and migration.use_ai_for_errors):
            return False
        if not self.context.ai_ready():
            self.logger.info("no AI configured; skipping the repair round")
            return False

        failing = [step.name for step in report.failures]
        self.logger.info("asking the model to fix: %s", ", ".join(failing))
        selected = ContextBuilder(self.context).select(
            paths=tuple(self._applied) or (),
            query=" ".join(failing),
        )
        try:
            engine = AIEngine(self.context)
            proposals = engine.propose(
                error_fix_messages(
                    project=self.context.project,
                    rules=ProjectRules.load(self.context.paths),
                    context=selected,
                    report=report,
                    what_changed=(
                        f"react-native {plan.plan.from_version} -> {plan.plan.to_version} "
                        "was just applied to this project"
                    ),
                ),
                task="migration",
            )
        except (ProviderError, ModelOutputError) as error:
            self.logger.warning("the repair round did not produce a fix: %s", error.message)
            return False

        kept, _ = plan.workflow.screen(proposals.proposals)
        if not kept:
            return False
        repair = plan.workflow.apply(
            kept,
            reason="migrate: repair build errors",
            question=f"Apply {sum(len(p.usable_edits) for p in kept)} repair change(s)?",
        )
        self.ai_fixes = 1 if repair.wrote_anything else 0
        return repair.wrote_anything

    def _newest(self, registry: NpmRegistry | None) -> str | None:
        if registry is None:
            return None
        document = registry.packument("react-native")
        newest = document.newest() if document else None
        return newest.version if newest else None

    # -- rendering ---------------------------------------------------------
    def _closing_line(self, plan: MigratePlan) -> None:
        ui.blank()
        if self.context.dry_run:
            ui.note("dry run: no branch was created and no file was written")
            return
        if plan.workflow.rolled_back:
            ui.failure("the project did not build after the migration; every change was rolled back")
            if plan.branch:
                ui.note(f"you are still on branch {plan.branch}")
            return
        if self.validation is not None and not self.validation.ok:
            ui.failure("the migration was applied but validation failed")
            return
        conflicts = [step for step in plan.plan.steps if step.state is StepState.CONFLICT]
        manual = plan.plan.manual_steps
        if self._applied:
            ui.success(f"{len(self._applied)} file(s) migrated to {plan.plan.to_version}")
        if conflicts or manual:
            ui.warning(f"{len(conflicts) + len(manual)} step(s) need you: see above")
        if self.context.project.ios.present:
            ui.note("run `pod install` in ios/ if it was not run for you")
        ui.note(f"then: rn-agent health --refresh \u00b7 {self.context.config.migration.docs_base}")


def _proposal(edits: list[FileEdit], *, title: str, risk: RiskLevel) -> Any:
    from ..models.proposal import Proposal

    return Proposal(id="migration", title=title, edits=edits, risk=risk)


register(MigrateCommand, phase=5)
