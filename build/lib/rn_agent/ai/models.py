"""Which models this account may actually use, and which one is live.

The `/model` picker must never present a hard-coded list as the truth. A key
sees the catalogue its own account is entitled to, and that is neither a vendor
marketing page nor a constant in this repository. So the order of truth is the
provider's own catalogue endpoint, then a cached copy of it, and only then the
provider class's bundled suggestions - labelled as suggestions, never as facts.

Two rules shape the code:

* **A model list is never worth a failed command.** Any remote lookup may fail;
  :class:`~rn_agent.errors.RNAgentError` is caught, logged at debug, and the
  answer degrades to cache-then-suggestions so the picker still opens offline.
* **The cache holds model ids and a timestamp, nothing else.** It lives beside
  the user config, so a credential or account detail landing in it would be a
  leak; nothing here writes a value it did not read from a model catalogue.

A partial name never silently picks a model: :meth:`ModelRegistry.resolve`
answers ``None`` when a query fits two, because switching to the wrong model is
a quiet and expensive mistake.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any, Final

from ..core.logging import get_logger
from ..core.paths import user_config_dir
from ..errors import RNAgentError
from ..utils.io import read_json, write_json
from .provider import AIProvider

#: Bumped when the on-disk shape changes; an older file is dropped, not migrated.
CACHE_VERSION: Final = 1
CACHE_FILE_NAME: Final = "model-cache.json"
DEFAULT_TTL_SECONDS: Final = 24.0 * 60.0 * 60.0

# Match quality bands. The gap between bands is wider than any penalty below,
# so a prefix hit can never be outranked by a scattered subsequence hit.
_EXACT: Final = 4000
_PREFIX: Final = 3000
_SUBSTRING: Final = 2000
_SUBSEQUENCE: Final = 1000
_MAX_PENALTY: Final = 900


class ModelSource(StrEnum):
    """Where a model id came from - which is how much it can be trusted."""

    #: The account's real catalogue, read from the provider's API.
    PROVIDER = "provider"
    #: The provider class's bundled suggestions: a starting point, not a promise.
    SUGGESTED = "suggested"
    #: Named in `.rn-agent/config.yaml` but not offered by the provider.
    CONFIG = "config"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One selectable model."""

    id: str
    provider: str
    source: ModelSource
    label: str = ""
    available: bool = True

    @property
    def display(self) -> str:
        """What the picker prints; providers rarely give a nicer name than the id."""
        return self.label or self.id


@dataclass(frozen=True, slots=True)
class ModelGroup:
    """One provider's models, as a picker section."""

    provider: str
    label: str
    models: tuple[ModelInfo, ...]
    connected: bool
    note: str | None = None


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """One provider's catalogue as last read, with the time it was read."""

    fetched_at: float
    models: tuple[str, ...]


