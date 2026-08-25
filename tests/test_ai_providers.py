"""Provider behaviour: request shape, response parsing, error mapping.

Every test drives a provider through a fake transport, so the assertions are
about the bytes we would put on the wire and what we make of the answer - never
about a live account.
"""

from __future__ import annotations

import pytest

from rn_agent.ai.anthropic import AnthropicProvider
from rn_agent.ai.ollama import OllamaProvider
from rn_agent.ai.openai import OpenAIProvider
from rn_agent.ai.registry import build_provider, canonical_name, provider_names, resolve_spec
from rn_agent.ai.types import Completion, Message, Usage
from rn_agent.errors import ProviderError, TransportError
from rn_agent.models.config import AIConfig

KEY = "sk-ant-test-0123456789abcdef"

ANTHROPIC_OK = {
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "hello"}, {"type": "thinking", "text": "ignored"}],
    "usage": {"input_tokens": 11, "output_tokens": 7},
    "stop_reason": "end_turn",
}
OPENAI_OK = {
    "model": "gpt-5",
    "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3},
}
OLLAMA_OK = {
    "model": "llama3.1",
    "message": {"role": "assistant", "content": "local reply"},
    "prompt_eval_count": 9,
    "eval_count": 4,
    "done_reason": "stop",
}


# --- values ----------------------------------------------------------------
def test_message_rejects_an_unknown_role():
    with pytest.raises(ValueError, match="unknown message role"):
        Message("tool", "payload")


def test_completion_knows_when_it_was_cut_off():
    assert Completion("x", "openai", "gpt-5", Usage(1, 2), stop_reason="length").truncated
    assert not Completion("x", "openai", "gpt-5", Usage(1, 2), stop_reason="stop").truncated
    assert Usage(4, 6).total_tokens == 10


# --- anthropic -------------------------------------------------------------
def test_anthropic_request_shape(transport):
    transport.queue(body=ANTHROPIC_OK)
    provider = AnthropicProvider(credential=KEY, transport=transport, max_output_tokens=256)

    provider.complete([Message.system("be brief"), Message.user("hi")], system="obey rules")

    call = transport.last
    assert call["method"] == "POST"
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == KEY
    assert call["headers"]["anthropic-version"] == AnthropicProvider.api_version
    payload = call["payload"]
    # Anthropic takes the system prompt apart from the turns, in prompt order.
    assert payload["system"] == "obey rules\n\nbe brief"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["max_tokens"] == 256


def test_anthropic_parses_text_usage_and_stop_reason(transport):
    transport.queue(body=ANTHROPIC_OK)
    provider = AnthropicProvider(credential=KEY, transport=transport)

    completion = provider.complete([Message.user("hi")], task="review")

    assert completion.text == "hello"  # non-text blocks are dropped
    assert completion.model == "claude-sonnet-4-5"
    assert completion.provider == "anthropic"
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (11, 7)
    assert completion.stop_reason == "end_turn"
    assert completion.task == "review"


def test_anthropic_verify_reports_the_account_catalogue(transport):
    transport.queue(body={"data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-opus-4-1"}]})
    provider = AnthropicProvider(credential=KEY, transport=transport)

    identity = provider.verify()

    assert transport.last["url"] == "https://api.anthropic.com/v1/models"
    assert identity.ok
    assert identity.models == ("claude-sonnet-4-5", "claude-opus-4-1")
    assert "2 model" in identity.detail


# --- openai ----------------------------------------------------------------
def test_openai_chat_model_uses_max_tokens_and_temperature(transport):
    transport.queue(body=OPENAI_OK)
    provider = OpenAIProvider(credential="sk-test-openai-key", transport=transport, temperature=0.4)

    provider.complete([Message.user("hi")], model="gpt-4.1")

    payload = transport.last["payload"]
    assert payload["max_tokens"] == 4096
    assert payload["temperature"] == 0.4
    assert "max_completion_tokens" not in payload
    assert transport.last["headers"]["authorization"] == "Bearer sk-test-openai-key"


def test_openai_reasoning_model_switches_the_token_field(transport):
    transport.queue(body=OPENAI_OK)
    provider = OpenAIProvider(credential="sk-test-openai-key", transport=transport, temperature=0.4)

    provider.complete([Message.user("hi")], model="o4-mini")

    payload = transport.last["payload"]
    # Reasoning models reject both `max_tokens` and a custom temperature.
    assert payload["max_completion_tokens"] == 4096
    assert "max_tokens" not in payload
    assert "temperature" not in payload


def test_openai_puts_the_system_prompt_first(transport):
    transport.queue(body=OPENAI_OK)
    provider = OpenAIProvider(credential="sk-test-openai-key", transport=transport)

    completion = provider.complete([Message.user("hi")], system="be terse")

    assert transport.last["payload"]["messages"][0] == {"role": "system", "content": "be terse"}
    assert completion.text == "hi"
    assert completion.usage.input_tokens == 5
    assert completion.stop_reason == "stop"


