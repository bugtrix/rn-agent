"""Confirmation dialogs, built from the same picker as everything else.

The brief asks for ``[Analyze] [Skip]`` when a migration build fails, and for
confirmation before anything is applied. Both are the same thing: a small set of
named actions, chosen with the arrow keys. Reusing the picker keeps the keys
consistent - and makes every dialog degrade the same way when there is no tty,
by returning the caller's stated default rather than blocking.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..cli import ui
from .select import Choice, select

Picker = Callable[..., Choice | None]


@dataclass(frozen=True, slots=True)
class Action:
    """One button."""

    value: str
    label: str
    hint: str = ""


def choose(
    title: str,
    actions: Sequence[Action],
    *,
    subtitle: str = "",
    lines: Sequence[str] = (),
    default: str | None = None,
    picker: Picker = select,
) -> str | None:
    """Show ``actions`` and return the chosen value.

    ``default`` is what a non-interactive session gets, which is why every caller
    has to decide what "no human here" means for its dialog - the safe answer is
    never assumed on their behalf.
    """
    if subtitle or lines:
        ui.blank()
        ui.header(title, subtitle)
        for line in lines:
            ui.console().print(f"  {line}")
    choices = [
        Choice(value=action.value, label=action.label, hint=action.hint) for action in actions
    ]
    chosen = picker(
        subtitle or title,
        choices,
        search=False,
        footer="↑↓ Navigate   Enter Choose   Esc Cancel",
    )
    if chosen is None:
        return default
    return chosen.value


def confirm(
    question: str,
    *,
    default: bool = False,
    yes_label: str = "Yes",
    no_label: str = "No",
    picker: Picker = select,
) -> bool:
    """A yes/no gate. A missing picker or a non-interactive terminal uses ``default``."""
    answer = choose(
        question,
        (Action("yes", yes_label), Action("no", no_label)),
        default="yes" if default else "no",
        picker=picker,
    )
    if answer is None:
        return ui.confirm(question, default=default)
    return answer == "yes"


def analyse_or_skip(
    *,
    title: str,
    detail: str,
    provider: str | None,
    model: str | None,
    picker: Picker = select,
) -> str:
    """The migration failure dialog: analyse with AI, retry, or skip.

    The provider and model are named in the dialog because accepting it spends
    the developer's own account, and that should never be a surprise.
    """
    lines = [detail]
    if provider and model:
        lines.append("")
        lines.append(f"[muted]AI analysis available · {provider} {model}[/muted]")
    else:
        lines.append("")
        lines.append("[warn]no AI connected - /login to enable analysis[/warn]")
    actions = [
        Action("analyze", "Analyze", f"ask {model}" if model else "needs /login"),
        Action("describe", "Describe a fix", "type what the AI should change"),
        Action("skip", "Skip", "leave it to me"),
    ]
    return choose(
        "Build failed",
        actions,
        subtitle=title,
        lines=lines,
        default="skip",
        picker=picker,
    ) or "skip"
