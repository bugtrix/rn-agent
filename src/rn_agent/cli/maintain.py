"""Maintenance commands: ``upgrade``, ``migrate``, ``compatibility``, ``docs``, ``release``.

Same shape as ``cli/develop.py``: parse, build the context, run one command.
Everything interesting - risk ranking, diff application, rollback, the report
file - belongs to the command, not to the router.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ..models.migration import StepKind
from ..upgrade.planner import POLICIES
from ..upgrade.versions import UpgradeRequest
from ..validation.runner import STEP_NAMES
from . import ui
from .runtime import as_tuple, build_context, execute, resolve_checks

CHECK_HELP = f"Validation step to run afterwards (repeatable): {', '.join(STEP_NAMES)}."


def upgrade(
    version: Annotated[
        str | None,
        typer.Argument(help="React Native version to move to, e.g. 0.86.0 or 0.86."),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option("--to", help="React Native version to move to."),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option(
            "--target",
            help=f"A React Native version, or a JS policy ({', '.join(POLICIES)}).",
        ),
    ] = None,
    deps: Annotated[
        bool,
        typer.Option("--deps", help="Upgrade JavaScript dependencies instead of React Native."),
    ] = False,
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Upgrade just this package (repeatable).")
    ] = None,
    skip: Annotated[
        list[str] | None, typer.Option("--skip", help="Leave this package alone (repeatable).")
    ] = None,
    native: Annotated[
        bool,
        typer.Option("--native", help="Include packages that ship native code."),
    ] = False,
    no_install: Annotated[
        bool, typer.Option("--no-install", help="Update package.json without installing.")
    ] = False,
    check: Annotated[list[str] | None, typer.Option("--check", help=CHECK_HELP)] = None,
    no_check: Annotated[
        bool, typer.Option("--no-check", help="Skip validation after upgrading.")
    ] = False,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Do not contact the registry (RN: cached diffs; JS: drift only).",
        ),
    ] = False,
) -> None:
    """Upgrade React Native to a chosen version, or bump JavaScript dependencies.

    With no flags, an interactive terminal asks which published React Native
    version to move to. Pass --deps (or --target patch|minor|latest) to bump
    packages instead. Scripts without a tty keep the old dependency-upgrade
    default.
    """
    from ..commands.migrate import MigrateCommand
    from ..commands.upgrade import UpgradeCommand
    from ..errors import RNAgentError
    from ..upgrade.versions import classify_upgrade, concrete_rn_version

    if version in POLICIES and target is None and to is None:
        target, version = version, None

    try:
        request = classify_upgrade(
            to=to or version,
            target=target,
            deps=deps,
            only=as_tuple(only),
            skip=as_tuple(skip),
            native=native,
        )
    except RNAgentError as error:
        ui.error_panel(error.message, error.hint)
        raise typer.Exit(error.exit_code) from error

    context = build_context("upgrade")
    try:
        if request.kind == "ask":
            request = _resolve_ask(context, offline=offline)
    except RNAgentError as error:
        ui.error_panel(error.message, error.hint)
        raise typer.Exit(error.exit_code) from error
    if request.kind == "rn":
        if request.version:
            try:
                request = UpgradeRequest(
                    kind="rn", version=concrete_rn_version(request.version, offline=offline)
                )
            except RNAgentError as error:
                ui.error_panel(error.message, error.hint)
                raise typer.Exit(error.exit_code) from error
        execute(
            MigrateCommand(
                context,
                to_version=request.version,
                install=not no_install,
                offline=offline,
                verbose=context.verbose,
            )
        )
    else:
        execute(
            UpgradeCommand(
                context,
                policy=request.policy,
                only=as_tuple(only),
                skip=as_tuple(skip),
                include_native=native,
                install=not no_install,
                checks=resolve_checks(check, disabled=no_check, default=("typecheck", "tests")),
                offline=offline,
                verbose=context.verbose,
            )
        )


def _resolve_ask(context: object, *, offline: bool) -> UpgradeRequest:
    """Interactive: pick an RN version. Piped/CI: keep the JS-deps default."""
    from ..tui.theme import interactive_terminal
    from .options import OPTIONS

    if OPTIONS.json_output or not interactive_terminal():
        return UpgradeRequest(kind="deps", policy="minor")
    chosen = _prompt_rn_version(context, offline=offline)
    if not chosen:
        ui.note("cancelled")
        raise typer.Exit(0)
    return UpgradeRequest(kind="rn", version=chosen)


def _prompt_rn_version(context: object, *, offline: bool) -> str | None:
    from ..commands.health import CONTEXT_STALE_SECONDS
    from ..core.context import AgentContext
    from ..errors import RNAgentError
    from ..tui.versions import pick_rn_version
    from ..tui.wizard import ask_version

    assert isinstance(context, AgentContext)
    project, _ = context.ensure_project(stale_seconds=CONTEXT_STALE_SECONDS)
    current = project.rn_version
    if not current:
        raise RNAgentError(
            "the project's React Native version could not be established",
            hint="Run your package manager's install, then `rn-agent scan`.",
        )
    return pick_rn_version(current, offline=offline, asker=ask_version)


def migrate(
    to: Annotated[
        str | None, typer.Option("--to", help="Target React Native version (default: newest).")
    ] = None,
    kind: Annotated[
        list[str] | None,
        typer.Option(
            "--kind",
            help=f"Limit to a step kind (repeatable): {', '.join(k.value for k in StepKind)}.",
        ),
    ] = None,
    skip_native: Annotated[
        bool, typer.Option("--skip-native", help="Leave android/ and ios/ untouched.")
    ] = False,
    no_install: Annotated[
        bool, typer.Option("--no-install", help="Do not run the package manager afterwards.")
    ] = False,
    check: Annotated[list[str] | None, typer.Option("--check", help=CHECK_HELP)] = None,
    no_check: Annotated[
        bool,
        typer.Option(
            "--no-check",
            help="Skip typecheck, tests and pods (this is the default).",
        ),
    ] = False,
    build: Annotated[
        bool, typer.Option("--build", help="Also run the Android and iOS builds (slow).")
    ] = False,
    no_ai: Annotated[
        bool, typer.Option("--no-ai", help="Do not ask a model to fix build errors.")
    ] = False,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use cached diffs and local rules only.")
    ] = False,
    allow_dirty: Annotated[
        bool, typer.Option("--allow-dirty", help="Migrate even with uncommitted changes.")
    ] = False,
    no_branch: Annotated[
        bool, typer.Option("--no-branch", help="Do not create a migration branch.")
    ] = False,
    rules_dir: Annotated[
        Path | None,
        typer.Option("--rules-dir", help="Directory of local migration rule files."),
    ] = None,
) -> None:
    """Migrate React Native to a newer version, step by step."""
    from ..commands.migrate import MigrateCommand

    context = build_context("migrate")
    execute(
        MigrateCommand(
            context,
            to_version=to,
            kinds=as_tuple(kind),
            skip_native=skip_native,
            install=not no_install,
            checks=resolve_checks(check, disabled=no_check, default=()),
            build=build,
            use_ai=not no_ai,
            offline=offline,
            allow_dirty=allow_dirty,
            branch=False if no_branch else None,
            rules_dir=rules_dir,
            verbose=context.verbose,
        )
    )


def compatibility(
    target: Annotated[
        str | None,
        typer.Option("--target", help="React Native version to check against."),
    ] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use installed metadata and the bundled table only.")
    ] = False,
    no_dependencies: Annotated[
        bool, typer.Option("--no-dependencies", help="Check runtime and platforms only.")
    ] = False,
) -> None:
    """Check this project against a React Native version before you migrate."""
    from ..commands.compatibility import CompatibilityCommand

    context = build_context("compatibility")
    execute(
        CompatibilityCommand(
            context,
            target=target,
            offline=offline,
            include_dependencies=not no_dependencies,
            verbose=context.verbose,
        )
    )


def docs(
    section: Annotated[
        list[str] | None, typer.Option("--section", help="Section to cover (repeatable).")
    ] = None,
    output: Annotated[
        str, typer.Option("--output", "-o", help="File to write.")
    ] = "docs/PROJECT.md",
    file: Annotated[
        list[str] | None,
        typer.Option("--file", "-f", help="Extra file to include as context (repeatable)."),
    ] = None,
) -> None:
    """Write project documentation from the scanned facts."""
    from ..commands.docs import DocsCommand

    context = build_context("docs")
    execute(
        DocsCommand(
            context,
            sections=as_tuple(section),
            output=output,
            files=as_tuple(file),
            verbose=context.verbose,
        )
    )


def release(
    bump: Annotated[
        str, typer.Option("--bump", help="major, minor or patch.")
    ] = "patch",
    version: Annotated[
        str | None, typer.Option("--version", help="Set an exact version instead of bumping.")
    ] = None,
    no_changelog: Annotated[
        bool, typer.Option("--no-changelog", help="Do not write release notes.")
    ] = False,
    changelog_path: Annotated[
        str, typer.Option("--changelog-path", help="Changelog file to prepend to.")
    ] = "CHANGELOG.md",
    force: Annotated[
        bool, typer.Option("--force", help="Proceed even when there are blockers.")
    ] = False,
) -> None:
    """Prepare a release: versions, changelog and the checklist."""
    from ..commands.release import ReleaseCommand

    context = build_context("release")
    execute(
        ReleaseCommand(
            context,
            bump=bump,
            version=version,
            changelog=not no_changelog,
            changelog_path=changelog_path,
            force=force,
            verbose=context.verbose,
        )
    )


def register(app: typer.Typer) -> None:
    """Attach the phase 4-6 commands to the root app."""
    for command in (upgrade, migrate, compatibility, docs, release):
        app.command()(command)
