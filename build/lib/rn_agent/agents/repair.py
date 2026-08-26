"""Ask the model to fix a failed install, typecheck, or test.

Used after ``migrate`` so a version bump that does not build can take a few
quiet repair rounds. Consent is asked once; later rounds reuse it and write
without a second prompt. ``fix`` / ``feature`` no longer enter this loop -
they keep the change the developer already confirmed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..cli import ui
from ..core.context import AgentContext
from ..errors import ConfirmationDeclined, ModelOutputError, ProviderError
from ..models.proposal import Proposal
from ..models.validation import ValidationReport
from .context_builder import ContextBuilder
from .engine import AIEngine
from .prompts import error_fix_messages
from .rules import ProjectRules

if TYPE_CHECKING:
    from .workflow import EditWorkflow

#: Interactive sessions get this many tries; ``--yes`` stays at one (the caller).
MAX_ROUNDS = 3


def gather_instruction(
    context: AgentContext,
    failing: Sequence[str],
    *,
    task: str,
    workflow: EditWorkflow | None = None,
) -> str | None:
    """Ask once whether to spend a model call.

    Returns ``None`` to skip this round. An empty string means: use the error
    log only. ``--yes`` accepts the canned analysis. A later round on the same
    workflow does not ask again.
    """
    if workflow is not None and workflow.repair_allowed is False:
        return None
    if workflow is not None and workflow.repair_allowed is True:
        return ""
    if not context.ai_ready():
        context.logger.info("no AI configured; skipping the repair round")
        if workflow is not None:
            workflow.repair_allowed = False
        return None
    model = context.config.ai.model_for(task) or "the configured model"
    names = ", ".join(failing) or "validation"
    allowed = context.safety.confirm(
        f"{names} failed. Repair with {model}?",
        default=True,
    )
    if workflow is not None:
        workflow.repair_allowed = allowed
    if not allowed:
        context.logger.info("repair round declined")
        return None
    return ""


def run_round(
    workflow: EditWorkflow,
    report: ValidationReport,
    *,
    what_changed: str,
    paths: Sequence[str],
    task: str,
    reason: str,
) -> bool:
    """One consent → prompt → apply cycle. True when something was written."""
    failing = [step.name for step in report.failures]
    instruction = gather_instruction(workflow.context, failing, task=task, workflow=workflow)
    if instruction is None:
        return False
    workflow.context.logger.info("asking the model to fix: %s", ", ".join(failing) or "validation")
    selected = ContextBuilder(workflow.context).select(
        paths=tuple(paths) or (),
        query=" ".join((instruction, *failing)).strip(),
    )
    try:
        engine = AIEngine(workflow.context)
        proposals = engine.propose(
            error_fix_messages(
                project=workflow.context.project,
                rules=ProjectRules.load(workflow.context.paths),
                context=selected,
                report=report,
                what_changed=what_changed,
                instruction=instruction,
            ),
            task=task,
        )
    except (ProviderError, ModelOutputError) as error:
        workflow.context.logger.warning(
            "the repair round did not produce a fix: %s", error.message
        )
        return False
    return apply_proposals(workflow, proposals.proposals, reason=reason)


def apply_proposals(
    workflow: EditWorkflow, proposals: Sequence[Proposal], *, reason: str
) -> bool:
    kept, _ = workflow.screen(proposals)
    if not kept:
        return False
    count = sum(len(p.usable_edits) for p in kept)
    try:
        outcome = workflow.apply(
            kept,
            reason=reason,
            question=f"Apply {count} repair change(s)?",
            confirmed=workflow.repair_allowed is True,
        )
    except ConfirmationDeclined:
        return False
    if outcome.wrote_anything:
        ui.note(f"applied {count} repair change(s)")
    return outcome.wrote_anything
