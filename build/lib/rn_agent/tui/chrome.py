"""The frame around the conversation: banner, status bar, help footer.

The status line is the part that earns its keep. A developer running an agent
against a real app needs four facts visible at all times - which account is
answering, which model, which React Native version, and whether the tree is
clean - because every one of them changes what the answer means. Showing them
continuously is cheaper than remembering to ask.

Everything here is Rich, and everything degrades: no colour when `NO_COLOR` is
set, no box-drawing assumptions beyond what the existing reports already use.
The one hard rule is that an unauthenticated session says so, loudly, rather
than looking ready and failing on the first request.
"""

from __future__ import annotations

from rich.box import ROUNDED
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..cli import ui
from ..constants import APP_TITLE, APP_VERSION
from .session import StatusSnapshot

TAGLINE = "React Native AI Engineering Agent"
SEPARATOR = " · "


def render_banner(snapshot: StatusSnapshot) -> None:
    """The header shown once when the terminal opens."""
    heading = Text(APP_TITLE, style="bold")
    heading.append(f"  {APP_VERSION}", style="dim")
    heading.append(f"\n{TAGLINE}", style="dim")

    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="dim", justify="left", no_wrap=True)
    facts.add_column(style="bold")
    facts.add_row("Project", snapshot.project_name or "-")
    facts.add_row("RN", snapshot.rn_version or _unscanned_hint(snapshot))
    facts.add_row("Provider", _provider_cell(snapshot))
    facts.add_row("Model", snapshot.model or "[dim]none selected · /model[/dim]")
    if snapshot.account:
        facts.add_row("Account", snapshot.account)
    if snapshot.dry_run:
        facts.add_row("Mode", "[warn]dry run - nothing will be written[/warn]")

    ui.console().print(
        Panel(Group(heading, "", facts), box=ROUNDED, border_style="cyan", expand=False)
    )
    if not snapshot.ready:
        ui.console().print(f"  [warn]{connect_hint(snapshot)}[/warn]")
    ui.console().print(f"  [muted]{help_hint()}[/muted]")
    ui.blank()


def _provider_cell(snapshot: StatusSnapshot) -> str:
    """Provider plus the *real* auth method - never a flattering guess."""
    if not snapshot.provider:
        return "[dim]not connected · /login[/dim]"
    label = snapshot.provider_label or snapshot.provider
    method = snapshot.auth_label
    state = "connected" if snapshot.connected else "[warn]not connected[/warn]"
    return f"{label}  [dim]auth: {method} · {state}[/dim]"


def _unscanned_hint(snapshot: StatusSnapshot) -> str:
    return "-" if snapshot.scanned else "[dim]unknown · /scan[/dim]"


def status_line(snapshot: StatusSnapshot) -> str:
    """The one-line status bar, as Rich markup.

    Shape follows the spec: ``Anthropic · Claude Sonnet · RN 0.86 · Git clean``,
    and an unusable session leads with the warning instead of the provider.
    """
    if not snapshot.provider:
        return "[warn]⚠ AI not connected[/warn]" + SEPARATOR + "[info]/login[/info]"
    if not snapshot.connected:
        label = snapshot.provider_label or snapshot.provider
        return (
            f"[warn]⚠ {label} not connected[/warn]{SEPARATOR}"
            f"[info]/login {snapshot.provider}[/info]"
        )

    parts: list[str] = [snapshot.provider_label or snapshot.provider]
    if snapshot.auth_method is not None and snapshot.auth_method.value == "none":
        # A local runtime has no account; saying "Local" is the honest label.
        parts.append("Local")
    parts.append(snapshot.model or "no model")
    parts.append(f"RN {snapshot.rn_version}" if snapshot.rn_version else "RN unknown")
    parts.append(_git_cell(snapshot))
    if snapshot.turns:
        parts.append(f"{snapshot.turns} turns")
    if snapshot.dry_run:
        parts.append("[warn]dry run[/warn]")
    return SEPARATOR.join(parts)


def _git_cell(snapshot: StatusSnapshot) -> str:
    if snapshot.git_dirty is None:
        return "no git"
    if snapshot.git_dirty:
        return "[warn]Git dirty[/warn]"
    branch = f" {snapshot.git_branch}" if snapshot.git_branch else ""
    return f"[ok]Git clean[/ok]{branch}"


def render_status(snapshot: StatusSnapshot) -> None:
    ui.console().print(f"  [muted]{status_line(snapshot)}[/muted]")


def connect_hint(snapshot: StatusSnapshot) -> str:
    """What to type next when the session cannot make a request."""
    if not snapshot.provider:
        return "no provider connected - run /login to sign in with your own account"
    if not snapshot.connected:
        return f"{snapshot.provider} is not connected - run /login {snapshot.provider}"
    if not snapshot.model:
        return "no model selected - run /model"
    return ""


def help_hint() -> str:
    return "/help for commands · /login to connect · Ctrl+K palette · Ctrl+D to exit"


def render_help(rows: list[tuple[str, str]], *, title: str = "Commands") -> None:
    """The ``/help`` table: every slash command and what it does.

    Usage strings are escaped before they reach Rich: ``/model [name]`` contains
    what Rich reads as a style tag, and an unescaped one silently deletes the
    placeholder it is meant to document.
    """
    ui.table(
        ["Command", "What it does"],
        [(escape(usage), summary) for usage, summary in rows],
        title=title,
        styles=["bold", None],
    )
    ui.note("Ctrl+K opens the palette · Ctrl+P cycles models · Esc cancels a picker")


def render_auth_table(rows: list[tuple[str, str, str, str]]) -> None:
    """``/whoami``-style table: provider, auth method, state, detail."""
    ui.table(["Provider", "Auth", "State", "Detail"], rows, title="AI accounts")


def spinner_text(action: str, snapshot: StatusSnapshot) -> str:
    """Progress label that names the model doing the work.

    The live wait line is ``Working...`` (see ``cli.working``). This string is
    the quieter status-bar form: which account is being spent.
    """
    model = snapshot.model or "model"
    provider = snapshot.provider_label or snapshot.provider or "provider"
    return f"{action} · {provider} {model}"
