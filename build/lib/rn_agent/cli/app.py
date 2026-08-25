"""The rn-agent CLI.

Thin by design: it parses flags, builds one :class:`AgentContext` (the shared
brain) and hands control to a registered command. Project commands live here;
the AI setup commands (``login``, ``provider``, ``model``, ...) attach
themselves from ``cli/auth.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from ..constants import APP_TITLE, APP_VERSION
from ..core.command import AgentCommand, CommandOutcome
from ..core.context import AgentContext
from ..errors import RNAgentError
from ..models.project import ProjectContext
from . import auth, develop, maintain, runtime, ui
from .options import OPTIONS

app = typer.Typer(
    name="rn-agent",
    help=f"{APP_TITLE}: one agent for scanning, diagnosing, fixing and migrating React Native apps.",
    add_completion=False,
    # Bare `rn-agent` opens the interactive terminal rather than printing help:
    # the agent is a place you work, and every subcommand still behaves exactly
    # as it did. `--help` is one keystroke away and unchanged.
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_enable=False,
)


def _version_callback(value: bool) -> None:
    if value:
        ui.console().print(f"{APP_TITLE} {APP_VERSION}")
        raise typer.Exit(0)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    path: Annotated[
        Path | None,
        typer.Option("--path", "-C", help="Project directory (default: current directory)."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Never write anything; show what would happen.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Answer yes to confirmation prompts.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose output and logs.")] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable output instead of the Rich report.")
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    OPTIONS.path = path
    OPTIONS.dry_run = dry_run
    OPTIONS.yes = yes
    OPTIONS.verbose = verbose
    OPTIONS.json_output = json_output
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand: open the terminal. Imported here so the AI and
    # prompt_toolkit stacks stay out of `rn-agent scan`.
    from ..tui.app import run as run_terminal

    raise typer.Exit(
        run_terminal(start=path, dry_run=dry_run, assume_yes=yes, verbose=verbose)
    )


def _build_context(command: str) -> AgentContext:
    return runtime.build_context(command)


def _finish(outcome: CommandOutcome, payload: dict[str, Any] | None = None) -> None:
    """Render an error (if any) and exit with the command's code."""
    runtime.finish(outcome, payload)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
@app.command()
def scan(
    show: Annotated[
        bool, typer.Option("--show", help="Print the stored context without rescanning.")
    ] = False,
    no_tools: Annotated[
        bool, typer.Option("--no-tools", help="Skip node/java/pod version probes (faster).")
    ] = False,
) -> None:
    """Detect the project and build the context every command shares."""
    from ..commands.scan import ScanCommand
    from ..reporting.scan_view import render_scan

    if show:
        try:
            context = AgentContext.create(command="scan", start=OPTIONS.path)
            project = context.project
        except RNAgentError as error:
            ui.error_panel(error.message, error.hint)
            raise typer.Exit(error.exit_code) from error
        if OPTIONS.json_output:
            ui.console().print_json(json.dumps(project.model_dump(mode="json"), default=str))
        else:
            render_scan(project, verbose=OPTIONS.verbose, wrote=True)
        raise typer.Exit(0)

    try:
        context = _build_context("scan")
    except RNAgentError as error:
        ui.error_panel(error.message, error.hint)
        raise typer.Exit(error.exit_code) from error

    command = ScanCommand(context, verbose=OPTIONS.verbose, probe_tools=not no_tools)
    if OPTIONS.json_output:
        outcome = _run_quiet(command)
        payload = _scan_payload(context)
    else:
        outcome = command.run()
        payload = None
    _finish(outcome, payload)


def _run_quiet(command: AgentCommand) -> CommandOutcome:
    """Run a command with its Rich report suppressed (JSON mode)."""
    command.quiet = True
    return command.run()


def _scan_payload(context: AgentContext) -> dict[str, Any]:
    project: ProjectContext = context.project
    return project.model_dump(mode="json")


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
@app.command()
def health(
    deep: Annotated[
        bool, typer.Option("--deep", help="Also run tsc --noEmit and eslint (slower).")
    ] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Rescan the project before diagnosing.")
    ] = False,
    fail_under: Annotated[
        int | None,
        typer.Option("--fail-under", help="Exit non-zero when the score is below this value."),
    ] = None,
    area: Annotated[
        list[str] | None,
        typer.Option(
            "--area",
            help="Limit to an area: project, react-native, javascript, android, ios.",
        ),
    ] = None,
) -> None:
    """Diagnose React Native, JavaScript, Android and iOS configuration."""
    from ..commands.health import HealthCommand

    try:
        context = _build_context("health")
    except RNAgentError as error:
        ui.error_panel(error.message, error.hint)
        raise typer.Exit(error.exit_code) from error

    command = HealthCommand(
        context,
        deep=deep,
        verbose=OPTIONS.verbose,
        refresh=refresh,
        fail_under=fail_under,
        categories=tuple(area or ()),
    )
    if OPTIONS.json_output:
        outcome = _run_quiet(command)
        # Serialise the in-memory report so --json works in dry-run too,
        # where no report file is written.
        payload: dict[str, Any] | None = (
            command.report.model_dump(mode="json") if command.report else outcome.summary
        )
    else:
        outcome = command.run()
        payload = None
    _finish(outcome, payload)


