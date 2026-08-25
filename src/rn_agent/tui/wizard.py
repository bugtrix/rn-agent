"""The migration wizard: ask, show, confirm, then run the real engine.

``/migrate`` with no arguments is a conversation - what version are you on, where
are you going, shall I look first - because a React Native migration touches
native projects and nobody should trigger one by pressing Enter on a half-typed
line. What the wizard does *not* do is migrate: it collects the answers and hands
them to the same ``rn-agent migrate`` that the command line runs, so the
deterministic engine, the branch, the diff application, the validation and the
rollback are all the tested implementation.

The AI never enters the loop here. The engine asks for permission before spending
tokens on a build failure (through the safety confirmer, which the terminal wires
to the ``[Analyze] [Skip]`` dialog), which is the only place a model is involved
in a migration at all.
"""

from __future__ import annotations

from collections.abc import Callable

from ..cli import ui
from ..utils.semver import compare, parse
from .dialogs import Action, choose, confirm
from .router import RouteResult, parse_flags, run_cli
from .select import Choice, select
from .session import SessionManager

Picker = Callable[..., Choice | None]
Asker = Callable[[str], str | None]


def ask_version(prompt: str) -> str | None:
    """Read a version from the developer. ``None`` when there is no terminal."""
    from prompt_toolkit import prompt as read_line
    from prompt_toolkit.formatted_text import FormattedText

    from .theme import interactive_terminal

    if not interactive_terminal():
        return None
    try:
        answer = read_line(FormattedText([("class:prompt.arrow", f"{prompt} ")]))
    except (EOFError, KeyboardInterrupt):
        return None
    return answer.strip() or None


def migrate(
    session: SessionManager,
    args: list[str],
    *,
    picker: Picker = select,
    asker: Asker = ask_version,
) -> RouteResult:
    """``/migrate`` - the wizard, then the engine."""
    positional, options = parse_flags(
        args, flags=("skip-native", "build", "no-ai", "offline", "allow-dirty", "no-branch", "no-install")
    )
    snapshot = session.snapshot()
    current = snapshot.rn_version

    if not snapshot.scanned or not current:
        ui.warning("this project has not been scanned yet")
        if not confirm("Scan it now?", default=True):
            return RouteResult(message="cancelled")
        code = run_cli(session, ["scan"])
        if code != 0:
            return RouteResult(exit_code=code)
        snapshot = session.snapshot()
        current = snapshot.rn_version
        if not current:
            return RouteResult(
                exit_code=3,
                warning="the React Native version could not be established; install dependencies first",
            )

    target = _target_from(options, positional) or _ask_target(current, asker)
    if not target:
        return RouteResult(message="cancelled")

    verdict = _check_direction(current, target)
    if verdict is not None:
        return verdict

    ui.blank()
    ui.header("React Native Migration", f"{current}  →  {target}")
    ui.key_values(
        [
            ("project", snapshot.project_name),
            ("git", _git_word(snapshot)),
            ("native steps", "skipped" if options.get("skip-native") else "included"),
            ("builds", "yes" if options.get("build") else "no (--build to run them)"),
            ("ai repair", "off" if options.get("no-ai") else "on failure, with your consent"),
        ]
    )
    if snapshot.git_dirty:
        ui.warning("the git tree is dirty - migrate refuses to start unless you pass --allow-dirty")

    action = choose(
        "Analyze project?",
        (
            Action("analyze", "Analyze", "plan the migration, write nothing"),
            Action("run", "Analyze and apply", "branch, apply, validate, roll back on failure"),
            Action("cancel", "Cancel"),
        ),
        subtitle=f"{current} → {target}",
        default="analyze",
        picker=picker,
    )
    if action == "cancel" or action is None:
        return RouteResult(message="cancelled")
    argv = ["migrate", "--to", target, *_passthrough(options)]
    if action == "analyze":
        # A plan, not a migration: the engine's own dry-run mode is the preview.
        code = _dry_run(session, argv)
        if code != 0:
            return RouteResult(exit_code=code)
        if not confirm(f"Apply this migration to {target}?", default=False):
            return RouteResult(message="nothing was applied")
    return RouteResult(exit_code=run_cli(session, argv))


def _dry_run(session: SessionManager, argv: list[str]) -> int:
    """Run the engine in preview mode, whatever the session's own flags are."""
    was = session.dry_run
    session.dry_run = True
    try:
        return run_cli(session, argv)
    finally:
        session.dry_run = was


def _target_from(options: dict[str, str | bool], positional: list[str]) -> str | None:
    explicit = options.get("to")
    if isinstance(explicit, str) and explicit:
        return explicit
    return positional[0] if positional else None


def _ask_target(current: str, asker: Asker) -> str | None:
    ui.blank()
    ui.key_values([("current React Native version", current)])
    answer = asker("Target React Native version:")
    if answer is None:
        ui.warning("no target version given - `/migrate --to 0.86.0`")
    return answer


def _check_direction(current: str, target: str) -> RouteResult | None:
    """Refuse a target that is not a newer version, before anything runs."""
    if parse(target) is None:
        return RouteResult(exit_code=1, warning=f"{target} is not a React Native version")
    order = compare(current, target)
    if order is None:
        return RouteResult(
            exit_code=1, warning=f"cannot compare {current} with {target}"
        )
    if order >= 0:
        return RouteResult(
            exit_code=1,
            warning=f"this project is already on {current}; {target} is not newer",
        )
    return None


def _passthrough(options: dict[str, str | bool]) -> list[str]:
    """Forward the flags the engine understands, untouched."""
    forwarded: list[str] = []
    for flag in ("skip-native", "build", "no-ai", "offline", "allow-dirty", "no-branch", "no-install"):
        if options.get(flag):
            forwarded.append(f"--{flag}")
    for name in ("kind", "rules-dir"):
        value = options.get(name)
        if isinstance(value, str) and value:
            forwarded.extend([f"--{name}", value])
    return forwarded


def _git_word(snapshot: object) -> str:
    dirty = getattr(snapshot, "git_dirty", None)
    branch = getattr(snapshot, "git_branch", None)
    if dirty is None:
        return "not a repository"
    return f"{branch or 'detached'} ({'dirty' if dirty else 'clean'})"
