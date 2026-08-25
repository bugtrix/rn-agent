"""``rn-agent delegate`` - hand a task to the Cursor agent, then hold it to account.

Every other AI command in this agent asks a model for a proposal and applies it
itself. This one is the opposite: Cursor's agent has its own tools and does the
editing, and rn-agent's job becomes the part Cursor cannot do for you - knowing
this project's rules, and proving the result still builds.

The bargain, stated plainly:

* rn-agent's ``FileManager`` backups do **not** cover these edits, because they
  never pass through it. A clean git tree is required instead, so ``git restore .``
  is an exact undo, and a branch is cut so the previous tip never moves.
* The project's rules become a ``permissions.deny`` list in Cursor's own
  ``.cursor/cli.json`` *before* the agent starts, so a forbidden write is refused
  by Cursor rather than merely reported afterwards.
* What actually changed is then audited against those same rules and validated
  with the usual install/typecheck/test steps.

On failure nothing destructive runs. The command prints the exact restore
command and exits non-zero - the same rule ``GitManager`` follows everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agents.cursor_agent import CursorAgentRunner, DelegationOutcome
from ..agents.rules import ProjectRules
from ..cli import ui
from ..core.command import AgentCommand
from ..core.context import AgentContext
from ..core.registry import register
from ..errors import RNAgentError
from ..models.project import ProjectContext
from ..models.validation import ValidationReport
from .health import CONTEXT_STALE_SECONDS

#: An agent run is minutes, not seconds. This is not an API call.
DEFAULT_TIMEOUT = 900.0


def _developer_changes(status: Any) -> bool:
    """Whether the tree holds work of the developer's that an undo would lose.

    ``.rn-agent/`` is this agent's own state - context snapshots, logs, the
    knowledge db. It is not work, ``git restore .`` never touches it, and letting
    it block the run would mean every project without that line in `.gitignore`
    could never delegate. (`rn-agent health` is what advises adding the line.)
    """
    paths = (
        *status.modified,
        *status.staged,
        *status.untracked,
        *status.conflicted,
    )
    return any(not path.replace("\\", "/").startswith(".rn-agent/") for path in paths)


@dataclass(slots=True)
class DelegateAnalysis:
    project: ProjectContext
    rules: ProjectRules
    executable: str
    dirty: bool


@dataclass(slots=True)
class DelegatePlan:
    task: str
    denied: list[str]
    branch: str | None = None
    permissions_file: Path | None = None
    previous_permissions: str | None = None


class DelegateCommand(AgentCommand[DelegateAnalysis, DelegatePlan]):
    name = "delegate"
    description = "Hand a task to the Cursor agent, then audit and validate what it changed"
    read_only = False
    requires_context = True

    def __init__(
        self,
        context: AgentContext,
        *,
        task: str | None = None,
        model: str | None = None,
        checks: tuple[str, ...] = ("typecheck", "tests"),
        allow_native: bool = False,
        allow_dependencies: bool = False,
        allow_dirty: bool = False,
        branch: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        verbose: bool = False,
    ) -> None:
        super().__init__(context)
        self.task = (task or "").strip()
        self.model = model
        self.checks = checks
        self.allow_native = allow_native
        self.allow_dependencies = allow_dependencies
        self.allow_dirty = allow_dirty
        self.branch = branch
        self.timeout = timeout
        self.verbose = verbose
        self.outcome: DelegationOutcome | None = None
        self.validation: ValidationReport | None = None

    # -- phases ------------------------------------------------------------
    def analyze(self) -> DelegateAnalysis:
        if not self.task:
            raise RNAgentError(
                "delegate needs a task",
                hint='Say what to do, e.g. `rn-agent delegate "extract the header into a component"`.',
            )
        project, _ = self.context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
        rules = ProjectRules.load(self.context.paths)
        executable = self._runner(rules).executable()
        status = self.context.git.status()
        dirty = _developer_changes(status)
        if not self.context.dry_run:
            self.context.git.require_repository()
            if dirty and not self.allow_dirty:
                raise RNAgentError(
                    "the working tree has uncommitted changes",
                    hint=(
                        "Commit or stash first: a clean tree is what makes `git restore .` an "
                        "exact undo of what the agent writes. Or pass --allow-dirty and own that."
                    ),
                )
        return DelegateAnalysis(
            project=project, rules=rules, executable=executable, dirty=dirty
        )

    def plan(self, analysis: DelegateAnalysis) -> DelegatePlan:
        runner = self._runner(analysis.rules)
        return DelegatePlan(task=self.task, denied=runner.deny_list())

    def execute(self, plan: DelegatePlan) -> None:
        agent = self.context
        if agent.dry_run:
            # A dry run must not let the agent write, so it does not run at all.
            # Showing the command and the deny list is the whole preview.
            return

        if self.branch:
            plan.branch = agent.git.create_branch("rn-agent-delegate")

        runner = self._runner(ProjectRules.load(agent.paths))
        plan.permissions_file, plan.previous_permissions = runner.write_permissions()
        try:
            with ui.working():
                summary, duration = runner.run(plan.task)
        finally:
            # The deny list is ours, not the developer's configuration.
            runner.restore_permissions(plan.permissions_file, plan.previous_permissions)

        changed = tuple(agent.git.diff_names())
        violations = tuple(runner.audit(list(changed)))
        self.outcome = DelegationOutcome(
            ran=True,
            changed=changed,
            violations=violations,
            branch=plan.branch,
            summary=summary,
            duration_ms=duration,
            recoverable=not self.allow_dirty,
        )

    def validate(self, plan: DelegatePlan) -> dict[str, Any]:
        _ = plan
        outcome = self.outcome
        if outcome is None or not outcome.changed:
            return {"ok": True, "changed": 0}

        from ..validation.runner import ProjectValidator

        report = ProjectValidator(self.context).run(list(self.checks))
        self.validation = report
        return {
            "ok": report.ok and not outcome.violations,
            "changed": len(outcome.changed),
            "violations": len(outcome.violations),
            "checks": report.summary() if hasattr(report, "summary") else None,
        }

    # -- reporting ---------------------------------------------------------
    def render(self, analysis: DelegateAnalysis, plan: DelegatePlan) -> None:
        if self.quiet:
            return
        if self.context.dry_run:
            ui.header("delegate (dry run)", analysis.executable)
            ui.note("nothing ran; this is what would be sent")
            ui.blank()
            ui.bullet(f"task: {plan.task}")
            ui.section(f"Denied to the agent ({len(plan.denied)})")
            for entry in plan.denied:
                ui.code(entry, indent=4)
            return

        outcome = self.outcome
        if outcome is None:
            return
        ui.header("delegate", f"Cursor · {analysis.executable}")
        if outcome.summary:
            ui.indented(outcome.summary)
            ui.blank()
        if not outcome.changed:
            ui.note("the agent changed nothing")
            return
        ui.section(f"Changed ({len(outcome.changed)})")
        for path in outcome.changed:
            ui.code(path, indent=4)
        if outcome.violations:
            ui.section(f"Rule violations ({len(outcome.violations)})")
            for violation in outcome.violations:
                ui.failure(f"{violation.path}: {violation.detail}")
        if self.validation is not None and not self.validation.ok:
            ui.blank()
            ui.warning("the project does not build after these edits")
        if outcome.violations or (self.validation is not None and not self.validation.ok):
            ui.blank()
            if outcome.recoverable:
                ui.bullet("discard the agent's work with `git restore .`")
            else:
                ui.warning(
                    "the tree was already dirty, so `git restore .` would also discard "
                    "your own uncommitted work"
                )

    # -- internals ---------------------------------------------------------
    def _runner(self, rules: ProjectRules) -> CursorAgentRunner:
        return CursorAgentRunner(
            root=self.context.paths.project_root,
            runner=self.context.runner,
            rules=rules,
            model=self._model(),
            timeout=self.timeout,
            allow_native=self.allow_native,
            allow_dependencies=self.allow_dependencies,
            credential=self._credential(),
            logger=self.logger,
        )

    def _model(self) -> str | None:
        """Which model Cursor should use, and when to say nothing at all.

        ``ai.model`` belongs to the configured provider: handing Cursor an
        Anthropic or OpenAI id would just be rejected. So the configured model is
        forwarded only when Cursor *is* the configured provider; otherwise Cursor
        picks its own default, which is the right answer rather than a guess.
        """
        if self.model:
            return self.model
        ai = self.context.config.ai
        if ai.provider == "cursor":
            return ai.model_for("delegate")
        return None

    def _credential(self) -> str | None:
        """An explicit key, when the developer stored one. Otherwise Cursor's own."""
        try:
            return self.context.auth.credential("cursor")
        except RNAgentError:
            return None

    def summary(self, analysis: DelegateAnalysis, plan: DelegatePlan) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task": plan.task,
            "executable": analysis.executable,
            "denied": plan.denied,
            "dry_run": self.context.dry_run,
        }
        if self.outcome is not None:
            payload.update(self.outcome.as_dict())
        if self.validation is not None:
            payload["validation_ok"] = self.validation.ok
        return payload

    def exit_code(self, analysis: DelegateAnalysis, plan: DelegatePlan) -> int:
        """Non-zero when a rule was broken or the project stopped building.

        A delegated edit that violates the project's rules is a failure even if
        it compiles: the rules are the contract, and the developer asked for them.
        """
        _ = analysis, plan
        outcome = self.outcome
        if outcome is not None and outcome.violations:
            return 1
        if self.validation is not None and not self.validation.ok:
            return 1
        return 0


register(DelegateCommand, phase=8)
