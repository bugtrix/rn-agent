"""Daily development commands: ``review``, ``fix``, ``feature``, ``test``.

Thin on purpose. Each function parses flags, builds the shared context and runs
one command - the four phases, the safety gate, the report file and the exit
code all come from the command itself. Command modules are imported inside the
functions so the AI stack is not loaded by ``rn-agent scan --help``.
"""

from __future__ import annotations

from typing import Annotated

import typer

from ..models.review import REVIEW_AREAS
from ..validation.runner import STEP_NAMES
from .runtime import as_tuple, build_context, execute, resolve_checks

FILE_HELP = (
    "Limit to this file or directory (repeatable). A native path is confirmation "
    "to edit that file without --allow-native."
)
CHECK_HELP = (
    "Validation step to run afterwards (repeatable, off unless you pass this): "
    f"{', '.join(STEP_NAMES)}."
)


def review(
    file: Annotated[list[str] | None, typer.Option("--file", "-f", help=FILE_HELP)] = None,
    changed: Annotated[
        bool, typer.Option("--changed", help="Review what git reports as changed.")
    ] = False,
    area: Annotated[
        list[str] | None,
        typer.Option("--area", help=f"Limit to an area (repeatable): {', '.join(REVIEW_AREAS)}."),
    ] = None,
    about: Annotated[
        str | None, typer.Option("--about", help="What to focus on, in your words.")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Maximum number of files to send.")
    ] = None,
    fail_under: Annotated[
        int | None,
        typer.Option("--fail-under", help="Exit non-zero when the score is below this value."),
    ] = None,
) -> None:
    """Analyse components, hooks, state and performance with your model."""
    from ..commands.review import ReviewCommand

    context = build_context("review")
    execute(
        ReviewCommand(
            context,
            files=as_tuple(file),
            changed=changed,
            areas=as_tuple(area),
            instruction=about,
            limit=limit,
            fail_under=fail_under,
            verbose=context.verbose,
        )
    )


def fix(
    issue: Annotated[
        list[str] | None,
        typer.Option("--issue", help="Finding id from health/review (repeatable)."),
    ] = None,
    file: Annotated[list[str] | None, typer.Option("--file", "-f", help=FILE_HELP)] = None,
    about: Annotated[
        str | None, typer.Option("--about", help="Describe the problem in your words.")
    ] = None,
    changed: Annotated[
        bool, typer.Option("--changed", help="Fix what git reports as changed.")
    ] = False,
    check: Annotated[list[str] | None, typer.Option("--check", help=CHECK_HELP)] = None,
    no_check: Annotated[
        bool, typer.Option("--no-check", help="Skip validation after applying (default).")
    ] = False,
    allow_native: Annotated[
        bool,
        typer.Option(
            "--allow-native",
            help=(
                "Permit any android/ios edit. Prefer --file on the native path, "
                "or list it in rules.allow_native_paths."
            ),
        ),
    ] = False,
    allow_deps: Annotated[
        bool, typer.Option("--allow-deps", help="Permit package.json dependency edits.")
    ] = False,
    keep: Annotated[
        bool,
        typer.Option("--keep", help="Keep the changes even when validation fails."),
    ] = False,
) -> None:
    """Fix reported problems. Pass --check to typecheck or run tests afterwards."""
    from ..commands.fix import FixCommand

    context = build_context("fix")
    execute(
        FixCommand(
            context,
            issues=as_tuple(issue),
            files=as_tuple(file),
            instruction=about,
            changed=changed,
            checks=resolve_checks(check, disabled=no_check, default=()),
            allow_native=allow_native,
            allow_dependencies=allow_deps,
            keep_on_failure=keep,
            verbose=context.verbose,
        )
    )


def feature(
    description: Annotated[str, typer.Argument(help="What the feature should do.")],
    file: Annotated[list[str] | None, typer.Option("--file", "-f", help=FILE_HELP)] = None,
    allow_deps: Annotated[
        bool, typer.Option("--allow-deps", help="Permit package.json dependency edits.")
    ] = False,
    check: Annotated[list[str] | None, typer.Option("--check", help=CHECK_HELP)] = None,
    no_check: Annotated[
        bool, typer.Option("--no-check", help="Skip validation after applying (default).")
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Keep the changes even when validation fails.")
    ] = False,
) -> None:
    """Implement a feature following the project's existing architecture."""
    from ..commands.feature import FeatureCommand

    context = build_context("feature")
    execute(
        FeatureCommand(
            context,
            description=description,
            files=as_tuple(file),
            allow_dependencies=allow_deps,
            checks=resolve_checks(check, disabled=no_check, default=()),
            keep_on_failure=keep,
            verbose=context.verbose,
        )
    )


def test(
    target: Annotated[
        list[str] | None, typer.Argument(help="Files or directories to write tests for.")
    ] = None,
    framework: Annotated[
        str | None, typer.Option("--framework", help="Override the detected test framework.")
    ] = None,
    no_run: Annotated[
        bool, typer.Option("--no-run", help="Write the tests without running them.")
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Keep generated tests even when they fail.")
    ] = False,
) -> None:
    """Generate tests for your code and run them."""
    from ..commands.test import TestCommand

    context = build_context("test")
    execute(
        TestCommand(
            context,
            targets=as_tuple(target),
            framework=framework,
            run_tests=not no_run,
            keep_on_failure=keep,
            verbose=context.verbose,
        )
    )


def delegate(
    task: Annotated[
        str | None,
        typer.Argument(metavar="TASK", help="What the Cursor agent should do, in your words."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Cursor model to use (default: Cursor's own).")
    ] = None,
    check: Annotated[list[str] | None, typer.Option("--check", help=CHECK_HELP)] = None,
    no_check: Annotated[
        bool, typer.Option("--no-check", help="Skip validation after the agent runs (default).")
    ] = False,
    allow_native: Annotated[
        bool,
        typer.Option(
            "--allow-native",
            help="Permit any android/ios edit, or list paths in rules.allow_native_paths.",
        ),
    ] = False,
    allow_deps: Annotated[
        bool, typer.Option("--allow-deps", help="Permit package.json dependency edits.")
    ] = False,
    allow_dirty: Annotated[
        bool,
        typer.Option("--allow-dirty", help="Run with uncommitted changes (no exact undo)."),
    ] = False,
    no_branch: Annotated[
        bool, typer.Option("--no-branch", help="Work on the current branch instead of a new one.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to let the agent run.")
    ] = 900.0,
) -> None:
    """Hand a task to the Cursor agent, then audit what it changed."""
    from ..commands.delegate import DelegateCommand

    context = build_context("delegate")
    execute(
        DelegateCommand(
            context,
            task=task,
            model=model,
            checks=resolve_checks(check, disabled=no_check, default=()),
            allow_native=allow_native,
            allow_dependencies=allow_deps,
            allow_dirty=allow_dirty,
            branch=not no_branch,
            timeout=timeout,
            verbose=context.verbose,
        )
    )


def register(app: typer.Typer) -> None:
    """Attach the AI development commands to the root app."""
    for command in (review, fix, feature, test, delegate):
        app.command()(command)
