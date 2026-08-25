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
from ..validation.runner import STEP_NAMES
from .runtime import as_tuple, build_context, execute, resolve_checks

CHECK_HELP = f"Validation step to run afterwards (repeatable): {', '.join(STEP_NAMES)}."


def upgrade(
    target: Annotated[
        str,
        typer.Option("--target", help=f"How far to move: {', '.join(POLICIES)}."),
    ] = "minor",
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
        typer.Option("--offline", help="Do not contact the registry; report drift only."),
    ] = False,
) -> None:
    """Risk-ranked dependency upgrades, with peer and native analysis."""
    from ..commands.upgrade import UpgradeCommand

    context = build_context("upgrade")
    execute(
        UpgradeCommand(
            context,
            policy=target,
            only=as_tuple(only),
            skip=as_tuple(skip),
            include_native=native,
            install=not no_install,
            checks=resolve_checks(check, disabled=no_check, default=("typecheck", "tests")),
            offline=offline,
            verbose=context.verbose,
        )
    )


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