@dataclass(slots=True)
class ModelRegistry:
    """Answers what is selectable, cached across runs so the picker opens fast."""

    cache_file: Path = field(default_factory=lambda: user_config_dir() / CACHE_FILE_NAME)
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    logger: logging.Logger = field(default_factory=lambda: get_logger("models"))
    _entries: dict[str, _CacheEntry] | None = field(default=None, init=False, repr=False)

    # -- discovery ---------------------------------------------------------
    def discover(
        self,
        provider: str,
        *,
        build: Callable[[], AIProvider] | None,
        connected: bool,
        suggested: Sequence[str] = (),
        refresh: bool = False,
    ) -> tuple[ModelInfo, ...]:
        """What ``provider`` offers, best source first.

        A fresh cache entry wins unless ``refresh``; then the live catalogue,
        which is cached; then the bundled suggestions. A provider that is not
        connected is never asked, so opening the picker cannot cost a request
        for an account the developer has not logged into.
        """
        if not refresh:
            fresh = self._cached(provider)
            if fresh:
                return self._infos(provider, fresh, ModelSource.PROVIDER, available=connected)
        live = self._fetch(provider, build) if connected and build is not None else None
        if live:
            self._store(provider, live)
            return self._infos(provider, live, ModelSource.PROVIDER, available=connected)
        # Nothing live: a stale copy of the real catalogue still beats guessing.
        stale = self._cached(provider, ignore_ttl=True)
        if stale:
            return self._infos(provider, stale, ModelSource.PROVIDER, available=connected)
        return self._infos(provider, suggested, ModelSource.SUGGESTED, available=connected)

    def grouped(
        self,
        *,
        active_provider: str | None,
        active_model: str | None,
        providers: Sequence[tuple[str, str, bool, Sequence[str]]],
        build: Callable[[str], AIProvider] | None = None,
        refresh: bool = False,
    ) -> list[ModelGroup]:
        """Picker sections: the active provider first, then the order given.

        The active model is guaranteed to be the first entry of the active
        provider's group even when the catalogue does not offer it - a model
        chosen in config must stay visible, marked :attr:`ModelSource.CONFIG`,
        rather than vanishing from the list the developer is looking at.
        """
        groups: list[ModelGroup] = []
        for name, label, connected, suggested in sorted(
            providers, key=lambda entry: entry[0] != active_provider
        ):
            models = self.discover(
                name,
                build=partial(build, name) if build is not None else None,
                connected=connected,
                suggested=suggested,
                refresh=refresh,
            )
            if name == active_provider and active_model:
                models = _promote(models, active_model, provider=name, connected=connected)
            groups.append(
                ModelGroup(
                    provider=name,
                    label=label,
                    models=models,
                    connected=connected,
                    note=None if connected else f"not connected - /login {name}",
                )
            )
        return groups

    # -- lookup ------------------------------------------------------------
    @staticmethod
    def resolve(query: str, models: Sequence[ModelInfo]) -> ModelInfo | None:
        """The one model ``query`` names, or ``None`` if that is not one model.

        Ambiguity is an answer: `/model claude-sonnet` with two matching ids
        must return nothing rather than pick the first and quietly reroute
        every later request.
        """
        text = query.strip()
        if not text:
            return None
        for model in models:
            if model.id == text:
                return model
        lowered = text.casefold()
        for candidates in (
            [model for model in models if model.id.casefold() == lowered],
            [model for model in models if model.id.casefold().startswith(lowered)],
            [model for model in models if lowered in model.id.casefold()],
        ):
            if candidates:
                return candidates[0] if len(candidates) == 1 else None
        return None

    @staticmethod
    def search(query: str, models: Sequence[ModelInfo]) -> list[ModelInfo]:
        """Fuzzy filter for the picker's search box, best match first.

        Characters need only appear in order (``clop`` finds
        ``claude-opus-4-1``), because that is how a developer types at a
        filter. Equal scores keep the order they were given in.
        """
        text = query.strip().casefold()
        if not text:
            return list(models)
        scored: list[tuple[int, ModelInfo]] = []
        for model in models:
            score = _best_score(text, model)
            if score is not None:
                scored.append((score, model))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [model for _, model in scored]

    # -- cache -------------------------------------------------------------
    def invalidate(self, provider: str | None = None) -> None:
        """Forget a cached catalogue - or all of them - so the next read is live."""
        entries = self._load()
        if provider is None:
            if not entries:
                return
            entries.clear()
        elif entries.pop(provider, None) is None:
            return
        self._write(entries)

    def _fetch(self, provider: str, build: Callable[[], AIProvider]) -> tuple[str, ...] | None:
        try:
            return tuple(build().list_models())
        except RNAgentError as exc:
            # Offline, rate limited or a rejected key: the picker still opens on
            # what we knew last time. Debug, not warning - this is expected.
            self.logger.debug("no live catalogue for %s: %s", provider, exc.message)
            return None

    def _cached(self, provider: str, *, ignore_ttl: bool = False) -> tuple[str, ...]:
        entry = self._load().get(provider)
        if entry is None:
            return ()
        if ignore_ttl:
            return entry.models
        if self.ttl_seconds <= 0:
            return ()
        age = time.time() - entry.fetched_at
        return entry.models if age <= self.ttl_seconds else ()

    def _store(self, provider: str, models: Sequence[str]) -> None:
        entries = self._load()
        entries[provider] = _CacheEntry(fetched_at=time.time(), models=tuple(models))
        self._write(entries)

    def _load(self) -> dict[str, _CacheEntry]:
        if self._entries is None:
            self._entries = _parse_cache(read_json(self.cache_file, default=None))
        return self._entries

    def _write(self, entries: dict[str, _CacheEntry]) -> None:
        self._entries = entries
        payload = {
            "version": CACHE_VERSION,
            "providers": {
                name: {"fetched_at": entry.fetched_at, "models": list(entry.models)}
                for name, entry in entries.items()
            },
        }
        try:
            write_json(self.cache_file, payload)
        except OSError as exc:  # pragma: no cover - read-only home
            # A cache we cannot write is a slower picker, not a broken one.
            self.logger.debug("could not write %s: %s", self.cache_file, exc)

    @staticmethod
    def _infos(
        provider: str,
        ids: Sequence[str],
        source: ModelSource,
        *,
        available: bool,
    ) -> tuple[ModelInfo, ...]:
        seen: set[str] = set()
        infos: list[ModelInfo] = []
        for identifier in ids:
            name = identifier.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            infos.append(
                ModelInfo(id=name, provider=provider, source=source, available=available)
            )
        return tuple(infos)


