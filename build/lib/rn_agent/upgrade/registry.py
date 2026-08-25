"""Reading the npm registry - the only remote source of dependency facts.

The *abbreviated* packument is requested on purpose
(``application/vnd.npm.install-v1+json``): it is what npm itself installs from,
it is an order of magnitude smaller than the full document, and it still carries
the two things that decide an upgrade - each version's ``peerDependencies`` and
its ``engines``.

Unreachable is not fatal. ``available`` goes false, every lookup returns
``None``, and the caller reports "registry unreachable" instead of inventing a
target version. That is the same rule the scanner applies to a missing
``node_modules``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

from ..core.logging import get_logger
from ..errors import TransportError
from ..net.http import DEFAULT_TIMEOUT, JsonTransport, default_transport
from ..utils.semver import Version, parse, satisfies

DEFAULT_REGISTRY: Final = "https://registry.npmjs.org"
ABBREVIATED: Final = "application/vnd.npm.install-v1+json"


@dataclass(frozen=True, slots=True)
class PackageVersion:
    """One published version, as the registry describes it."""

    version: str
    dependencies: Mapping[str, str] = field(default_factory=dict)
    peer_dependencies: Mapping[str, str] = field(default_factory=dict)
    engines: Mapping[str, str] = field(default_factory=dict)
    deprecated: str | None = None

    @property
    def parsed(self) -> Version | None:
        return parse(self.version)

    @property
    def prerelease(self) -> bool:
        version = self.parsed
        return bool(version and version.prerelease)


@dataclass(frozen=True, slots=True)
class Packument:
    """A package's published versions and its ``latest`` dist-tag."""

    name: str
    latest: str | None = None
    versions: tuple[PackageVersion, ...] = ()

    def version(self, number: str) -> PackageVersion | None:
        for entry in self.versions:
            if entry.version == number:
                return entry
        return None

    def stable(self) -> tuple[PackageVersion, ...]:
        """Published versions excluding pre-releases, oldest first."""
        usable = [entry for entry in self.versions if entry.parsed and not entry.prerelease]
        usable.sort(key=lambda entry: entry.parsed)  # type: ignore[arg-type,return-value]
        return tuple(usable)

    def newest(self) -> PackageVersion | None:
        """The newest stable version, or the ``latest`` tag if that is newer."""
        stable = self.stable()
        tagged = self.version(self.latest) if self.latest else None
        if tagged is not None and not tagged.prerelease:
            return tagged
        return stable[-1] if stable else None

    def highest_satisfying(self, spec: str | None) -> PackageVersion | None:
        """Newest stable version inside ``spec``; ``None`` when nothing matches.

        An undecidable range (``workspace:*``, a git URL) yields ``None`` rather
        than the newest version, because "I cannot tell" must not become an
        upgrade suggestion.
        """
        if not spec:
            return None
        best: PackageVersion | None = None
        for entry in self.stable():
            if satisfies(entry.version, spec) is True:
                best = entry
        return best


@dataclass(slots=True)
class NpmRegistry:
    """Looks packages up, caches per process, and degrades to "unknown"."""

    transport: JsonTransport | None = None
    base_url: str = DEFAULT_REGISTRY
    timeout: float = 30.0
    logger: logging.Logger = field(default_factory=lambda: get_logger("registry"))
    available: bool = True
    _cache: dict[str, Packument | None] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.transport is None:
            self.transport = default_transport()
        self.base_url = self.base_url.rstrip("/")

    # -- lookups -----------------------------------------------------------
    def packument(self, name: str) -> Packument | None:
        """Everything published for ``name``, or ``None`` if it cannot be read."""
        if name in self._cache:
            return self._cache[name]
        if not self.available:
            return None
        document = self._fetch(name)
        self._cache[name] = document
        return document

    def peer_dependencies(self, name: str, version: str) -> Mapping[str, str] | None:
        document = self.packument(name)
        if document is None:
            return None
        entry = document.version(version)
        return entry.peer_dependencies if entry else None

    def engines(self, name: str, version: str) -> Mapping[str, str] | None:
        document = self.packument(name)
        if document is None:
            return None
        entry = document.version(version)
        return entry.engines if entry else None

    # -- internals ---------------------------------------------------------
    def _fetch(self, name: str) -> Packument | None:
        assert self.transport is not None  # set in __post_init__
        url = f"{self.base_url}/{_encode(name)}"
        try:
            response = self.transport.request(
                "GET",
                url,
                headers={"accept": ABBREVIATED},
                timeout=self.timeout or DEFAULT_TIMEOUT,
            )
        except TransportError as exc:
            # One failure means the network is gone for this run; do not retry
            # 100 packages against a host that is not answering.
            self.available = False
            self.logger.warning("npm registry unreachable: %s", exc.message)
            return None
        if response.status == 404:
            self.logger.debug("package not found in registry: %s", name)
            return None
        if not response.ok:
            self.logger.warning("registry returned HTTP %s for %s", response.status, name)
            return None
        return _parse_packument(name, response.body)


def _encode(name: str) -> str:
    """``@scope/name`` -> ``@scope%2Fname``, which is what the registry expects."""
    if name.startswith("@") and "/" in name:
        scope, _, rest = name.partition("/")
        return f"{quote(scope, safe='@')}%2F{quote(rest, safe='')}"
    return quote(name, safe="")


def _parse_packument(name: str, body: Mapping[str, Any]) -> Packument | None:
    versions = body.get("versions")
    if not isinstance(versions, Mapping):
        return None
    parsed: list[PackageVersion] = []
    for number, payload in versions.items():
        if not isinstance(number, str) or not isinstance(payload, Mapping):
            continue
        deprecated = payload.get("deprecated")
        parsed.append(
            PackageVersion(
                version=number,
                dependencies=_string_map(payload.get("dependencies")),
                peer_dependencies=_string_map(payload.get("peerDependencies")),
                engines=_string_map(payload.get("engines")),
                deprecated=deprecated if isinstance(deprecated, str) else None,
            )
        )
    tags = body.get("dist-tags")
    latest = tags.get("latest") if isinstance(tags, Mapping) else None
    reported = body.get("name")
    return Packument(
        name=reported if isinstance(reported, str) and reported else name,
        latest=latest if isinstance(latest, str) else None,
        versions=tuple(parsed),
    )


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }
