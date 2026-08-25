"""Wizards: ask, show, confirm, then run the real engine.

``/upgrade`` and ``/migrate`` with no arguments are a conversation - what version
are you on, where are you going, shall I look first - because a React Native
version change touches native projects and nobody should trigger one by pressing
Enter on a half-typed line. What a wizard does *not* do is the work: it collects
the answers and hands them to the same CLI command, so the deterministic engine,
the branch, the diff application, the validation and the rollback are all the
tested implementation.

``/upgrade`` asks which React Native version to move to (from published
releases). JavaScript dependency bumps are still there, as an explicit choice
on the same list, so the command developers actually type does the thing they
mean.

The AI never enters the loop here. The engine asks for permission before spending
tokens on a build failure (through the safety confirmer, which the terminal wires
to the ``[Analyze] [Skip]`` dialog), which is the only place a model is involved
in a migration at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..cli import ui
from ..errors import RNAgentError
from ..upgrade.versions import RnTarget, classify_upgrade, concrete_rn_version
from ..utils.semver import compare, parse
from .dialogs import Action, choose, confirm
from .router import RouteResult, parse_flags, run_cli
from .select import Choice, select
from .session import SessionManager
from .versions import pick_rn_version, pick_upgrade

Picker = Callable[..., Choice | None]
Asker = Callable[[str], str | None]

MIGRATE_FLAGS = (
    "skip-native",
    "build",
    "no-ai",
    "offline",
    "allow-dirty",
    "no-branch",
    "no-install",
)
UPGRADE_FLAGS = (
    "deps",
    "native",
    "no-install",
    "no-check",
    "offline",
    "skip-native",
    "build",
    "no-ai",
    "allow-dirty",
    "no-branch",
)


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
    targets: Sequence[RnTarget] | None = None,
) -> RouteResult:
    """``/migrate`` - pick a version, then the engine."""
    positional, options = parse_flags(args, flags=MIGRATE_FLAGS)
    current = _ensure_current(session, picker)
    if isinstance(current, RouteResult):
        return current
    target = _target_from(options, positional) or pick_rn_version(
        current,
        targets=targets,
        offline=bool(options.get("offline")),
        picker=picker,
        asker=asker,
    )
    if not target:
        return RouteResult(message="cancelled")
    return _confirm_and_migrate(session, current, target, options, picker)


def upgrade(
    session: SessionManager,
    args: list[str],
    *,
    picker: Picker = select,
    asker: Asker = ask_version,
    targets: Sequence[RnTarget] | None = None,
) -> RouteResult:
    """``/upgrade`` - ask which React Native version, or bump JS dependencies."""
    positional, options = parse_flags(args, flags=UPGRADE_FLAGS)
    try:
        request = classify_upgrade(
            to=_option_str(options.get("to")) or (positional[0] if positional else None),
            target=_option_str(options.get("target")),
            deps=bool(options.get("deps")),
            only=_as_one(options.get("only")),
            skip=_as_one(options.get("skip")),
            native=bool(options.get("native")),
        )
    except RNAgentError as error:
        detail = error.message + (f" - {error.hint}" if error.hint else "")
        return RouteResult(exit_code=error.exit_code, warning=detail)

    if request.kind == "deps":
        return RouteResult(exit_code=run_cli(session, ["upgrade", "--deps", *args]))

    current = _ensure_current(session, picker)
    if isinstance(current, RouteResult):
        return current

    if request.kind == "rn" and request.version:
        target = request.version
    else:
        picked = pick_upgrade(
            current,
            targets=targets,
            offline=bool(options.get("offline")),
            picker=picker,
            asker=asker,
        )
        if picked is None:
            return RouteResult(message="cancelled")
        if picked.kind == "deps":
            argv = ["upgrade", "--deps", "--target", picked.value, *_deps_passthrough(options)]
            return RouteResult(exit_code=run_cli(session, argv))
        target = picked.value

    return _confirm_and_migrate(session, current, target, options, picker)


def _ensure_current(session: SessionManager, picker: Picker) -> str | RouteResult:
    """The installed React Native version, scanning first when needed."""
    snapshot = session.snapshot()
    current = snapshot.rn_version
    if snapshot.scanned and current:
        return current
    ui.warning("this project has not been scanned yet")
    if not confirm("Scan it now?", default=True, picker=picker):
        return RouteResult(message="cancelled")
    code = run_cli(session, ["scan"])
    if code != 0:
        return RouteResult(exit_code=code)
    current = session.snapshot().rn_version
    if not current:
        return RouteResult(
            exit_code=3,
            warning="the React Native version could not be established; install dependencies first",
        )
    return current


def _confirm_and_migrate(
    session: SessionManager,
    current: str,
    target: str,
    options: dict[str, str | bool],
    picker: Picker,
) -> RouteResult:
    try:
        target = concrete_rn_version(target, offline=bool(options.get("offline")))
    except RNAgentError as error:
        detail = error.message + (f" - {error.hint}" if error.hint else "")
        return RouteResult(exit_code=error.exit_code, warning=detail)
    verdict = _check_direction(current, target)
    if verdict is not None:
        return verdict

    snapshot = session.snapshot()
    ui.blank()
    ui.header("React Native Upgrade", f"{current}  →  {target}")
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
            Action("analyze", "Analyze", "plan the upgrade, write nothing"),
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
        # A plan, not a write: the engine's own dry-run mode is the preview.
        code = _dry_run(session, argv)
        if code != 0:
            return RouteResult(exit_code=code)
        if not confirm(f"Apply this upgrade to {target}?", default=False, picker=picker):
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
    """Forward the flags the migrate engine understands, untouched."""
    forwarded: list[str] = []
    for flag in MIGRATE_FLAGS:
        if options.get(flag):
            forwarded.append(f"--{flag}")
    for name in ("kind", "rules-dir"):
        value = options.get(name)
        if isinstance(value, str) and value:
            forwarded.extend([f"--{name}", value])
    return forwarded


def _deps_passthrough(options: dict[str, str | bool]) -> list[str]:
    """Flags the dependency-upgrade engine understands."""
    forwarded: list[str] = []
    for flag in ("native", "no-install", "no-check", "offline"):
        if options.get(flag):
            forwarded.append(f"--{flag}")
    return forwarded


def _option_str(value: str | bool | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_one(value: str | bool | None) -> tuple[str, ...]:
    text = _option_str(value)
    return (text,) if text else ()


def _git_word(snapshot: object) -> str:
    dirty = getattr(snapshot, "git_dirty", None)
    branch = getattr(snapshot, "git_branch", None)
    if dirty is None:
        return "not a repository"
    return f"{branch or 'detached'} ({'dirty' if dirty else 'clean'})"