def _promote(
    models: tuple[ModelInfo, ...], active: str, *, provider: str, connected: bool
) -> tuple[ModelInfo, ...]:
    """Move the active model to the front, adding it when it is not offered."""
    rest = tuple(model for model in models if model.id != active)
    current = next((model for model in models if model.id == active), None)
    if current is None:
        current = ModelInfo(
            id=active, provider=provider, source=ModelSource.CONFIG, available=connected
        )
    return (current, *rest)


def _parse_cache(document: Any) -> dict[str, _CacheEntry]:
    """Read the cache defensively: anything unexpected means "no cache"."""
    if not isinstance(document, Mapping) or document.get("version") != CACHE_VERSION:
        return {}
    providers = document.get("providers")
    if not isinstance(providers, Mapping):
        return {}
    entries: dict[str, _CacheEntry] = {}
    for name, payload in providers.items():
        if not isinstance(name, str) or not isinstance(payload, Mapping):
            continue
        fetched_at = payload.get("fetched_at")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, int | float):
            continue
        models = payload.get("models")
        if not isinstance(models, list):
            continue
        ids = tuple(item for item in models if isinstance(item, str) and item)
        if ids:
            entries[name] = _CacheEntry(fetched_at=float(fetched_at), models=ids)
    return entries


def _best_score(query: str, model: ModelInfo) -> int | None:
    """The better of the id and label match, or ``None`` when neither matches."""
    scores = [
        score
        for score in (_match_score(query, model.id), _match_score(query, model.label))
        if score is not None
    ]
    return max(scores) if scores else None


def _match_score(query: str, text: str) -> int | None:
    """How well ``text`` matches an already-casefolded ``query``; higher is better."""
    if not text:
        return None
    lowered = text.casefold()
    if lowered == query:
        return _EXACT
    span = _subsequence_span(query, lowered)
    if span is None:
        return None
    contiguous = lowered.find(query)
    if contiguous >= 0:
        base, start, slack = (_PREFIX, 0, 0) if contiguous == 0 else (_SUBSTRING, contiguous, 0)
    else:
        first, last = span
        base, start, slack = _SUBSEQUENCE, first, last - first + 1 - len(query)
    # Earlier and tighter wins, but never enough to jump a band.
    return base - min(_MAX_PENALTY, slack * 10 + start)


def _subsequence_span(query: str, text: str) -> tuple[int, int] | None:
    """Bounds of the left-most in-order match of ``query``, or ``None``."""
    first = -1
    cursor = 0
    for char in query:
        found = text.find(char, cursor)
        if found < 0:
            return None
        if first < 0:
            first = found
        cursor = found + 1
    return None if first < 0 else (first, cursor - 1)
