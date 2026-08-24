"""Where a migration's facts come from.

The upstream diff between two React Native versions is published by the
community's ``rn-diff-purge`` repository - it is what the Upgrade Helper website
renders, and it is the closest thing to an authoritative "what changed in the
template" answer. It is fetched through the shared transport and cached under
``.rn-agent/cache/migrations``, because a migration is usually run more than once
and re-downloading is both slow and rude.

Unreachable is not fatal: the fetch returns ``None`` with a reason, and the
planner degrades to what ``package.json`` and the local rules can prove. That is
the same rule the scanner applies to a missing ``node_modules``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ..core.logging import get_logger
from ..errors import TransportError
from ..net.http import JsonTransport, default_transport
from ..utils.io import atomic_write_text, read_text

DIFF_BASE: Final = "https://raw.githubusercontent.com/react-native-community/rn-diff-purge/diffs/diffs"
UPGRADE_HELPER: Final = "https://react-native-community.github.io/upgrade-helper"


@dataclass(frozen=True, slots=True)
class DiffDocument:
    """One version-to-version diff, and where it came from."""

    from_version: str
    to_version: str
    text: str
    source: str
    cached: bool = False

    @property
    def empty(self) -> bool:
        return not self.text.strip()


@dataclass(slots=True)
class DiffSource:
    """Fetches (and caches) the upstream template diff."""

    cache_dir: Path
    transport: JsonTransport | None = None
    base_url: str = DIFF_BASE
    timeout: float = 60.0
    logger: logging.Logger = field(default_factory=lambda: get_logger("migration"))
    #: Set when a fetch failed, so the report can say why the plan is thin.
    reason: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.transport is None:
            self.transport = default_transport()
        self.base_url = self.base_url.rstrip("/")

    # -- fetching ----------------------------------------------------------
    def url_for(self, from_version: str, to_version: str) -> str:
        return f"{self.base_url}/{from_version}..{to_version}.diff"

    def helper_url(self, from_version: str, to_version: str) -> str:
        """The human page for the same diff, for the report to cite."""
        return f"{UPGRADE_HELPER}/?from={from_version}&to={to_version}"

    def fetch(
        self, from_version: str, to_version: str, *, offline: bool = False
    ) -> DiffDocument | None:
        """The diff for this version pair, from cache or the network."""
        cached = self._read_cache(from_version, to_version)
        if cached is not None:
            return cached
        if offline:
            self.reason = "offline: the upstream template diff was not fetched"
            return None

        url = self.url_for(from_version, to_version)
        assert self.transport is not None  # set in __post_init__
        try:
            response = self.transport.request(
                "GET", url, headers={"accept": "text/plain"}, timeout=self.timeout
            )
        except TransportError as exc:
            self.reason = f"could not reach {url}: {exc.message}"
            self.logger.warning("%s", self.reason)
            return None
        if response.status == 404:
            self.reason = (
                f"no published diff for {from_version} -> {to_version} "
                "(is that a real released pair?)"
            )
            self.logger.warning("%s", self.reason)
            return None
        if not response.ok:
            self.reason = f"{url} returned HTTP {response.status}"
            self.logger.warning("%s", self.reason)
            return None
        if not response.text.strip():
            self.reason = f"{url} returned an empty diff"
            return None

        self._write_cache(from_version, to_version, response.text)
        return DiffDocument(
            from_version=from_version,
            to_version=to_version,
            text=response.text,
            source=url,
        )

    # -- cache -------------------------------------------------------------
    def cache_path(self, from_version: str, to_version: str) -> Path:
        return self.cache_dir / "migrations" / f"{from_version}..{to_version}.diff"

    def _read_cache(self, from_version: str, to_version: str) -> DiffDocument | None:
        path = self.cache_path(from_version, to_version)
        text = read_text(path)
        if text is None or not text.strip():
            return None
        self.logger.debug("using cached diff %s", path)
        return DiffDocument(
            from_version=from_version,
            to_version=to_version,
            text=text,
            source=str(path),
            cached=True,
        )

    def _write_cache(self, from_version: str, to_version: str, text: str) -> None:
        path = self.cache_path(from_version, to_version)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, text)
        except OSError as exc:  # pragma: no cover - unwritable cache
            self.logger.debug("could not cache the diff: %s", exc)