# --- ollama ----------------------------------------------------------------
def test_ollama_needs_no_credential_and_disables_streaming(transport):
    provider = OllamaProvider(transport=transport, max_output_tokens=512, temperature=0.2)
    transport.queue(body=OLLAMA_OK)

    completion = provider.complete([Message.user("hi")])

    payload = transport.last["payload"]
    assert transport.last["url"] == "http://127.0.0.1:11434/api/chat"
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": 0.2, "num_predict": 512}
    assert completion.text == "local reply"
    # Ollama reports token counts at the top level, not under `usage`.
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (9, 4)


def test_ollama_host_env_var_without_a_scheme(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "workstation:11434")
    assert OllamaProvider.resolve_base_url(None) == "http://workstation:11434"
    # Explicit config still wins over the environment.
    assert OllamaProvider.resolve_base_url("https://gpu.box/") == "https://gpu.box"


def test_ollama_unreachable_says_how_to_start_it(transport):
    transport.fail(TransportError("cannot reach http://127.0.0.1:11434/api/tags: refused"))
    provider = OllamaProvider(transport=transport)

    with pytest.raises(ProviderError) as failure:
        provider.list_models()

    assert "ollama serve" in (failure.value.hint or "")


# --- credentials and errors ------------------------------------------------
def test_provider_refuses_to_exist_without_a_credential():
    with pytest.raises(ProviderError) as failure:
        AnthropicProvider()

    assert "no credential" in failure.value.message
    assert "rn-agent login anthropic" in (failure.value.hint or "")
    assert "ANTHROPIC_API_KEY" in (failure.value.hint or "")


def test_the_key_is_never_shown_in_full(transport):
    provider = AnthropicProvider(credential=KEY, transport=transport)
    assert provider.masked_credential == "…cdef"
    assert KEY not in repr(provider)


@pytest.mark.parametrize(
    ("status", "expected_hint"),
    [
        (401, "rn-agent login"),
        (403, "plan"),
        (404, "rn-agent model --list"),
        (429, "Rate limit"),
        (503, "failing on its side"),
    ],
)
def test_http_failures_map_to_actionable_hints(transport, status, expected_hint):
    transport.queue(status=status, body={"error": {"message": "nope"}})
    provider = AnthropicProvider(credential=KEY, transport=transport)

    with pytest.raises(ProviderError) as failure:
        provider.complete([Message.user("hi")])

    assert failure.value.exit_code == 10
    assert f"HTTP {status}" in failure.value.message
    assert expected_hint in (failure.value.hint or "")


def test_a_provider_error_never_echoes_a_key(transport):
    leaked = "sk-ant-verysecretvalue0123456789"
    transport.queue(status=401, body={"error": {"message": f"invalid key {leaked}"}})
    provider = AnthropicProvider(credential=KEY, transport=transport)

    with pytest.raises(ProviderError) as failure:
        provider.complete([Message.user("hi")])

    assert leaked not in failure.value.message
    assert "[redacted]" in failure.value.message


def test_a_non_json_error_page_still_produces_an_error(transport):
    transport.queue(status=500, body={}, text="<html>gateway</html>")
    provider = OpenAIProvider(credential="sk-test-openai-key", transport=transport)

    with pytest.raises(ProviderError, match="HTTP 500"):
        provider.complete([Message.user("hi")])


def test_ollama_string_error_shape_is_understood(transport):
    transport.queue(status=404, body={"error": 'model "llama9" not found'})
    provider = OllamaProvider(transport=transport)

    with pytest.raises(ProviderError, match="llama9"):
        provider.complete([Message.user("hi")])


def test_an_empty_conversation_is_refused(transport):
    provider = OllamaProvider(transport=transport)
    with pytest.raises(ProviderError, match="empty conversation"):
        provider.complete([])
    assert transport.calls == []


# --- registry --------------------------------------------------------------
def test_aliases_developers_actually_type():
    assert canonical_name("claude") == "anthropic"
    assert resolve_spec("gpt").name == "openai"
    assert resolve_spec("Anthropic").name == "anthropic"
    assert resolve_spec("gemini").name == "google"
    assert set(provider_names()) == {"anthropic", "openai", "google", "vertex", "cursor", "ollama"}


def test_unknown_provider_lists_the_known_ones():
    with pytest.raises(ProviderError) as failure:
        resolve_spec("copilot")
    assert "anthropic" in (failure.value.hint or "")

    with pytest.raises(ProviderError) as missing:
        resolve_spec(None)
    assert "rn-agent login" in (missing.value.hint or "")


def test_build_provider_applies_config_and_task_models(transport):
    config = AIConfig(
        provider="anthropic",
        model="claude-sonnet-4-5",
        base_url="https://gateway.internal",
        max_output_tokens=64,
        timeout_seconds=9.5,
    )
    config.models.migration = "claude-opus-4-1"

    provider = build_provider(config, credential=KEY, task="migration", transport=transport)

    assert provider.model == "claude-opus-4-1"
    assert provider.base_url == "https://gateway.internal"
    assert provider.max_output_tokens == 64
    assert provider.timeout == 9.5

    transport.queue(body=ANTHROPIC_OK)
    provider.complete([Message.user("hi")])
    assert transport.last["url"] == "https://gateway.internal/v1/messages"
    assert transport.last["timeout"] == 9.5


def test_ollama_is_buildable_without_any_credential(transport):
    provider = build_provider(AIConfig(provider="ollama"), credential=None, transport=transport)
    assert provider.name == "ollama"
    assert provider.model == "llama3.1"
