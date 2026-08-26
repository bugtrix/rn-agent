"""One palette for the terminal, in both rendering systems.

The agent draws with two libraries on purpose: Rich for reports (tables,
panels, the health and migration output that also has to work when piped to a
file) and prompt_toolkit for anything the developer *drives* with the keyboard
(the prompt, the pickers, the palette). Only prompt_toolkit can own the screen
and read keys; only Rich already renders every existing report.

Keeping the two colour vocabularies here means a picker and a report never
disagree about what "warning" looks like, and `NO_COLOR` turns both off.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache

from prompt_toolkit.styles import Style

from ..constants import ENV_NO_COLOR

#: prompt_toolkit style names, chosen to read like the Rich theme in cli/ui.py.
STYLE_RULES: dict[str, str] = {
    "frame.border": "#5f5f87",
    "frame.label": "bold",
    "dialog.title": "bold",
    "selector.group": "bold #87afff",
    "selector.current": "reverse bold",
    "selector.marker": "bold #00d75f",
    "selector.hint": "#6c6c6c",
    "selector.note": "#ffaf5f",
    "selector.disabled": "#585858",
    "selector.match": "bold #00d7ff",
    "selector.footer": "#6c6c6c",
    "selector.search": "bold",
    "status.ok": "#00d75f",
    "status.warn": "#ffaf5f",
    "status.error": "bold #ff5f5f",
    "status.muted": "#6c6c6c",
    "prompt.arrow": "bold #00d75f",
    "prompt.slash": "#87afff",
    "prompt.placeholder": "#585858",
    "bottom-toolbar": "bg:#1c1c1c #6c6c6c",
}


def colors_enabled(*, configured: bool = True) -> bool:
    """Colour is on unless the environment or the config says otherwise.

    ``NO_COLOR`` is honoured because a developer who set it meant it, and a
    non-tty (a pipe, a CI log) gets plain text for the same reason.
    """
    if not configured:
        return False
    if os.environ.get(ENV_NO_COLOR):
        return False
    return sys.stdout.isatty()


@lru_cache(maxsize=2)
def selector_style(*, colors: bool = True) -> Style:
    """The prompt_toolkit style for pickers and the palette."""
    if not colors:
        return Style.from_dict({})
    return Style.from_dict(STYLE_RULES)


def interactive_terminal() -> bool:
    """Whether a full-screen picker can be drawn at all.

    Everything interactive checks this first: a piped or redirected session must
    fall back to flags and printed output rather than block on a keypress that
    can never arrive.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()
