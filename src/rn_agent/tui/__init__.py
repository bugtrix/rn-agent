"""The interactive terminal.

``rn-agent`` with no arguments opens this; every subcommand still works exactly
as before. The split inside is deliberate:

* :mod:`~rn_agent.tui.select` - one keyboard-driven picker, reused by the
  provider list, the model list, the palette and every dialog;
* :mod:`~rn_agent.tui.session` - what a session owns (project, account, model,
  conversation) so switching provider or model loses none of it;
* :mod:`~rn_agent.tui.router` - slash commands, which re-enter the real CLI
  rather than reimplementing it;
* :mod:`~rn_agent.tui.handlers` - the session-scoped commands that have no CLI
  twin (``/login``, ``/provider``, ``/model``, ``/status``, ``/context``);
* :mod:`~rn_agent.tui.agent` - what a line of prose does: chat, look things up,
  and apply the files the prompt asked for, or offer ``/migrate`` /
  ``/upgrade`` when that is the work;
* :mod:`~rn_agent.tui.chrome` / :mod:`~rn_agent.tui.dialogs` - the frame and the
  confirmations.
"""

from __future__ import annotations

from .app import Terminal, build_session, run
from .router import CommandRouter, RouteResult, SlashCommand
from .select import Choice, Selector, select
from .session import SessionManager, StatusSnapshot

__all__ = [
    "Choice",
    "CommandRouter",
    "RouteResult",
    "Selector",
    "SessionManager",
    "SlashCommand",
    "StatusSnapshot",
    "Terminal",
    "build_session",
    "run",
    "select",
]
