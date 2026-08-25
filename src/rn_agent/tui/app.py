"""The interactive terminal: ``rn-agent`` with no arguments.

The loop is intentionally small. Everything it can delegate, it delegates: the
router owns command dispatch, the session owns state, the pickers own selection,
and the existing commands own the actual work. What lives here is the shell -
the prompt, the key bindings, and the decision about whether a line is a command,
a question, or a request that should become a command.

Three deliberate choices:

* **A tty is required, and its absence is not an error.** Without one the terminal
  prints the same status a developer would see, plus the command list, and exits
  zero. A CI job that runs ``rn-agent`` by accident gets information, not a hang.
* **Ctrl+K and Ctrl+P do not fight the terminal.** Ctrl+C cancels the current
  line, Ctrl+D exits, Ctrl+K opens the palette, Ctrl+P cycles models - none of
  which are reserved by readline conventions the way Ctrl+A/E/W are.
* **History is a file.** ``~/.config/rn-agent/history`` so up-arrow works across
  sessions, and never inside the project.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from ..ai.models import ModelRegistry
from ..auth.manager import AuthenticationManager
from ..cli import ui
from ..core.context import AgentContext
from ..core.logging import get_logger
from ..core.paths import user_config_dir
from ..errors import RNAgentError
from . import chrome, handlers
from .palette import open_palette
from .router import CommandRouter, RouteResult
from .select import select
from .session import SessionManager
from .theme import colors_enabled, interactive_terminal

HISTORY_FILE = "history"


class SlashCompleter(Completer):
    """Completes ``/`` commands as you type. Plain prose is left alone."""

    def __init__(self, router: CommandRouter) -> None:
        self._router = router

    def get_completions(self, document: Any, complete_event: Any) -> Any:
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/") or " " in text:
            return
        needle = text[1:].casefold()
        for command in sorted(self._router.commands(), key=lambda item: item.name):
            if command.name.startswith(needle):
                yield Completion(
                    command.name,
                    start_position=-len(needle),
                    display=command.slash,
                    display_meta=command.summary,
                )


@dataclass
class Terminal:
    """One interactive run."""

    session: SessionManager
    router: CommandRouter = field(init=False)
    logger: logging.Logger = field(default_factory=lambda: get_logger("tui"))
    _prompt: PromptSession[str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.router = CommandRouter(session=self.session, picker=select)
        # The session commands need the router (for /help) and the router needs
        # them (for dispatch), so they are bound after both exist.
        self.router.extra.update(handlers.session_commands(picker=select, router=self.router))
        self.router.__post_init__()

    # -- the loop ----------------------------------------------------------
    def run(self) -> int:
        snapshot = self.session.snapshot()
        if self.session.context.config.ui.banner:
            chrome.render_banner(snapshot)
        exit_code = 0
        while True:
            try:
                line = self._read()
            except (EOFError, KeyboardInterrupt):
                ui.blank()
                ui.note("bye")
                return exit_code
            if line is None:
                continue
            text = line.strip()
            if not text:
                continue
            result = self._handle(text)
            if result.quit:
                return result.exit_code
            exit_code = result.exit_code

    def _read(self) -> str | None:
        prompt = self._session_prompt()
        return prompt.prompt(FormattedText([("class:prompt.arrow", "\n> ")]))

    def _handle(self, text: str) -> RouteResult:
        try:
            if self.router.is_command(text):
                result = self.router.dispatch(text)
            else:
                result = self._ask(text)
        except KeyboardInterrupt:
            ui.note("cancelled")
            return RouteResult(warning="cancelled")
        if result.message:
            ui.note(result.message)
        if result.warning:
            ui.warning(result.warning)
        if self.session.context.config.ui.status_bar:
            chrome.render_status(self.session.snapshot())
        return result

    # -- prose -------------------------------------------------------------
    def _ask(self, text: str) -> RouteResult:
        """A line that is not a command: route it, or answer it."""
        from .agent import answer

        return answer(self.session, self.router, text)

    # -- prompt ------------------------------------------------------------
    def _session_prompt(self) -> PromptSession[str]:
        if self._prompt is None:
            history_path = user_config_dir() / HISTORY_FILE
            history_path.parent.mkdir(parents=True, exist_ok=True)
            self._prompt = PromptSession(
                history=FileHistory(str(history_path)),
                completer=SlashCompleter(self.router),
                complete_while_typing=True,
                key_bindings=self._bindings(),
                enable_history_search=True,
            )
        return self._prompt

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("c-k")
        def _palette(event: Any) -> None:
            """Ctrl+K: pick a command, then run it as if it were typed."""
            command = open_palette(self.router)
            if command is None:
                return
            event.app.current_buffer.text = f"{command.slash} "
            event.app.current_buffer.cursor_position = len(event.app.current_buffer.text)

        @bindings.add("c-p")
        def _cycle(event: Any) -> None:
            """Ctrl+P: next model on the connected provider."""
            try:
                chosen = handlers.model_cycle(self.session)
            except RNAgentError as error:
                ui.warning(error.message)
                return
            if chosen is None:
                ui.note("only one model available - /model to see the list")
            else:
                ui.success(f"model: {chosen}")

        return bindings


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def build_session(
    *,
    start: Path | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
    verbose: bool = False,
) -> SessionManager:
    """Everything one interactive run needs, assembled once."""
    context = AgentContext.create(
        command="terminal",
        start=start,
        dry_run=dry_run,
        assume_yes=assume_yes,
        verbose=verbose,
        confirmer=ui.confirm,
    )
    return SessionManager(
        context=context,
        auth=AuthenticationManager(logger=get_logger("auth")),
        registry=ModelRegistry(),
        dry_run=dry_run,
        assume_yes=assume_yes,
        verbose=verbose,
    )


def run(
    *,
    start: Path | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
    verbose: bool = False,
) -> int:
    """Open the terminal, or explain why it cannot open.

    A directory that is not a React Native project is the common case here -
    someone typed ``rn-agent`` in the wrong folder - so it gets the same error
    panel and exit code as any other command, not a traceback.
    """
    try:
        session = build_session(
            start=start, dry_run=dry_run, assume_yes=assume_yes, verbose=verbose
        )
    except RNAgentError as error:
        ui.error_panel(error.message, error.hint)
        return error.exit_code
    config = session.context.config.ui
    if not config.interactive or not interactive_terminal():
        return _static_status(session)
    ui.reset_console(None)
    _ = colors_enabled(configured=config.colors)
    return Terminal(session=session).run()


def _static_status(session: SessionManager) -> int:
    """What a non-interactive ``rn-agent`` prints instead of taking the screen."""
    snapshot = session.snapshot()
    chrome.render_banner(snapshot)
    router = CommandRouter(session=session)
    router.extra.update(handlers.session_commands(router=router))
    router.__post_init__()
    chrome.render_help(router.help_rows())
    ui.blank()
    ui.note(
        "this terminal is not interactive (piped output, or ui.interactive: false) - "
        "run a command directly, e.g. `rn-agent health`"
    )
    return 0
