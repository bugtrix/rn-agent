"""What every project command does around the command itself.

Building the shared context, rendering an expected failure as a panel instead of
a traceback, suppressing the Rich report in ``--json`` mode and exiting with the
command's own code is identical for ``scan``, ``health`` and every phase 3-6
command. It lives here so the three CLI modules cannot drift apart, and so a new
command needs one Typer function and nothing else.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, NoReturn

import typer

from ..core.command import AgentCommand, CommandOutcome
from ..core.context import AgentContext
from ..errors import RNAgentError
from ..validation.runner import STEP_NAMES
from . import ui
from .options import OPTIONS


def build_context(command: str) -> AgentContext:
    """The shared brain for one invocation, or an exit with a rendered error."""
    try:
        return AgentContext.create(
            command=command,
            start=OPTIONS.path,
            dry_run=OPTIONS.dry_run,
            assume_yes=OPTIONS.yes,
            verbose=OPTIONS.verbose,
            confirmer=ui.confirm,
        )
    except RNAgentError as error:
        ui.error_panel(error.message, error.hint)
        raise typer.Exit(error.exit_code) from error


def finish(outcome: CommandOutcome, payload: dict[str, Any] | None = None) -> NoReturn:
    """Render an error (if any), emit JSON (if asked) and exit."""
    if outcome.error is not None:
        ui.error_panel(outcome.error.message, outcome.error.hint)
        raise typer.Exit(outcome.exit_code)
    if OPTIONS.json_output and payload is not None:
        ui.console().print_json(json.dumps(payload, default=str))
    raise typer.Exit(outcome.exit_code)


def execute(command: AgentCommand) -> NoReturn:
    """Run a command, honouring ``--json`` for both output and rendering.

    In JSON mode the Rich report is suppressed and the command's own report
    object is serialised, so ``--json`` works in dry-run too - where no report
    file is written.
    """
    if not OPTIONS.json_output:
        finish(command.run())
    command.quiet = True
    outcome = command.run()
    finish(outcome, report_payload(command, outcome))


def report_payload(command: AgentCommand, outcome: CommandOutcome) -> dict[str, Any]:
    """A command's report as JSON, falling back to its run summary."""
    report = getattr(command, "report", None)
    dump = getattr(report, "model_dump", None)
    if callable(dump):
        payload = dump(mode="json")
        if isinstance(payload, dict):
            return payload
    return outcome.summary


def resolve_checks(
    selected: Sequence[str] | None, *, disabled: bool, default: tuple[str, ...]
) -> tuple[str, ...]:
    """Turn ``--check``/``--no-check`` into the validator's step names."""
    if disabled:
        return ()
    if not selected:
        return default
    unknown = [name for name in selected if name not in STEP_NAMES]
    if unknown:
        ui.error_panel(
            f"unknown validation step(s): {', '.join(unknown)}",
            f"Known steps: {', '.join(STEP_NAMES)}.",
        )
        raise typer.Exit(1)
    return tuple(dict.fromkeys(selected))


def as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    """Typer gives ``None`` for an unused repeatable option."""
    return tuple(values or ())
