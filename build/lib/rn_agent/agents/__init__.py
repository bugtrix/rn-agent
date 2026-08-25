"""The AI work layer: what the model is told, and what it is allowed to do.

Six commands share this package (``review``, ``fix``, ``feature``, ``test``,
``docs`` and the error repair inside ``migrate``), which is why the pieces are
separate:

* :mod:`~rn_agent.agents.rules` - the project's constraints, as prompt text and
  as enforcement;
* :mod:`~rn_agent.agents.context_builder` - which files may be sent, inside the
  configured budget, with secrets excluded;
* :mod:`~rn_agent.agents.prompts` - the exact wording and the output contract;
* :mod:`~rn_agent.agents.output` - decoding a reply, or refusing it;
* :mod:`~rn_agent.agents.engine` - one call path, with accounting and one repair
  attempt;
* :mod:`~rn_agent.agents.apply` - rules, risk, consent, write, rollback.
"""

from __future__ import annotations

from .apply import ApplyOutcome, EditApplier
from .context_builder import ContextBuilder, ContextFile, PromptContext, estimate_tokens
from .engine import AIEngine
from .rules import ProjectRules, RuleViolation, is_native_path

__all__ = [
    "AIEngine",
    "ApplyOutcome",
    "ContextBuilder",
    "ContextFile",
    "EditApplier",
    "ProjectRules",
    "PromptContext",
    "RuleViolation",
    "estimate_tokens",
    "is_native_path",
]
