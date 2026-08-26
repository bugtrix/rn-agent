"""What happens when a line is not a command.

Free text is an agent turn, the way Cursor or Claude Code is: the developer
types a prompt, the model may read the project and the web, then it stages
file writes and those land after one confirm. Slash commands still exist for
work that has its own engine (``/migrate``, ``/upgrade``).
"""

from __future__ import annotations

import shlex

from ..agents.context_builder import ContextBuilder
from ..agents.inspect import (
    MAX_ROUNDS,
    ToolCall,
    edits_from_reply,
    merge_edits,
    parse_tool,
    run_tool,
)
from ..agents.intent import STRONG, Detection, Intent, detect
from ..agents.prompts import chat_messages
from ..agents.rules import ProjectRules
from ..agents.workflow import EditWorkflow
from ..ai.types import Message
from ..cli import ui
from ..cli.working import set_working_label
from ..errors import ConfirmationDeclined, RNAgentError
from ..models.proposal import FileEdit, Proposal
from ..reporting import change_view
from .dialogs import Action, choose
from .router import CommandRouter, RouteResult
from .session import SessionManager

#: Only these intents interrupt chat - they have their own write engines.
COMMAND_FIRST = frozenset({Intent.MIGRATE, Intent.UPGRADE})


def answer(session: SessionManager, router: CommandRouter, text: str) -> RouteResult:
    """Answer a line of prose, or offer a command that writes files."""
    detection = detect(text)
    if (
        detection.intent in COMMAND_FIRST
        and detection.confidence >= STRONG
        and detection.actionable
    ):
        routed = _offer(session, router, text, detection)
        if routed is not None:
            return routed
    return _converse(session, router, text)


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
    invocation = slash_invocation(command, *detection.arguments)

    chosen = choose(
        "Run a command?",
        (
            Action("run", f"Run {invocation}", "the real command, same as the CLI"),
            Action("ask", "Answer as a question", "no files touched yet"),
            Action("cancel", "Cancel"),
        ),
        subtitle=text,
        lines=[f"[muted]matched \"{detection.reason}\"[/muted]"],
        default="ask",
    )
    if chosen == "run":
        return router.dispatch(invocation)
    if chosen == "cancel":
        return RouteResult(message="cancelled")
    return None


def _converse(session: SessionManager, router: CommandRouter, text: str) -> RouteResult:
    """Investigate, stage edits, apply. One confirm for the writes."""
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

    rules = ProjectRules.load(session.context.paths)
    selected = ContextBuilder(session.context).select(paths=("package.json",), query=text)
    messages = chat_messages(
        project=project,
        rules=rules,
        context=selected,
        question=text,
        history=list(session.history),
    )

    provider = session.provider()
    answer_text = ""
    truncated = False
    pending: list[FileEdit] = []
    try:
        with ui.working(label="Thinking"):
            for _round in range(MAX_ROUNDS):
                completion = provider.complete(messages, task="default")
                session.context.record_ai_usage(completion)
                payload = parse_tool(completion.text)
                if payload is not None:
                    call = run_tool(session.context, payload)
                    if call.edits:
                        pending = merge_edits(pending, list(call.edits))
                    _render_tool(call)
                    messages = [
                        *messages,
                        Message.assistant(completion.text),
                        Message.user(f"TOOL RESULT ({call.name} {call.detail}):\n{call.result}"),
                    ]
                    set_working_label(call.label)
                    continue
                proposed = edits_from_reply(completion.text)
                if proposed:
                    pending = merge_edits(pending, proposed)
                    answer_text = _summary_from_proposals(completion.text) or "Applying the file changes."
                    truncated = completion.truncated
                    break
                answer_text = completion.text
                truncated = completion.truncated
                break
            else:
                answer_text = (
                    "I ran out of lookup rounds before a final answer. "
                    "Ask a follow-up, or name the file you want me to read."
                )
    except KeyboardInterrupt:
        return RouteResult(warning="cancelled")
    except RNAgentError as error:
        detail = f"{error.message}" + (f" - {error.hint}" if error.hint else "")
        return RouteResult(exit_code=error.exit_code, warning=detail)

    applied = ""
    if pending:
        applied = _apply_pending(session, rules, pending, prompt=text)

    session.remember("user", text)
    session.remember("assistant", "\n".join(part for part in (answer_text, applied) if part))

    ui.blank()
    ui.console().print(answer_text.strip() or "[muted](no answer)[/muted]")
    if truncated:
        ui.warning("the answer hit the output limit - raise ai.max_output_tokens")
    return RouteResult()


def _apply_pending(
    session: SessionManager,
    rules: ProjectRules,
    pending: list[FileEdit],
    *,
    prompt: str,
) -> str:
    """Screen and write staged edits. One confirm, default yes."""
    title = prompt.strip().splitlines()[0][:72] or "chat edit"
    proposal = Proposal(id="chat-edit", title=title, summary=prompt.strip()[:200], edits=pending)
    workflow = EditWorkflow(session.context, rules=rules, task="chat", keep_on_failure=True)
    kept, refused = workflow.screen([proposal])
    if refused:
        change_view.render_refusals(refused)
    if not kept:
        return "no file changes survived the project rules"
    count = sum(len(item.usable_edits) for item in kept)
    try:
        outcome = workflow.apply(
            kept,
            reason=f"chat: {title}",
            question=f"Apply {count} file change(s)?",
            confirm_default=True,
        )
    except ConfirmationDeclined:
        ui.note("cancelled - nothing was written")
        return "file changes cancelled"
    change_view.render_outcome(outcome, dry_run=session.context.dry_run)
    if outcome.wrote_anything:
        return f"applied {len(outcome.applied)} file(s): {', '.join(outcome.applied)}"
    return "no files changed"


def _summary_from_proposals(text: str) -> str:
    from ..agents.output import extract_json
    from ..errors import ModelOutputError

    try:
        payload = extract_json(text)
    except ModelOutputError:
        return ""
    notes = payload.get("notes")
    if isinstance(notes, list):
        lines = [str(item).strip() for item in notes if str(item).strip()]
        if lines:
            return "\n".join(lines)
    for key in ("summary", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    proposals = payload.get("proposals")
    if isinstance(proposals, list) and proposals and isinstance(proposals[0], dict):
        title = proposals[0].get("title") or proposals[0].get("summary")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return ""


def _render_tool(call: ToolCall) -> None:
    title = {
        "read": "Read",
        "grep": "Grep",
        "glob": "Glob",
        "npm": "npm",
        "search": "Search",
        "fetch": "Fetch",
        "write": "Write",
        "delete": "Delete",
        "rename": "Rename",
    }.get(call.name, call.name)
    extra = f"  [muted]{call.summary}[/muted]" if call.summary else ""
    ui.console().print(f"  [info]• {title}[/info]  [value]{call.detail}[/value]{extra}")


def slash_invocation(
    command: str,
    *arguments: str,
    about: str | None = None,
    positional: str | None = None,
) -> str:
    """Build a slash command that ``dispatch`` can split without losing spaces."""
    parts = list(arguments)
    if about:
        parts.extend(["--about", about])
    if positional:
        parts.append(positional)
    if not parts:
        return f"/{command}"
    return f"/{command} " + " ".join(shlex.quote(part) for part in parts)


def suggestion_for(text: str) -> str | None:
    """The command a request maps to, for callers that only want the routing."""
    detection = detect(text)
    if not detection.actionable or detection.confidence < STRONG:
        return None
    return " ".join([f"/{detection.intent.value}", *detection.arguments])


def is_question(text: str) -> bool:
    return detect(text).intent is Intent.QUESTION
