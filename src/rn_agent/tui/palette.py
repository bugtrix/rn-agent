"""Ctrl+K: every command, one fuzzy search away.

The palette exists because a keyboard-first tool should never require the
developer to remember a name. It is deliberately the same widget as the provider
and model pickers - one set of keys to learn, one place where navigation is
defined - and it searches the command *summary* as well as the name, so "gradle"
finds ``/migrate`` and "sign in" finds ``/login``.
"""

from __future__ import annotations

from collections.abc import Callable

from .router import CommandRouter, SlashCommand, command_choices
from .select import Choice, select

Picker = Callable[..., Choice | None]

PALETTE_FOOTER = "↑↓ Navigate   Enter Run   Esc Cancel   type to search"


def open_palette(router: CommandRouter, *, picker: Picker = select) -> SlashCommand | None:
    """Show the palette and return the chosen command, or ``None``."""
    choices = command_choices(router)
    if not choices:
        return None
    chosen = picker("Search commands…", choices, footer=PALETTE_FOOTER)
    if chosen is None:
        return None
    payload = chosen.payload
    return payload if isinstance(payload, SlashCommand) else router.get(chosen.value)
