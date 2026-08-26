"""The "Working..." wait state while a model is thinking.

Cursor's own CLI uses a yellow spinner, a two-character highlight that walks
the word, and ``[esc]`` to cancel. This is that same wait, so a long review or
fix does not look hung. It is a presentation concern: nothing here talks to a
provider.

Animation is skipped when stdout is not a tty or ``--json`` is on, so tests,
pipes and CI logs stay byte-stable.
"""

from __future__ import annotations

import contextlib
import os
import select
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.text import Text

WORKING_WORD = "Working..."
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_MS = 80
SHIMMER_MS = 100
SHIMMER_WIDTH = 2
MAX_LABEL = 48

_depth = 0
_depth_lock = threading.Lock()
_label = WORKING_WORD


def current_label() -> str:
    """The word currently walking the highlight, for nested waits."""
    return _label


def set_working_label(label: str | None) -> None:
    """Change the wait word without stacking a second Live line."""
    global _label
    text = (label or WORKING_WORD).strip() or WORKING_WORD
    if len(text) > MAX_LABEL:
        text = text[: MAX_LABEL - 1] + "…"
    _label = text


def working_enabled() -> bool:
    """Whether the wait animation would actually draw."""
    from .options import OPTIONS
    from .ui import console

    if OPTIONS.json_output:
        return False
    # Pytest is often attached to a real tty; drawing Live + cbreak there
    # freezes the suite and eats keystrokes.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return bool(console().is_terminal)


def render_working(elapsed: float, *, word: str = WORKING_WORD, esc: bool = True) -> Text:
    """One frame of the wait line, as a Rich ``Text``.

    ``elapsed`` is seconds since the wait started. The highlight window is two
    characters so the screenshot's yellow ``ng`` in ``Working...`` is what you
    get at 0.6s, then it wraps.
    """
    ticks = max(0, int(elapsed * 1000))
    spin = SPINNER_FRAMES[(ticks // SPINNER_MS) % len(SPINNER_FRAMES)]
    travel = len(word) + SHIMMER_WIDTH
    pos = (ticks // SHIMMER_MS) % travel - 1

    line = Text()
    line.append(spin, style="bold yellow")
    line.append(" ")
    for index, char in enumerate(word):
        if pos <= index < pos + SHIMMER_WIDTH:
            line.append(char, style="bold yellow")
        else:
            line.append(char, style="dim")
    if esc:
        line.append("  ")
        line.append("[esc]", style="dim")
    return line


class _WorkingDisplay:
    """A Live renderable that advances from the console clock."""

    def __init__(self) -> None:
        self._origin: float | None = None

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        now = console.get_time()
        if self._origin is None:
            self._origin = now
        yield render_working(now - self._origin, word=current_label())


@contextmanager
def working(
    *,
    enabled: bool | None = None,
    listen_escape: bool | None = None,
    label: str | None = None,
) -> Iterator[None]:
    """Show the wait animation for the duration of the block.

    Nested calls are a no-op so a repair retry does not stack a second line.
    Escape raises ``KeyboardInterrupt`` on a tty; callers already treat that as
    cancel. ``label`` is the walking word (``Thinking``, ``Reading Podfile``);
    nested calls can still change it via :func:`set_working_label`.
    """
    global _depth
    previous = current_label()
    if label:
        set_working_label(label)
    if enabled is None:
        enabled = working_enabled()
    if not enabled:
        try:
            yield
        finally:
            set_working_label(previous)
        return

    with _depth_lock:
        nested = _depth > 0
        _depth += 1
    if nested:
        try:
            yield
        finally:
            set_working_label(previous)
            with _depth_lock:
                _depth -= 1
        return

    from .ui import console

    stop = threading.Event()
    if listen_escape is None:
        listen_escape = sys.stdin.isatty()
    watcher: threading.Thread | None = None
    try:
        with Live(
            _WorkingDisplay(),
            console=console(),
            refresh_per_second=16,
            transient=True,
            redirect_stderr=False,
            redirect_stdout=False,
        ):
            if listen_escape:
                watcher = threading.Thread(
                    target=_watch_escape,
                    args=(stop,),
                    daemon=True,
                    name="rn-agent-esc",
                )
                watcher.start()
            yield
    finally:
        stop.set()
        if watcher is not None:
            watcher.join(timeout=0.4)
        set_working_label(previous)
        with _depth_lock:
            _depth -= 1


def _watch_escape(stop: threading.Event) -> None:
    """Raise KeyboardInterrupt when Escape is pressed by itself.

    Arrow keys arrive as ESC plus more bytes; those are drained, not treated as
    cancel. Terminal settings are restored even when the wait is interrupted.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        _watch_escape_windows(stop)
        return
    _watch_escape_posix(stop)


def _watch_escape_posix(stop: threading.Event) -> None:
    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover - non-posix
        return
    stream: TextIO = sys.stdin
    if not stream.isatty():
        return
    try:
        fd = stream.fileno()
        old = termios.tcgetattr(fd)
    except (termios.error, ValueError, OSError):
        return
    try:
        tty.setcbreak(fd)
        while not stop.is_set():
            ready, _, _ = select.select([stream], [], [], 0.05)
            if not ready:
                continue
            chunk = os.read(fd, 1)
            if chunk != b"\x1b":
                continue
            if select.select([stream], [], [], 0.02)[0]:
                while select.select([stream], [], [], 0)[0]:
                    os.read(fd, 1)
                continue
            stop.set()
            _interrupt_main()
            return
    finally:
        with contextlib.suppress(termios.error, OSError):
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _watch_escape_windows(stop: threading.Event) -> None:  # pragma: no cover
    # Guarded by `sys.platform` rather than try/except: that is what tells a type
    # checker on macOS or Linux that this module only exists on Windows.
    if sys.platform == "win32":
        import msvcrt

        while not stop.wait(0.05):
            if not msvcrt.kbhit():
                continue
            if msvcrt.getch() == b"\x1b":
                stop.set()
                _interrupt_main()
                return


def _interrupt_main() -> None:
    """Ask the waiting call to stop, the same way Ctrl+C would."""
    import _thread

    _thread.interrupt_main()
