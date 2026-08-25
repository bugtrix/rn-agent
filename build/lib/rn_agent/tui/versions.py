"""Pick a React Native version, using the same picker as everything else.

The catalogue comes from the npm packument (newest patch of each newer series).
A dead registry or ``--offline`` degrades to typing a version, which is the same
fallback the migration wizard has always had - the picker is a convenience, not
a requirement.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..cli import ui
from ..upgrade.versions import RnTarget, published_rn_targets
from .select import Choice, select

Picker = Callable[..., Choice | None]
Asker = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class UpgradePick:
    """What the upgrade picker returned."""

    kind: str  # "rn" or "deps"
    value: str


def pick_rn_version(
    current: str,
    *,
    targets: Sequence[RnTarget] | None = None,
    offline: bool = False,
    picker: Picker = select,
    asker: Asker,
) -> str | None:
    """A published version newer than ``current``, or a typed one."""
    available = targets if targets is not None else published_rn_targets(current, offline=offline)
    choices = rn_version_choices(available)
    if not available:
        return _type_version(current, asker)
    ui.blank()
    ui.key_values([("current React Native version", current)])
    chosen = picker(
        f"Target React Native version  ·  current {current}",
        choices,
        footer="↑↓ Navigate   Enter Select   type to search   Esc Cancel",
    )
    if chosen is None:
        return None
    if chosen.value == "type":
        return _type_version(current, asker)
    return chosen.value or None


def pick_upgrade(
    current: str,
    *,
    targets: Sequence[RnTarget] | None = None,
    offline: bool = False,
    picker: Picker = select,
    asker: Asker,
) -> UpgradePick | None:
    """One list: React Native versions, then JavaScript dependency policies."""
    available = targets if targets is not None else published_rn_targets(current, offline=offline)
    ui.blank()
    ui.key_values([("current React Native version", current)])
    chosen = picker(
        f"Upgrade  ·  React Native {current}",
        upgrade_choices(available),
        footer="↑↓ Navigate   Enter Select   type to search   Esc Cancel",
    )
    if chosen is None:
        return None
    if chosen.value == "rn:type":
        typed = _type_version(current, asker)
        return UpgradePick("rn", typed) if typed else None
    if chosen.value.startswith("rn:"):
        return UpgradePick("rn", chosen.value.removeprefix("rn:"))
    if chosen.value.startswith("deps:"):
        return UpgradePick("deps", chosen.value.removeprefix("deps:"))
    return None


def rn_version_choices(targets: Sequence[RnTarget]) -> list[Choice]:
    """Picker rows for a React Native move only."""
    rows = [_rn_choice(target, prefix="") for target in targets]
    rows.append(
        Choice(
            value="type",
            label="Type a version…",
            hint="if it is not in the list",
            group="Other",
        )
    )
    return rows


def upgrade_choices(targets: Sequence[RnTarget]) -> list[Choice]:
    """Picker rows for ``/upgrade``: RN versions first, then dependency policies."""
    rows = [_rn_choice(target, prefix="rn:") for target in targets]
    rows.append(
        Choice(
            value="rn:type",
            label="Type a React Native version…",
            hint="offline, or a version not listed",
            group="React Native",
        )
    )
    rows.extend(
        [
            Choice(
                value="deps:patch",
                label="patch",
                hint="same major.minor, newer patch",
                group="JavaScript dependencies",
            ),
            Choice(
                value="deps:minor",
                label="minor",
                hint="same major - the usual dependency bump",
                group="JavaScript dependencies",
            ),
            Choice(
                value="deps:latest",
                label="latest",
                hint="any newer version, including majors",
                group="JavaScript dependencies",
            ),
        ]
    )
    return rows


def _rn_choice(target: RnTarget, *, prefix: str) -> Choice:
    group = (
        "React Native"
        if prefix
        else ("Recommended" if target.newest_published else target.series)
    )
    return Choice(
        value=f"{prefix}{target.version}",
        label=target.version,
        hint=target.hint,
        group=group,
    )


def _type_version(current: str, asker: Asker) -> str | None:
    ui.blank()
    ui.key_values([("current React Native version", current)])
    answer = asker("Target React Native version:")
    if answer is None:
        ui.warning("no target version given - pass --to 0.86.0")
    return answer