# ---------------------------------------------------------------------------
# context (inspect the shared brain)
# ---------------------------------------------------------------------------
@app.command()
def info() -> None:
    """Show where rn-agent keeps state for this project."""
    try:
        context = AgentContext.create(command="info", start=OPTIONS.path)
    except RNAgentError as error:
        ui.error_panel(error.message, error.hint)
        raise typer.Exit(error.exit_code) from error

    paths = context.paths
    scanned = paths.context_file.is_file()
    ui.header(f"{APP_TITLE} {APP_VERSION}", "project state")
    ui.key_values(
        [
            ("project root", paths.project_root),
            ("agent dir", paths.agent_dir),
            ("config", paths.config_file if paths.config_file.exists() else "not created yet"),
            ("context", paths.context_file if scanned else "run `rn-agent scan`"),
            ("knowledge db", paths.knowledge_db if paths.knowledge_db.exists() else "-"),
            ("logs", paths.logs_dir if paths.logs_dir.exists() else "-"),
            ("ai provider", context.config.ai.provider or "not configured"),
            ("ai model", context.config.ai.model or "-"),
            ("ai credential", _credential_source(context) or "none"),
        ]
    )
    if scanned:
        project = context.project
        ui.blank()
        ui.key_values(
            [
                ("scanned at", project.generated_at),
                ("react-native", project.rn_version),
                ("dependencies", len(project.dependencies)),
                ("native modules", len(project.native_modules)),
            ]
        )
    ui.blank()
    ui.note("`rn-agent whoami` shows the AI setup in detail")
    context.close()
    raise typer.Exit(0)


def _credential_source(context: AgentContext) -> str | None:
    """Where the provider key would come from, without asking for a network."""
    from ..ai.registry import resolve_spec

    provider = context.config.ai.provider
    if not provider:
        return None
    try:
        credential = context.credentials.resolve(resolve_spec(provider))
    except RNAgentError as error:
        return error.message
    return credential.describe() if credential else None


# ---------------------------------------------------------------------------
# The other command groups attach themselves, so this router never has to know
# which commands exist: AI setup (phase 2), development (phase 3) and
# maintenance (phases 4-6).
# ---------------------------------------------------------------------------
auth.register(app)
develop.register(app)
maintain.register(app)


#: Global flags live on the group, which Click requires *before* the subcommand.
#: Developers reasonably type `rn-agent health --json`, so they are hoisted.
#: ``--path`` takes a value; the rest are switches. No subcommand defines any of
#: these names, so hoisting cannot shadow a command's own flag.
GLOBAL_SWITCHES: frozenset[str] = frozenset(
    {"--dry-run", "--yes", "-y", "--verbose", "-v", "--json", "--version"}
)
GLOBAL_VALUED: frozenset[str] = frozenset({"--path", "-C"})


def hoist_global_flags(argv: list[str]) -> list[str]:
    """Move group-level flags in front of the subcommand.

    ``rn-agent health --json`` and ``rn-agent --json health`` are the same
    request; only one of them parses without this.
    """
    globals_: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":  # everything after this is positional
            rest.extend(argv[index:])
            break
        if token in GLOBAL_SWITCHES:
            globals_.append(token)
        elif token in GLOBAL_VALUED and index + 1 < len(argv):
            globals_.extend([token, argv[index + 1]])
            index += 1
        elif any(token.startswith(f"{name}=") for name in GLOBAL_VALUED | GLOBAL_SWITCHES):
            globals_.append(token)
        else:
            rest.append(token)
        index += 1
    return [*globals_, *rest]


def main() -> None:
    """Console-script entry point."""
    import sys

    try:
        app(args=hoist_global_flags(sys.argv[1:]))
    except RNAgentError as error:  # pragma: no cover - safety net
        ui.error_panel(error.message, error.hint)
        raise SystemExit(error.exit_code) from error


if __name__ == "__main__":  # pragma: no cover
    main()
