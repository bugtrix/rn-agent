"""Terminal presentation primitives (§34).

One console, one set of styles, one confirmation prompt - so every command
looks and behaves the same. Nothing here knows about project internals.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from typing import Any

from rich.box import ROUNDED
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from ..constants import APP_TITLE, ENV_NO_COLOR
from .working import working as working

THEME = Theme(
    {
        "ok": "bold green",
        "fail": "bold red",
        "warn": "bold yellow",
        "info": "cyan",
        "muted": "dim",
        "heading": "bold",
        "critical": "bold red",
        "high": "red",
        "medium": "yellow",
        "low": "cyan",
        "value": "bold white",
    }
)

SEVERITY_STYLE = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "info": "info",
}

MARK_OK = "[ok]\u2713[/ok]"
MARK_FAIL = "[fail]\u2717[/fail]"
MARK_WARN = "[warn]![/warn]"
MARK_SKIP = "[muted]-[/muted]"
ARROW = "\u2192"

_console: Console | None = None


def console() -> Console:
    """The shared console (colour disabled by NO_COLOR or a non-tty)."""
    global _console
    if _console is None:
        _console = Console(
            theme=THEME,
            no_color=bool(os.environ.get(ENV_NO_COLOR)),
            soft_wrap=False,
            highlight=False,
        )
    return _console


def reset_console(new_console: Console | None = None) -> None:
    """Test hook: swap the console for a recording one."""
    global _console
    _console = new_console


def banner(subtitle: str | None = None) -> None:
    text = Text(APP_TITLE, style="bold")
    if subtitle:
        text.append(f"\n{subtitle}", style="dim")
    console().print(Panel(text, box=ROUNDED, expand=False))


def header(title: str, subtitle: str | None = None) -> None:
    body = Text(title, style="bold")
    if subtitle:
        body.append(f"  {subtitle}", style="dim")
    console().print(Panel(body, box=ROUNDED, expand=False))


def section(title: str) -> None:
    console().print()
    console().print(f"[heading]{title}[/heading]")


def field(label: str, value: Any, *, width: int = 18, style: str = "value") -> None:
    rendered = "[muted]-[/muted]" if value in (None, "", []) else f"[{style}]{value}[/{style}]"
    console().print(f"  {label.ljust(width)} {rendered}")


def key_values(pairs: Sequence[tuple[str, Any]], *, width: int = 18) -> None:
    for label, value in pairs:
        field(label, value, width=width)


def table(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    title: str | None = None,
    styles: Sequence[str | None] | None = None,
) -> None:
    grid = Table(title=title, box=ROUNDED, header_style="bold", title_justify="left", expand=False)
    for index, column in enumerate(columns):
        style = styles[index] if styles and index < len(styles) else None
        grid.add_column(column, style=style, overflow="fold")
    count = 0
    for row in rows:
        grid.add_row(*["" if cell is None else str(cell) for cell in row])
        count += 1
    if count:
        console().print(grid)


def bullet(text: str, *, style: str = "info", marker: str = "\u2192") -> None:
    console().print(f"  [{style}]{marker}[/{style}] {text}")


def note(text: str) -> None:
    console().print(f"  [muted]{text}[/muted]")


def indented(text: str, *, indent: int = 6, style: str | None = None) -> None:
    """Print wrapped text where continuation lines keep the indent.

    A plain ``print(f"      {text}")`` wraps back to column 0, which turns a
    two-line finding into something that reads like two findings.
    """
    body = Text(text, style=style or "")
    console().print(Padding(body, (0, 0, 0, indent)))



def code(line: str, *, indent: int = 8, style: str = "value") -> None:
    """Print a line meant to be copied, never broken by the renderer.

    Rich would wrap a long ``<uses-permission .../>`` mid-attribute, and a
    developer pasting that gets malformed XML. A wrapped URL is just as broken.
    Letting the terminal soft-wrap keeps the copy buffer correct.
    """
    console().print(
        Text(" " * indent + line, style=style),
        no_wrap=True,
        overflow="ignore",
        crop=False,
    )


def success(text: str) -> None:
    console().print(f"{MARK_OK} {text}")


def failure(text: str) -> None:
    console().print(f"{MARK_FAIL} [fail]{text}[/fail]")


def warning(text: str) -> None:
    console().print(f"{MARK_WARN} [warn]{text}[/warn]")


def blank() -> None:
    console().print()


def error_panel(message: str, hint: str | None = None) -> None:
    body = Text(message, style="red")
    if hint:
        body.append(f"\n\n{hint}", style="dim")
    console().print(Panel(body, title="error", box=ROUNDED, border_style="red", expand=False))


def confirm(question: str, default: bool = False) -> bool:
    """Yes/no prompt. Non-interactive terminals fall back to ``default``."""
    from rich.prompt import Confirm

    if not console().is_terminal:
        return default
    try:
        return bool(Confirm.ask(question, default=default, console=console()))
    except (EOFError, KeyboardInterrupt):
        return False


def ask_secret(question: str) -> str | None:
    """Prompt for a credential without echoing it.

    Returns ``None`` on a non-interactive terminal, so callers can insist on
    ``--stdin`` instead of hanging a CI job on an invisible prompt.
    """
    from rich.prompt import Prompt

    if not console().is_terminal:
        return None
    try:
        answer = Prompt.ask(question, password=True, console=console())
    except (EOFError, KeyboardInterrupt):
        return None
    return answer.strip() or None


def score_style(score: int) -> str:
    if score >= 90:
        return "ok"
    if score >= 75:
        return "info"
    if score >= 50:
        return "warn"
    return "fail"
