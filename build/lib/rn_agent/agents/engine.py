"""The one place the agent asks a model for something.

Every AI-backed command goes through :class:`AIEngine`, so five behaviours exist
once instead of six times:

* the per-task model is selected (``ai.models.<task>``, then ``ai.model``);
* usage is recorded in the knowledge store for accounting;
* the raw reply is logged at debug level, redacted;
* a truncated reply is reported as truncated rather than parsed as if complete;
* an unparsable reply gets exactly **one** repair attempt - the parse error is
  handed back to the model - and then fails loudly.

One retry, not a loop: a model that cannot honour the contract twice will not
honour it on the fifth attempt either, and the developer is paying per token.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..ai.types import Completion, Message
from ..core.context import AgentContext
from ..errors import ModelOutputError
from ..models.proposal import ProposalSet
from ..models.review import ReviewFinding
from ..utils.redaction import redact
from . import output

REPAIR_INSTRUCTION = """\
Your previous reply could not be parsed: {error}

Reply again with the same content as valid JSON matching the contract exactly. \
Output the JSON object only - no prose, no markdown fence.\
"""


@dataclass(slots=True)
class AIEngine:
    """Sends prompts, records usage, decodes replies."""

    context: AgentContext
    #: Every completion this engine produced, for reporting and accounting.
    completions: list[Completion] = field(default_factory=list)

    # -- raw calls ---------------------------------------------------------
    def ask(
        self,
        messages: Sequence[Message],
        *,
        task: str,
        max_output_tokens: int | None = None,
    ) -> Completion:
        """One model call, accounted for and logged."""
        provider = self.context.ai
        model = self.context.config.ai.model_for(task)
        completion = provider.complete(
            messages,
            model=model,
            max_output_tokens=max_output_tokens,
            task=task,
        )
        self.completions.append(completion)
        self.context.record_ai_usage(completion)
        self.context.logger.debug(
            "%s reply (%s tokens): %s",
            task,
            completion.usage.output_tokens,
            redact(completion.text[:2000]),
        )
        if completion.truncated:
            raise ModelOutputError(
                f"the model's {task} answer was cut off at the output limit",
                hint=(
                    "Raise ai.max_output_tokens in .rn-agent/config.yaml, or narrow the "
                    "request (fewer files, one issue at a time)."
                ),
            )
        return completion

    # -- typed calls -------------------------------------------------------
    def propose(self, messages: Sequence[Message], *, task: str) -> ProposalSet:
        """Ask for file edits and decode them, with one repair attempt."""
        completion = self.ask(messages, task=task)
        try:
            return output.parse_proposals(completion.text, task=task, completion=completion)
        except ModelOutputError as error:
            repaired = self._repair(messages, completion, error, task=task)
            return output.parse_proposals(repaired.text, task=task, completion=repaired)

    def review(self, messages: Sequence[Message], *, task: str = "review") -> tuple[
        list[ReviewFinding], list[str], Completion
    ]:
        """Ask for findings; returns ``(findings, notes, completion)``."""
        completion = self.ask(messages, task=task)
        try:
            findings, notes = output.parse_review(completion.text)
        except ModelOutputError as error:
            completion = self._repair(messages, completion, error, task=task)
            findings, notes = output.parse_review(completion.text)
        return findings, notes, completion

    def changelog(
        self, messages: Sequence[Message], *, task: str = "docs"
    ) -> tuple[list[str], list[str]]:
        completion = self.ask(messages, task=task)
        try:
            return output.parse_changelog(completion.text)
        except ModelOutputError as error:
            completion = self._repair(messages, completion, error, task=task)
            return output.parse_changelog(completion.text)

    @property
    def usage(self) -> dict[str, int]:
        return {
            "calls": len(self.completions),
            "input_tokens": sum(item.usage.input_tokens for item in self.completions),
            "output_tokens": sum(item.usage.output_tokens for item in self.completions),
        }

    @property
    def model(self) -> str | None:
        return self.completions[-1].model if self.completions else None

    @property
    def provider(self) -> str | None:
        return self.completions[-1].provider if self.completions else None

    # -- internals ---------------------------------------------------------
    def _repair(
        self,
        messages: Sequence[Message],
        completion: Completion,
        error: ModelOutputError,
        *,
        task: str,
    ) -> Completion:
        """Hand the parse error back once, then let the next failure through."""
        self.context.logger.info("%s reply unparsable (%s); asking once more", task, error.message)
        conversation = [
            *messages,
            Message.assistant(completion.text[:4000] or "(empty reply)"),
            Message.user(REPAIR_INSTRUCTION.format(error=error.message)),
        ]
        return self.ask(conversation, task=task)

