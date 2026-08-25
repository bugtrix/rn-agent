"""Which React Native version the developer is asking to move to.

``rn-agent upgrade`` used to mean "bump JavaScript ranges". Developers type
``upgrade`` when they mean React Native itself, so this module is the shared
decision for both the command line and the terminal wizard:

* a published version, or a series like ``0.86``, is a React Native move;
* ``patch`` / ``minor`` / ``latest`` (and ``--deps`` / ``--only``) is still a
  dependency bump;
* anything else is a question the wizard should ask, not a guess.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ..errors import RNAgentError
from ..utils.semver import Version, parse
from .planner import POLICIES
from .registry import NpmRegistry, Packument

#: ``0.86`` or ``v0.86`` - a series, not an exact release.
_SERIES_RE = re.compile(r"^v?(\d+\.\d+)$")

Kind = Literal["rn", "deps", "ask"]


@dataclass(frozen=True, slots=True)
class RnTarget:
    """One published React Native version the developer can move to."""

    version: str
    series: str
    newest_published: bool = False
    current_series: bool = False

    @property
    def hint(self) -> str:
        if self.newest_published:
            return "newest published"
        if self.current_series:
            return "latest patch of your series"
        return f"{self.series} latest"


@dataclass(frozen=True, slots=True)
class UpgradeRequest:
    """What ``upgrade`` should do, given the flags the developer passed."""

    kind: Kind
    version: str | None = None
    policy: str = "minor"


def classify_upgrade(
    *,
    to: str | None = None,
    target: str | None = None,
    deps: bool = False,
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
    native: bool = False,
) -> UpgradeRequest:
    """Turn flags into an RN move, a dependency bump, or "ask".

    ``--target`` is overloaded on purpose: developers write both
    ``--target minor`` and ``--target 0.86.0``. A policy name is a dependency
    bump; anything that parses as a version is React Native.
    """
    requested = _first(to, target)
    policy = target if target in POLICIES else None
    rn_spec = None if requested in POLICIES else requested

    if rn_spec and rn_spec not in POLICIES and parse(rn_spec) is None:
        raise RNAgentError(
            f"unknown upgrade target: {rn_spec}",
            hint=(
                "Pass a React Native version (--to 0.86.0) or a dependency "
                f"policy ({', '.join(POLICIES)})."
            ),
        )

    has_rn = bool(rn_spec) and parse(rn_spec) is not None
    has_deps = deps or bool(only) or bool(skip) or native or policy is not None
    if has_rn and has_deps:
        raise RNAgentError(
            "pass either a React Native version or dependency flags, not both",
            hint="Use --to 0.86.0 to move React Native, or --deps to bump packages.",
        )
    if has_rn:
        return UpgradeRequest(kind="rn", version=rn_spec)
    if has_deps:
        return UpgradeRequest(kind="deps", policy=policy or "minor")
    return UpgradeRequest(kind="ask")


def list_rn_targets(current: str, document: Packument | None) -> list[RnTarget]:
    """Newest stable patch of each series newer than ``current``.

    One row per series keeps the picker short: developers almost always want
    the latest patch of a minor, not every 0.81.x that was ever published.
    """
    current_version = parse(current)
    if document is None or current_version is None:
        return []
    newest = document.newest()
    newest_version = newest.version if newest else None
    by_series: dict[str, str] = {}
    for entry in document.stable():
        parsed = entry.parsed
        if parsed is None or parsed <= current_version:
            continue
        previous = by_series.get(parsed.series)
        previous_parsed = parse(previous) if previous else None
        if previous_parsed is None or parsed > previous_parsed:
            by_series[parsed.series] = entry.version
    ordered = sorted(
        by_series.items(),
        key=lambda item: parse(item[0]) or Version(0),
        reverse=True,
    )
    return [
        RnTarget(
            version=version,
            series=series,
            newest_published=version == newest_version,
            current_series=series == current_version.series,
        )
        for series, version in ordered
    ]


def published_rn_targets(
    current: str,
    *,
    offline: bool = False,
    registry: NpmRegistry | None = None,
) -> list[RnTarget]:
    """Load targets from the registry. An empty list means "ask the developer"."""
    if offline:
        return []
    try:
        client = registry if registry is not None else NpmRegistry()
        document = client.packument("react-native")
    except Exception:  # noqa: BLE001 - a dead registry is "type it yourself"
        return []
    return list_rn_targets(current, document)


def concrete_rn_version(
    spec: str,
    *,
    offline: bool = False,
    registry: NpmRegistry | None = None,
) -> str:
    """Expand a series and confirm the version exists, when the registry can."""
    if offline:
        return spec.strip().lstrip("v")
    document = None
    try:
        client = registry if registry is not None else NpmRegistry()
        document = client.packument("react-native")
    except Exception:  # noqa: BLE001 - fall back to the spec the developer typed
        document = None
    return resolve_rn_target(spec, document)


def resolve_rn_target(spec: str, document: Packument | None = None) -> str:
    """Turn ``0.86`` / ``0.86.0`` / ``v0.86.1`` into a concrete published version.

    A two-part spec is a series: the newest stable patch of that series is what
    "upgrade to 0.86" means. An exact spec must actually be published when the
    packument is available, so a typo fails here rather than halfway through a
    migration.
    """
    parsed = parse(spec)
    if parsed is None:
        raise RNAgentError(
            f"{spec} is not a React Native version",
            hint="Use a released version, for example 0.86.0, or a series like 0.86.",
        )
    if document is None:
        return spec.strip().lstrip("v")
    if _SERIES_RE.fullmatch(spec.strip()):
        series = parsed.series
        best: str | None = None
        for entry in document.stable():
            if entry.parsed is None or entry.parsed.series != series:
                continue
            best_parsed = parse(best) if best else None
            if best_parsed is None or entry.parsed > best_parsed:
                best = entry.version
        if best is None:
            raise RNAgentError(
                f"no published react-native {series} release",
                hint="Pick a series the registry actually has, for example 0.86.",
            )
        return best
    exact = document.version(spec.strip().lstrip("v")) or document.version(str(parsed))
    if exact is None:
        raise RNAgentError(
            f"react-native@{spec} is not published",
            hint="Run without --offline to pick from published versions.",
        )
    return exact.version


def _first(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None
