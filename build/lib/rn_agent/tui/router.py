"""Slash commands: one table, and no second implementation of anything.

The rule this module exists to enforce is that ``/health --deep`` and
``rn-agent health --deep`` are the *same* code. Rather than re-declaring flags,
the project commands are dispatched by handing argv back to the real Typer
application with ``standalone_mode=False``: the same parser, the same defaults,
the same renderers, the same exit codes - only the process boundary is missing.
A flag added to the CLI works in the terminal the day it lands, and there is
nowhere for the two surfaces to drift apart.

The handful of commands that are *about the session* rather than the project -
``/login``, ``/provider``, ``/model``, ``/status``, ``/context``, ``/clear`` -
have no CLI equivalent to reuse, so they live here.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import RNAgentError
from .session import SessionManager

if TYPE_CHECKING:
    from .select import Choice


@dataclass(frozen=True, slots=True)
class RouteResult:
    """What the loop should do next."""

    handled: bool = True
    #: Leave the terminal.
    quit: bool = False
    exit_code: int = 0
    #: A line for the loop to print, when the handler did not render itself.
    message: str | None = None
    #: Rendered as a warning rather than plain output.
    warning: str | None = None

    @property
    def failed(self) -> bool:
        return self.exit_code != 0


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """One entry in the palette and one line in ``/help``."""

    name: str
    summary: str
    handler: Callable[[SessionManager, list[str]], RouteResult]
    aliases: tuple[str, ...] = ()
    group: str = "Agent"
    usage: str = ""
    #: True when the command sends a prompt to a model, so the router can refuse
    #: early with "connect an account" instead of failing mid-run.
    needs_ai: bool = False

    @property
    def slash(self) -> str:
        return f"/{self.name}"

    @property
    def help_usage(self) -> str:
        return self.usage or self.slash


#: Project commands that are simply the CLI, re-entered in-process.
#: ``(name, summary, group, needs_ai)`` - the flags come from the CLI itself.
CLI_COMMANDS: tuple[tuple[str, str, str, bool], ...] = (
    ("scan", "Detect the project and refresh the shared context", "Project", False),
    ("health", "Diagnose React Native, JS, Android and iOS configuration", "Project", False),
    ("compatibility", "Check this project against a React Native version", "Project", False),
    ("review", "Analyse components, hooks, state and performance", "Develop", True),
    ("fix", "Fix reported problems, then prove the project still builds", "Develop", True),
    ("feature", "Implement a feature following the existing architecture", "Develop", True),
    ("test", "Generate tests for your code and run them", "Develop", True),
    # Not `needs_ai`: this one needs the *Cursor* CLI, not rn-agent's provider,
    # and it checks for that itself rather than borrowing the wrong gate.
    ("delegate", "Hand a task to the Cursor agent, then audit what it changed", "Develop", False),
    ("upgrade", "Upgrade React Native, or JavaScript dependencies", "Maintain", False),
    ("migrate", "Migrate React Native to a newer version", "Maintain", False),
    ("docs", "Write project documentation from the scanned facts", "Maintain", True),
    ("release", "Prepare a release: versions, changelog, checklist", "Maintain", True),
)


@dataclass
class CommandRouter:
    """Resolves a line of input to a handler."""

    session: SessionManager
    #: Injected by the app so handlers can open pickers; ``None`` in tests and
    #: non-interactive sessions, which is what makes every handler fall back to
    #: arguments instead of blocking on a keypress.
    picker: Callable[..., Choice | None] | None = None
    #: Where interactive handlers live. Set by the app to avoid a circular
    #: import at module load; each entry is ``(name, handler)``.
    extra: dict[str, SlashCommand] = field(default_factory=dict)
    _table: dict[str, SlashCommand] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        for command in self._build():
            self._table[command.name] = command
            for alias in command.aliases:
                self._table[alias] = command

    # -- table -------------------------------------------------------------
    def _build(self) -> list[SlashCommand]:
        commands: list[SlashCommand] = []
        for name, summary, group, needs_ai in CLI_COMMANDS:
            commands.append(
                SlashCommand(
                    name=name,
                    summary=summary,
                    handler=_cli_handler(name),
                    group=group,
                    usage=f"/{name} [flags]",
                    needs_ai=needs_ai,
                )
            )
        commands.extend(self.extra.values())
        return commands

    def commands(self) -> list[SlashCommand]:
        """Every command, de-duplicated, in table order."""
        seen: dict[str, SlashCommand] = {}
        for command in self._table.values():
            seen.setdefault(command.name, command)
        return list(seen.values())

    def get(self, name: str) -> SlashCommand | None:
        return self._table.get(name.lstrip("/").casefold())

    def help_rows(self) -> list[tuple[str, str]]:
        rows = [(command.help_usage, command.summary) for command in self.commands()]
        rows.sort(key=lambda row: row[0])
        return rows

    # -- dispatch ----------------------------------------------------------
    def is_command(self, text: str) -> bool:
        return text.strip().startswith("/")

    def split(self, text: str) -> tuple[str, list[str]]:
        """``"/model claude-sonnet"`` -> ``("model", ["claude-sonnet"])``.

        Quotes are honoured (``/feature "add a screen"``) and an unbalanced quote
        degrades to a plain split rather than raising at the developer.
        """
        stripped = text.strip().lstrip("/")
        try:
            parts = shlex.split(stripped)
        except ValueError:
            parts = stripped.split()
        if not parts:
            return "", []
        return parts[0].casefold(), parts[1:]

    def dispatch(self, text: str) -> RouteResult:
        """Run the command named by ``text``."""
        name, args = self.split(text)
        if not name:
            return RouteResult(handled=False)
        command = self.get(name)
        if command is None:
            return RouteResult(
                handled=False,
                exit_code=1,
                warning=f"unknown command /{name} - /help lists them, Ctrl+K searches",
            )
        if command.needs_ai and not self.session.ready():
            snapshot = self.session.snapshot()
            hint = (
                f"/login {snapshot.provider}"
                if snapshot.provider and not snapshot.connected
                else "/login"
            )
            return RouteResult(
                exit_code=1,
                warning=f"{command.slash} needs a connected account - run {hint}",
            )
        try:
            return command.handler(self.session, args)
        except RNAgentError as error:
            # Expected failures are the agent's own vocabulary: show the message
            # and the hint, keep the session alive.
            detail = f"{error.message}" + (f" - {error.hint}" if error.hint else "")
            return RouteResult(exit_code=error.exit_code, warning=detail)
        except KeyboardInterrupt:
            return RouteResult(warning="cancelled")


# ---------------------------------------------------------------------------
# project commands: the CLI, re-entered
# ---------------------------------------------------------------------------
def _cli_handler(name: str) -> Callable[[SessionManager, list[str]], RouteResult]:
    def handler(session: SessionManager, args: list[str]) -> RouteResult:
        code = run_cli(session, [name, *args])
        return RouteResult(exit_code=code)

    return handler


def run_cli(session: SessionManager, argv: Sequence[str]) -> int:
    """Invoke the real CLI in this process and return its exit code.

    Click's standalone mode is left on deliberately: it renders usage errors and
    the agent's own error panels exactly as the command line does, then calls
    ``sys.exit``. Catching that ``SystemExit`` is what turns a process boundary
    into a function call - the terminal survives, the behaviour is identical, and
    there is still only one implementation of every flag.

    The session's flags are replayed as global options, so a dry-run terminal
    stays dry and a verbose one stays verbose.
    """
    from ..cli.app import app

    options: list[str] = ["--path", str(session.context.root)]
    if session.dry_run:
        options.append("--dry-run")
    if session.assume_yes:
        options.append("--yes")
    if session.verbose:
        options.append("--verbose")

    try:
        app(args=[*options, *argv])
    except SystemExit as exit_signal:
        code = exit_signal.code
        return int(code) if isinstance(code, int) else 0 if code is None else 1
    return 0


def command_choices(router: CommandRouter) -> list[Choice]:
    """The palette's rows: every command, grouped, with its summary."""
    from .select import Choice

    order = ("Session", "Project", "Develop", "Maintain", "Agent")
    commands = sorted(
        router.commands(),
        key=lambda command: (
            order.index(command.group) if command.group in order else len(order),
            command.name,
        ),
    )
    return [
        Choice(
            value=command.slash,
            label=command.slash,
            hint=command.summary,
            group=command.group,
            payload=command,
        )
        for command in commands
    ]


def parse_flags(args: Sequence[str], *, flags: Sequence[str] = ()) -> tuple[list[str], dict[str, str | bool]]:
    """Tiny parser for the session commands' own options.

    Only the interactive handlers use this - project commands hand their flags
    to the CLI parser instead. Names in ``flags`` are treated as booleans;
    everything else consumes the next token.
    """
    positional: list[str] = []
    options: dict[str, str | bool] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            key = token[2:]
            if key in flags:
                options[key] = True
            elif "=" in key:
                name, _, value = key.partition("=")
                options[name] = value
            elif index + 1 < len(args):
                options[key] = args[index + 1]
                index += 1
            else:
                options[key] = True
        else:
            positional.append(token)
        index += 1
    return positional, options


def as_bool(value: Any) -> bool:
    return bool(value) and value not in {"false", "no", "0"}
