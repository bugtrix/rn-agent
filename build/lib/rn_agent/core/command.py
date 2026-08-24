"""The command contract (§37).

Every command implements the same four phases:

``analyze``  gather facts (read-only)
``plan``     decide what would change (read-only)
``execute``  apply the plan through FileManager/CommandRunner
``validate`` prove the result

``run()`` sequences them, records the run in the knowledge store and converts
any :class:`RNAgentError` into a rendered failure. Read-only commands simply
leave ``execute`` as a no-op, which is why ``scan`` and ``health`` can never
modify a project.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from ..errors import RNAgentError
from .context import AgentContext

Analysis = TypeVar("Analysis")
Plan = TypeVar("Plan")


@dataclass(slots=True)
class CommandOutcome:
    """What the CLI needs after a command finishes."""

    exit_code: int = 0
    summary: dict[str, Any] = field(default_factory=dict)
    error: RNAgentError | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class AgentCommand(ABC, Generic[Analysis, Plan]):
    """Base class for every rn-agent command."""

    name: str = "command"
    description: str = ""
    #: Read-only commands never enter the execute phase.
    read_only: bool = False
    #: Commands that need `rn-agent scan` to have run first.
    requires_context: bool = True

    def __init__(self, context: AgentContext) -> None:
        self.context = context
        self.logger = context.logger.getChild(self.name)
        #: JSON mode suppresses the Rich report without touching the pipeline.
        self.quiet = False

    # -- phases ------------------------------------------------------------
    @abstractmethod
    def analyze(self) -> Analysis:
        """Collect facts. Must not modify the project."""

    @abstractmethod
    def plan(self, analysis: Analysis) -> Plan:
        """Decide what should happen. Must not modify the project."""

    def execute(self, plan: Plan) -> None:
        """Apply the plan. Read-only commands leave this untouched."""
        return

    def validate(self, plan: Plan) -> dict[str, Any]:
        """Prove the outcome; returned data lands in the run summary."""
        return {}

    def render(self, analysis: Analysis, plan: Plan) -> None:
        """Present results to the developer (Rich output lives in reporting/)."""
        return

    def summary(self, analysis: Analysis, plan: Plan) -> dict[str, Any]:
        """Structured summary stored with the run."""
        return {}

    def exit_code(self, analysis: Analysis, plan: Plan) -> int:
        return 0

    # -- orchestration -----------------------------------------------------
    def run(self) -> CommandOutcome:
        context = self.context
        context.begin_run()
        try:
            analysis = self.analyze()
            plan = self.plan(analysis)
            if not self.read_only:
                self.execute(plan)
            validation = self.validate(plan)
            if not self.quiet:
                self.render(analysis, plan)
            summary = {**self.summary(analysis, plan), **validation}
            code = self.exit_code(analysis, plan)
            context.end_run(
                status="ok" if code == 0 else "failed", exit_code=code, summary=summary
            )
            return CommandOutcome(exit_code=code, summary=summary)
        except RNAgentError as error:
            self.logger.error("%s failed: %s", self.name, error.message)
            context.end_run(status="error", exit_code=error.exit_code, summary={"error": error.message})
            return CommandOutcome(exit_code=error.exit_code, error=error)
        except KeyboardInterrupt:  # pragma: no cover - interactive
            context.end_run(status="interrupted", exit_code=130)
            raise
        finally:
            context.close()
