"""What happens when a line is not a command.

Two paths, and choosing between them is deterministic:

1. **The request names work the agent already does.** "fix my android build" is
   ``/fix``; "can I move to 0.86?" is ``/compatibility --target 0.86``. The
   suggestion is offered - never executed silently - because running a command
   that writes files on the strength of a keyword match would be exactly the kind
   of surprise this project avoids.
2. **Anything else is a question**, answered with the scanned project as context
   and the conversation so far. No files are written on this path at all.

The context sent to the model is the same budgeted, secret-filtered selection
every other AI command uses, and the token cost is printed after the answer, so
"what did that cost" never needs guessing.
"""

from __future__ import annotations

from ..agents.context_builder import ContextBuilder
from ..agents.intent import Detection, Intent, detect
from ..agents.prompts import chat_messages
from ..agents.rules import ProjectRules
from ..cli import ui
from ..errors import RNAgentError
from .dialogs import Action, choose
from .router import CommandRouter, RouteResult
from .session import SessionManager

#: Below this, a suggestion is offered as one option among "just answer it".
OFFER_THRESHOLD = 0.6


def answer(session: SessionManager, router: CommandRouter, text: str) -> RouteResult:
    """Route or answer one line of prose."""
    detection = detect(text)
    if detection.actionable and detection.confidence >= OFFER_THRESHOLD:
        routed = _offer(session, router, text, detection)
        if routed is not None:
            return routed
    return _converse(session, text)


def _offer(
    session: SessionManager,
    router: CommandRouter,
    text: str,
    detection: Detection,
) -> RouteResult | None:
    """Propose the matching command. ``None`` means "answer it instead"."""
    command = detection.intent.command
    if command is None:  # pragma: no cover - guarded by caller
        return None
    invocation = " ".join([f"/{command}", *detection.arguments])

    chosen = choose(
        "Run a command?",
        (
            Action("run", f"Run {invocation}", "the real command, same as the CLI"),
            Action("ask", "Answer as a question", "no files touched"),
            Action("cancel", "Cancel"),
        ),
        subtitle=text,
        lines=[f"[muted]matched \"{detection.reason}\"[/muted]"],
        # No tty: answering is the conservative choice, because a command may write.
        default="ask",
    )
    if chosen == "run":
        return router.dispatch(invocation)
    if chosen == "cancel":
        return RouteResult(message="cancelled")
    return None


def _converse(session: SessionManager, text: str) -> RouteResult:
    """Answer a question with project context. Writes nothing."""
    if not session.ready():
        snapshot = session.snapshot()
        hint = (
            f"/login {snapshot.provider}"
            if snapshot.provider and not snapshot.connected
            else "/login"
        )
        return RouteResult(
            exit_code=10,
            warning=f"no connected account to answer with - run {hint}",
        )

    try:
        project, _ = session.context.ensure_project()
    except RNAgentError as error:
        return RouteResult(exit_code=error.exit_code, warning=error.message)

    selected = ContextBuilder(session.context).select(query=text)
    messages = chat_messages(
        project=project,
        rules=ProjectRules.load(session.context.paths),
        context=selected,
        question=text,
        history=list(session.history),
    )

    provider = session.provider()
    try:
        with ui.working():
            completion = provider.complete(messages, task="default")
    except KeyboardInterrupt:
        return RouteResult(warning="cancelled")
    except RNAgentError as error:
        detail = f"{error.message}" + (f" - {error.hint}" if error.hint else "")
        return RouteResult(exit_code=error.exit_code, warning=detail)

    session.context.record_ai_usage(completion)
    session.remember("user", text)
    session.remember("assistant", completion.text)

    ui.blank()
    ui.console().print(completion.text.strip() or "[muted](no answer)[/muted]")
    if selected:
        ui.note(
            f"{len(selected)} file(s) · ~{selected.approx_tokens:,} context tokens · "
            f"{completion.usage.total_tokens:,} tokens this turn"
        )
    if completion.truncated:
        ui.warning("the answer hit the output limit - raise ai.max_output_tokens")
    return RouteResult()


def suggestion_for(text: str) -> str | None:
    """The command a request maps to, for callers that only want the routing."""
    detection = detect(text)
    if not detection.actionable or detection.confidence < OFFER_THRESHOLD:
        return None
    return " ".join([f"/{detection.intent.value}", *detection.arguments])


def is_question(text: str) -> bool:
    return detect(text).intent is Intent.QUESTION
