"""The keyboard-driven picker every selection in the agent uses.

One widget serves ``/login`` (providers), ``/provider``, ``/model`` and the
command palette, so navigation is identical everywhere: arrows move, Enter
selects, Esc cancels, typing filters. Writing it once is also what makes the
grouped model list possible - "this provider" above "other providers", with the
active model marked - without four bespoke implementations drifting apart.

Two behaviours are load-bearing rather than decorative:

* **A non-interactive terminal never blocks.** ``select`` returns ``None``
  immediately when stdin is not a tty, so a piped session, CI, or
  ``rn-agent … --json`` falls back to flags instead of hanging on a keypress
  that cannot arrive.
* **Disabled rows are shown, not hidden.** A model whose provider is not
  connected stays visible with its reason, because "why is Opus missing?" is a
  worse experience than "Opus - openai not connected".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame

from .theme import interactive_terminal, selector_style

DEFAULT_FOOTER = "↑↓ Navigate   Enter Select   Esc Cancel"
SEARCH_FOOTER = "↑↓ Navigate   Enter Select   Esc Cancel   type to search"
MARKER = "❯ "
INDENT = "  "


@dataclass(frozen=True, slots=True)
class Choice:
    """One selectable row."""

    value: str
    label: str
    #: Right-hand detail: a model family, an auth method, a command summary.
    hint: str = ""
    #: Group heading this row belongs under ("Anthropic", "Other Providers").
    group: str = ""
    #: Marked as the active provider/model.
    current: bool = False
    #: Shown but not selectable, with ``note`` explaining why.
    disabled: bool = False
    note: str = ""
    #: Anything the caller wants back with the selection.
    payload: Any = None

    @property
    def search_fields(self) -> tuple[str, ...]:
        """Fields matched independently.

        Matching the *concatenation* would be far too loose: the subsequence
        ``clop`` would find "claude-sonnet-4-5 … anthropic" by borrowing the `p`
        from the provider name. Per-field keeps initials useful and results
        honest.
        """
        return tuple(part.casefold() for part in (self.value, self.label, self.hint) if part)


@dataclass(slots=True)
class _Row:
    """A rendered line: either a group heading or a choice."""

    choice: Choice | None
    heading: str = ""

    @property
    def selectable(self) -> bool:
        return self.choice is not None and not self.choice.disabled


@dataclass(slots=True)
class Selector:
    """State machine behind the picker, testable without a terminal.

    The Application in :meth:`run` is a thin shell over this: every key maps to
    a method here, so the navigation rules (skip headings, skip disabled rows,
    wrap at the ends, filter as you type) can be tested with no tty at all.
    """

    title: str
    choices: Sequence[Choice]
    footer: str = ""
    search: bool = True
    query: str = ""
    index: int = 0
    #: Optional live reload, bound to Ctrl+R (refresh the model catalogue).
    on_refresh: Callable[[], Sequence[Choice]] | None = None
    _rows: list[_Row] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.rebuild()
        self.index = self._first_selectable(prefer_current=True)

    # -- rows --------------------------------------------------------------
    def rebuild(self) -> None:
        """Recompute visible rows from the query, keeping group order."""
        needle = self.query.strip().casefold()
        rows: list[_Row] = []
        seen_group: str | None = None
        for choice in self.choices:
            if needle and not matches_choice(needle, choice):
                continue
            if choice.group and choice.group != seen_group:
                rows.append(_Row(choice=None, heading=choice.group))
                seen_group = choice.group
            rows.append(_Row(choice=choice))
        self._rows = rows

    @property
    def rows(self) -> list[_Row]:
        return self._rows

    @property
    def visible(self) -> list[Choice]:
        return [row.choice for row in self._rows if row.choice is not None]

    @property
    def current(self) -> Choice | None:
        if 0 <= self.index < len(self._rows):
            return self._rows[self.index].choice
        return None

    # -- navigation --------------------------------------------------------
    def move(self, delta: int) -> None:
        """Step to the next selectable row, wrapping at both ends."""
        count = len(self._rows)
        if count == 0:
            return
        position = self.index
        for _ in range(count):
            position = (position + delta) % count
            if self._rows[position].selectable:
                self.index = position
                return

    def type(self, character: str) -> None:
        self.query += character
        self._after_query_change()

    def backspace(self) -> None:
        if self.query:
            self.query = self.query[:-1]
            self._after_query_change()

    def clear_query(self) -> None:
        if self.query:
            self.query = ""
            self._after_query_change()

    def refresh(self) -> None:
        if self.on_refresh is None:
            return
        self.choices = list(self.on_refresh())
        self._after_query_change()

    def _after_query_change(self) -> None:
        self.rebuild()
        self.index = self._first_selectable()

    def _first_selectable(self, *, prefer_current: bool = False) -> int:
        if prefer_current:
            for position, row in enumerate(self._rows):
                if row.selectable and row.choice is not None and row.choice.current:
                    return position
        for position, row in enumerate(self._rows):
            if row.selectable:
                return position
        return 0

    # -- rendering ---------------------------------------------------------
    def fragments(self) -> StyleAndTextTuples:
        """The picker body, as prompt_toolkit fragments."""
        if not self._rows:
            return [("class:selector.hint", f"{INDENT}no match for {self.query!r}\n")]
        out: StyleAndTextTuples = []
        for position, row in enumerate(self._rows):
            if row.choice is None:
                out.append(("class:selector.group", f"\n{INDENT}{row.heading}\n"))
                continue
            out.extend(self._choice_fragments(row.choice, active=position == self.index))
        return out

    def _choice_fragments(self, choice: Choice, *, active: bool) -> StyleAndTextTuples:
        marker = MARKER if active else INDENT
        style = (
            "class:selector.disabled"
            if choice.disabled
            else "class:selector.current"
            if active
            else ""
        )
        line: StyleAndTextTuples = [
            ("class:selector.marker" if active else "", marker),
            (style, choice.label),
        ]
        if choice.current:
            line.append(("class:selector.marker", " ·current"))
        if choice.hint:
            line.append(("class:selector.hint", f"   {choice.hint}"))
        if choice.disabled and choice.note:
            line.append(("class:selector.note", f"   {choice.note}"))
        line.append(("", "\n"))
        return line

    def footer_fragments(self) -> StyleAndTextTuples:
        hint = self.footer or (SEARCH_FOOTER if self.search else DEFAULT_FOOTER)
        out: StyleAndTextTuples = []
        if self.query:
            out.append(("class:selector.search", f"{INDENT}search: {self.query}\n"))
        out.append(("class:selector.footer", f"{INDENT}{hint}"))
        return out


def _matches(needle: str, haystack: str) -> bool:
    """Subsequence match within one field: ``clop`` finds ``claude-opus``.

    Deliberately fuzzy rather than substring - the palette and the model list
    are both faster to drive when initials work.
    """
    position = 0
    for character in needle:
        if character == " ":
            continue
        position = haystack.find(character, position)
        if position < 0:
            return False
        position += 1
    return True


def matches_choice(needle: str, choice: Choice) -> bool:
    """Whether any single field of ``choice`` matches ``needle``."""
    return any(_matches(needle, field) for field in choice.search_fields)


def fuzzy_rank(needle: str, choices: Sequence[Choice]) -> list[Choice]:
    """Choices that match ``needle``, best first. Used by the palette.

    Scoring is coarse on purpose: a field that *starts* with the query beats a
    field that merely contains it, which beats a scattered subsequence. Ties
    keep the caller's order, so a deliberate grouping survives a search.
    """
    folded = needle.strip().casefold()
    if not folded:
        return list(choices)
    scored: list[tuple[int, int, Choice]] = []
    for order, choice in enumerate(choices):
        if not matches_choice(folded, choice):
            continue
        fields = choice.search_fields
        if any(field.startswith(folded) for field in fields):
            score = 0
        elif any(folded in field for field in fields):
            score = 1
        else:
            score = 2
        scored.append((score, order, choice))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [choice for _, _, choice in scored]


def select(
    title: str,
    choices: Sequence[Choice],
    *,
    search: bool = True,
    footer: str = "",
    on_refresh: Callable[[], Sequence[Choice]] | None = None,
    colors: bool = True,
) -> Choice | None:
    """Show the picker and return the chosen row, or ``None`` if cancelled.

    Returns ``None`` without drawing anything when the terminal is not
    interactive, so every caller must have a non-interactive path.
    """
    if not choices or not interactive_terminal():
        return None

    state = Selector(title=title, choices=list(choices), footer=footer, search=search, on_refresh=on_refresh)
    bindings = KeyBindings()

    @bindings.add("up")
    @bindings.add("c-p")
    def _up(event: Any) -> None:
        state.move(-1)

    @bindings.add("down")
    @bindings.add("c-n")
    def _down(event: Any) -> None:
        state.move(1)

    @bindings.add("enter")
    def _accept(event: Any) -> None:
        event.app.exit(result=state.current)

    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    @bindings.add("c-q")
    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    @bindings.add("c-r")
    def _reload(event: Any) -> None:
        state.refresh()

    if search:

        @bindings.add("backspace")
        def _erase(event: Any) -> None:
            state.backspace()

        @bindings.add("<any>")
        def _typed(event: Any) -> None:
            text = event.data
            if text and text.isprintable():
                state.type(text)

    body = Window(
        FormattedTextControl(state.fragments, focusable=True),
        dont_extend_height=True,
        height=Dimension(min=1, max=max(3, min(len(choices) + 4, 18))),
    )
    footer_window = Window(
        FormattedTextControl(state.footer_fragments), height=Dimension(min=1, max=2)
    )
    layout = Layout(HSplit([Frame(body, title=title), footer_window]))
    application: Application[Choice | None] = Application(
        layout=layout,
        key_bindings=bindings,
        style=selector_style(colors=colors),
        full_screen=False,
        mouse_support=False,
    )
    return application.run()
