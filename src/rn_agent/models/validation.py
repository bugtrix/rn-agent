"""Proof that a change did not break the project.

``fix``, ``feature``, ``test``, ``upgrade``, ``migrate`` and ``release`` all
need the same answer - does this project still install, typecheck, lint, test
and build? - so the shape of that answer lives here and the runner lives in
``validation/runner.py``.

A step the project cannot run (no ``node_modules``, no ``tsconfig.json``, no
test script) reports ``SKIP`` with the reason. It never counts as a pass, and it
never counts as a failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StepStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class ValidationStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    status: StepStatus
    command: str = ""
    detail: str = ""
    duration_ms: int = 0
    #: Last lines of the tool's output, redacted. Enough to fix, not a dump.
    output_tail: str = ""

    @property
    def failed(self) -> bool:
        return self.status is StepStatus.FAIL

    @property
    def skipped(self) -> bool:
        return self.status is StepStatus.SKIP


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    steps: list[ValidationStep] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed. An all-skipped run is not a proof, but it
        is not a failure either - callers check :attr:`proved` for that."""
        return not any(step.failed for step in self.steps)

    @property
    def proved(self) -> bool:
        """True when at least one step actually ran and none failed."""
        return self.ok and any(step.status is StepStatus.PASS for step in self.steps)

    @property
    def failures(self) -> list[ValidationStep]:
        return [step for step in self.steps if step.failed]

    @property
    def skipped(self) -> list[ValidationStep]:
        return [step for step in self.steps if step.skipped]

    def step(self, name: str) -> ValidationStep | None:
        for step in self.steps:
            if step.name == name:
                return step
        return None

    def counts(self) -> dict[str, int]:
        return {
            "steps": len(self.steps),
            "passed": sum(1 for step in self.steps if step.status is StepStatus.PASS),
            "failed": len(self.failures),
            "skipped": len(self.skipped),
        }

    def failure_text(self, *, limit: int = 4) -> str:
        """Compact failure summary - what an AI error-fix prompt is given."""
        blocks = [
            f"$ {step.command or step.name}\n{step.output_tail}".strip()
            for step in self.failures[:limit]
        ]
        return "\n\n".join(blocks)
