"""Claude on Vertex AI: the browser-login path to Claude, without an Anthropic key.

Two things are worth defending here. The request shape is Google's, not
Anthropic's - the model goes in the URL and ``anthropic_version`` goes in the
body - and the honesty rules: no invented model catalogue, no pretend
verification, and a refusal that names the missing Cloud project rather than
sending a request that would fail or, worse, bill someone.
"""

from __future__ import annotations

import pytest

from rn_agent.ai.registry import build_provider, canonical_name, resolve_spec
from rn_agent.ai.types import Message
from rn_agent.ai.vertex import (
    ANTHROPIC_VERSION,
    GLOBAL_HOST,
    VertexAnthropicProvider,
    regional_host,
)
from rn_agent.errors import ProviderError
from rn_agent.models.config import AIConfig

TOKEN = "ya29.a0AfB_token"
MODEL = "claude-sonnet-4-5@20250929"

VERTEX_OK = {
    "content": [{"type": "text", "text": "from vertex"}],
    "usage": {"input_tokens": 12, "output_tokens": 5},
    "stop_reason": "end_turn",
}


def vertex(transport, **extra) -> VertexAnthropicProvider:
    options = {"project": "my-project", "model": MODEL}
    options.update(extra)
    return VertexAnthropicProvider(credential=TOKEN, transport=transport, **options)


# --- the request Google expects --------------------------------------------
def test_the_model_is_in_the_url_and_never_in_the_body(transport):
    transport.queue(body=VERTEX_OK)

    vertex(transport).complete([Message.user("hi")])

    call = transport.last
    assert call["url"] == (
        f"{GLOBAL_HOST}/v1/projects/my-project/locations/global"
        f"/publishers/anthropic/models/{MODEL}:rawPredict"
    )
    # Sending `model` in the body is an error on rawPredict, not a redundancy.
    assert "model" not in call["payload"]
    assert call["payload"]["anthropic_version"] == ANTHROPIC_VERSION


def test_the_google_token_is_a_bearer_not_an_api_key(transport):
    transport.queue(body=VERTEX_OK)

    vertex(transport).complete([Message.user("hi")])

    headers = transport.last["headers"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    # An Anthropic-style key header would be silently ignored by Vertex.
    assert "x-api-key" not in headers
    assert "anthropic-version" not in headers


def test_a_region_moves_the_host_and_the_location(transport):
    transport.queue(body=VERTEX_OK)

    vertex(transport, region="us-east5").complete([Message.user("hi")])

    url = transport.last["url"]
    assert url.startswith("https://us-east5-aiplatform.googleapis.com/")
    assert "/locations/us-east5/" in url


def test_global_is_the_multi_region_host():
    assert regional_host("global") == GLOBAL_HOST
    assert regional_host("") == GLOBAL_HOST
    assert regional_host("europe-west1") == "https://europe-west1-aiplatform.googleapis.com"


def test_the_system_prompt_is_split_out_like_the_messages_api(transport):
    transport.queue(body=VERTEX_OK)

    vertex(transport).complete(
        [Message.system("be brief"), Message.user("hi")], system="obey rules"
    )

    payload = transport.last["payload"]
    assert payload["system"] == "obey rules\n\nbe brief"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_the_response_parses_exactly_like_anthropic(transport):
    transport.queue(body=VERTEX_OK)

    completion = vertex(transport).complete([Message.user("hi")], task="review")

    assert completion.text == "from vertex"
    assert completion.provider == "vertex"
    assert completion.usage.input_tokens == 12
    assert completion.usage.output_tokens == 5
    assert completion.stop_reason == "end_turn"
    assert completion.task == "review"


# --- refusing rather than guessing -----------------------------------------
def test_without_a_cloud_project_it_refuses_before_sending(transport):
    provider = VertexAnthropicProvider(credential=TOKEN, transport=transport, model=MODEL)

    with pytest.raises(ProviderError) as failure:
        provider.complete([Message.user("hi")])

    assert "project" in str(failure.value)
    # The hint names the command, and says who pays.
    assert "--cloud-project" in (failure.value.hint or "")
    assert transport.calls == []


def test_without_a_model_it_says_the_ids_carry_a_date(transport):
    provider = VertexAnthropicProvider(credential=TOKEN, transport=transport, project="p")

    with pytest.raises(ProviderError) as failure:
        provider.complete([Message.user("hi")])

    assert "@" in (failure.value.hint or "")  # claude-sonnet-4-5@20250929
    assert transport.calls == []


def test_no_model_catalogue_is_invented(transport):
    """Entitlements live in Model Garden, so there is nothing honest to list."""
    provider = vertex(transport)

    assert provider.list_models() == ()
    assert VertexAnthropicProvider.suggested_models == ()
    assert VertexAnthropicProvider.default_model == ""
    assert transport.calls == []


def test_verify_admits_it_did_not_call_the_api(transport):
    identity = vertex(transport).verify()

    assert identity.ok is True
    assert identity.models == ()
    # Every rawPredict call bills, so "verified" would be a lie.
    assert "not verified" in identity.detail
    assert "billable" in identity.detail
    assert transport.calls == []


def test_verify_still_refuses_without_a_project(transport):
    provider = VertexAnthropicProvider(credential=TOKEN, transport=transport, model=MODEL)

    with pytest.raises(ProviderError):
        provider.verify()


# --- registry wiring -------------------------------------------------------
def test_the_names_developers_type_reach_vertex():
    for alias in ("vertex", "claude-vertex", "vertex-claude", "vertex-anthropic"):
        assert canonical_name(alias) == "vertex"


def test_the_spec_declares_a_credential_but_no_env_var():
    spec = resolve_spec("vertex")

    assert spec.requires_credential is True
    # There is no VERTEX_API_KEY: the credential is a Google OAuth token, which
    # is the entire reason this provider exists.
    assert spec.env_var is None
    assert spec.provider_class is VertexAnthropicProvider


def test_config_project_and_region_reach_the_provider(transport):
    transport.queue(body=VERTEX_OK)
    config = AIConfig(provider="vertex", model=MODEL, project="billed-project", region="us-east5")

    provider = build_provider(
        config,
        credential=TOKEN,
        transport=transport,
        project=config.project,
        region=config.region,
    )
    provider.complete([Message.user("hi")])

    assert "/projects/billed-project/locations/us-east5/" in transport.last["url"]
