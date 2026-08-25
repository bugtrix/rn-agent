"""The model registry: what the picker may offer, and what it may not invent.

Every provider here is a stub whose catalogue is a constant and whose calls are
counted, so the assertions are about the registry's order of truth - live
catalogue, cache, suggestions - and never about a live account.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from rn_agent.ai.models import CACHE_VERSION, ModelInfo, ModelRegistry, ModelSource
from rn_agent.core.paths import user_config_dir
from rn_agent.errors import ProviderError, TransportError
from rn_agent.utils.io import write_json

#: Real ids only - a test that invents a model name teaches the wrong lesson.
LIVE = ("claude-sonnet-4-5", "claude-opus-4-1")
SUGGEST = ("claude-haiku-4-5",)
CATALOGUE = (
    "claude-sonnet-4-5",
    "claude-opus-4-1",
    "claude-3-5-sonnet-20240620",
    "claude-3-5-sonnet-20241022",
)
SECRET = "sk-ant-test-0123456789abcdef"


class StubProvider:
    """Answers with a fixed catalogue, or raises, and records every call."""

    def __init__(self, models=LIVE, error=None):
        self.models = tuple(models)
        self.error = error
        self.calls = 0
        # Present so the cache assertions can prove a credential never leaks.
        self.credential = SECRET

    def list_models(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.models


def make_registry(tmp_path: Path, **kwargs) -> ModelRegistry:
    return ModelRegistry(cache_file=tmp_path / "model-cache.json", **kwargs)


def cache_document(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "model-cache.json").read_text(encoding="utf-8"))


def as_models(ids, provider="anthropic", **kwargs) -> tuple[ModelInfo, ...]:
    return tuple(
        ModelInfo(id=name, provider=provider, source=ModelSource.PROVIDER, **kwargs)
        for name in ids
    )


# --- discovery -------------------------------------------------------------
def test_the_default_cache_lives_beside_the_user_config():
    assert ModelRegistry().cache_file == user_config_dir() / "model-cache.json"


def test_the_live_catalogue_wins_and_is_cached(tmp_path):
    stub = StubProvider()
    registry = make_registry(tmp_path)

    found = registry.discover("anthropic", build=lambda: stub, connected=True, suggested=SUGGEST)

    assert [model.id for model in found] == list(LIVE)
    assert {model.source for model in found} == {ModelSource.PROVIDER}
    assert all(model.available for model in found)
    assert stub.calls == 1

    # A second registry reads the file the first one wrote: no second request.
    again = make_registry(tmp_path).discover("anthropic", build=lambda: stub, connected=True)

    assert [model.id for model in again] == list(LIVE)
    assert stub.calls == 1


def test_refresh_refetches_and_overwrites_the_cache(tmp_path):
    stub = StubProvider()
    registry = make_registry(tmp_path)
    registry.discover("anthropic", build=lambda: stub, connected=True)

    stub.models = ("claude-sonnet-4-5",)  # the account lost access to opus
    refreshed = registry.discover("anthropic", build=lambda: stub, connected=True, refresh=True)

    assert stub.calls == 2
    assert [model.id for model in refreshed] == ["claude-sonnet-4-5"]
    assert cache_document(tmp_path)["providers"]["anthropic"]["models"] == ["claude-sonnet-4-5"]


def test_a_stale_cache_entry_is_refetched(tmp_path):
    write_json(
        tmp_path / "model-cache.json",
        {
            "version": CACHE_VERSION,
            "providers": {
                "anthropic": {
                    "fetched_at": time.time() - 7200,
                    "models": ["claude-haiku-4-5"],
                }
            },
        },
    )
    stub = StubProvider()

    found = make_registry(tmp_path, ttl_seconds=3600).discover(
        "anthropic", build=lambda: stub, connected=True
    )

    assert stub.calls == 1
    assert [model.id for model in found] == list(LIVE)


def test_a_disconnected_provider_is_never_asked(tmp_path):
    stub = StubProvider()

    found = make_registry(tmp_path).discover(
        "openai", build=lambda: stub, connected=False, suggested=("gpt-5", "gpt-4.1")
    )

    assert stub.calls == 0
    assert [model.id for model in found] == ["gpt-5", "gpt-4.1"]
    assert {model.source for model in found} == {ModelSource.SUGGESTED}
    assert not any(model.available for model in found)


@pytest.mark.parametrize(
    "error",
    [ProviderError("credential rejected"), TransportError("name resolution failed")],
)
def test_a_failed_catalogue_falls_back_to_suggestions(tmp_path, error):
    stub = StubProvider(error=error)

    found = make_registry(tmp_path).discover(
        "anthropic", build=lambda: stub, connected=True, suggested=SUGGEST
    )

    assert stub.calls == 1
    assert [model.id for model in found] == list(SUGGEST)
    assert {model.source for model in found} == {ModelSource.SUGGESTED}


def test_a_failed_catalogue_prefers_a_stale_cache_over_suggestions(tmp_path):
    write_json(
        tmp_path / "model-cache.json",
        {
            "version": CACHE_VERSION,
            "providers": {
                "anthropic": {"fetched_at": time.time() - 7200, "models": list(LIVE)}
            },
        },
    )
    stub = StubProvider(error=ProviderError("rate limited"))

    found = make_registry(tmp_path, ttl_seconds=60).discover(
        "anthropic", build=lambda: stub, connected=True, suggested=SUGGEST
    )

    assert stub.calls == 1
    assert [model.id for model in found] == list(LIVE)
    assert {model.source for model in found} == {ModelSource.PROVIDER}


@pytest.mark.parametrize(
    "content",
    [
        "{not json at all",
        "[]",
        '{"version": 99, "providers": {"anthropic": {"models": ["claude-haiku-4-5"]}}}',
        '{"version": 1, "providers": {"anthropic": {"models": ["claude-haiku-4-5"]}}}',
        '{"version": 1, "providers": "anthropic"}',
    ],
)
def test_an_unusable_cache_file_is_ignored(tmp_path, content):
    cache = tmp_path / "model-cache.json"
    cache.write_text(content, encoding="utf-8")
    stub = StubProvider()

    found = ModelRegistry(cache_file=cache).discover(
        "anthropic", build=lambda: stub, connected=True, suggested=SUGGEST
    )

    assert stub.calls == 1
    assert [model.id for model in found] == list(LIVE)
    assert cache_document(tmp_path)["providers"]["anthropic"]["models"] == list(LIVE)


def test_invalidate_drops_one_provider_then_all_of_them(tmp_path):
    anthropic, openai = StubProvider(), StubProvider(("gpt-5",))
    registry = make_registry(tmp_path)
    registry.discover("anthropic", build=lambda: anthropic, connected=True)
    registry.discover("openai", build=lambda: openai, connected=True)

    registry.invalidate("anthropic")
    registry.discover("anthropic", build=lambda: anthropic, connected=True)
    registry.discover("openai", build=lambda: openai, connected=True)

    assert (anthropic.calls, openai.calls) == (2, 1)

    registry.invalidate()
    registry.discover("openai", build=lambda: openai, connected=True)

    assert openai.calls == 2


# --- grouping --------------------------------------------------------------
def test_grouped_orders_marks_and_annotates(tmp_path):
    stubs = {"anthropic": StubProvider()}

    groups = make_registry(tmp_path).grouped(
        active_provider="openai",
        active_model="gpt-4.1",
        providers=[
            ("anthropic", "Anthropic", True, SUGGEST),
            ("openai", "OpenAI", False, ("gpt-5",)),
        ],
        # A KeyError here would prove the disconnected provider was built.
        build=lambda name: stubs[name],
    )

    assert [group.provider for group in groups] == ["openai", "anthropic"]
    active, other = groups
    assert active.label == "OpenAI"
    assert not active.connected
    assert active.note == "not connected - /login openai"
    assert other.connected
    assert other.note is None
    # The configured model stays visible although the account never offered it.
    assert active.models[0] == ModelInfo(
        id="gpt-4.1", provider="openai", source=ModelSource.CONFIG, available=False
    )
    assert [model.id for model in active.models] == ["gpt-4.1", "gpt-5"]
    assert [model.id for model in other.models] == list(LIVE)
    assert stubs["anthropic"].calls == 1


def test_grouped_lifts_an_offered_active_model_to_the_front(tmp_path):
    groups = make_registry(tmp_path).grouped(
        active_provider="anthropic",
        active_model="claude-opus-4-1",
        providers=[("anthropic", "Anthropic", True, LIVE)],
    )

    models = groups[0].models
    assert [model.id for model in models] == ["claude-opus-4-1", "claude-sonnet-4-5"]
    # Promoted, not duplicated, and still honestly labelled a suggestion.
    assert models[0].source is ModelSource.SUGGESTED


def test_grouped_keeps_the_given_order_without_an_active_provider(tmp_path):
    groups = make_registry(tmp_path).grouped(
        active_provider=None,
        active_model=None,
        providers=[
            ("anthropic", "Anthropic", False, SUGGEST),
            ("openai", "OpenAI", False, ("gpt-5",)),
        ],
    )

    assert [group.provider for group in groups] == ["anthropic", "openai"]


# --- resolving -------------------------------------------------------------
def test_resolve_matches_exactly_then_loosens():
    models = as_models(CATALOGUE)

    assert ModelRegistry.resolve("claude-opus-4-1", models).id == "claude-opus-4-1"
    assert ModelRegistry.resolve("CLAUDE-Opus-4-1", models).id == "claude-opus-4-1"
    assert ModelRegistry.resolve("claude-o", models).id == "claude-opus-4-1"
    assert ModelRegistry.resolve("opus", models).id == "claude-opus-4-1"


def test_resolve_refuses_an_ambiguous_or_unknown_query():
    models = as_models(CATALOGUE)

    # Two ids share this prefix; picking one would silently reroute every call.
    assert ModelRegistry.resolve("claude-3-5-sonnet", models) is None
    assert ModelRegistry.resolve("2024", models) is None
    assert ModelRegistry.resolve("gpt-5", models) is None
    assert ModelRegistry.resolve("  ", models) is None
    assert ModelRegistry.resolve("claude-opus-4-1", ()) is None


# --- searching -------------------------------------------------------------
def test_search_matches_characters_in_order():
    models = as_models(CATALOGUE)

    assert [model.id for model in ModelRegistry.search("opus", models)] == ["claude-opus-4-1"]
    assert [model.id for model in ModelRegistry.search("clop", models)] == ["claude-opus-4-1"]
    assert ModelRegistry.search("zzz", models) == []
    assert ModelRegistry.search("", models) == list(models)


def test_search_puts_the_best_match_first():
    models = as_models(("claude-3-5-sonnet-20241022", "claude-sonnet-4-5"))

    found = ModelRegistry.search("sonnet", models)

    # Earlier hit first, whatever order the catalogue arrived in.
    assert [model.id for model in found] == ["claude-sonnet-4-5", "claude-3-5-sonnet-20241022"]


def test_search_prefers_a_prefix_over_a_scattered_match():
    models = as_models(("claude-opus-4-1", "claude-sonnet-4-5"))

    found = ModelRegistry.search("claude-s", models)

    assert [model.id for model in found] == ["claude-sonnet-4-5", "claude-opus-4-1"]


def test_search_keeps_the_given_order_for_equally_good_matches():
    models = as_models(("claude-opus-4-1", "claude-sonnet-4-5"))

    found = ModelRegistry.search("claude", models)

    assert [model.id for model in found] == ["claude-opus-4-1", "claude-sonnet-4-5"]


def test_search_and_display_use_the_label_when_there_is_one():
    labelled = ModelInfo(
        id="claude-opus-4-1",
        provider="anthropic",
        source=ModelSource.PROVIDER,
        label="Claude Opus 4.1",
    )

    assert ModelRegistry.search("Opus", (labelled,)) == [labelled]
    assert labelled.display == "Claude Opus 4.1"
    assert as_models(("gpt-5",), provider="openai")[0].display == "gpt-5"


# --- what the cache is allowed to hold -------------------------------------
def test_the_cache_holds_model_ids_and_nothing_else(tmp_path):
    stub = StubProvider()
    make_registry(tmp_path).discover("anthropic", build=lambda: stub, connected=True)

    raw = (tmp_path / "model-cache.json").read_text(encoding="utf-8")
    assert SECRET not in raw
    for word in ("key", "token", "credential", "secret", "authorization", "account", "email"):
        assert word not in raw.casefold()

    document = json.loads(raw)
    assert set(document) == {"version", "providers"}
    entry = document["providers"]["anthropic"]
    assert set(entry) == {"fetched_at", "models"}
    assert entry["models"] == list(LIVE)
    assert isinstance(entry["fetched_at"], float)
