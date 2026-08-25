"""Gemini request shape, response parsing and catalogue filtering.

Google's API differs from every other provider here in ways that are easy to get
wrong silently - the model lives in the URL, the assistant role is called
``model``, system text is not a turn, and a blocked prompt comes back as an
empty ``candidates`` list rather than an error status. Each of those has a test,
driven through the fake transport so nothing reaches a real account.
"""

from __future__ import annotations

import pytest

from rn_agent.ai.google import GoogleProvider
from rn_agent.ai.types import Message
from rn_agent.errors import ProviderError

KEY = "AIzaTestKey0123456789abcdef"
TOKEN = "ya29.test-access-token-0123456789"

GEMINI_OK = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "hello"}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7, "totalTokenCount": 18},
    "modelVersion": "gemini-2.5-flash",
}


def google(transport, **kwargs):
    return GoogleProvider(credential=KEY, transport=transport, **kwargs)


# --- the model is in the URL ------------------------------------------------
def test_the_model_becomes_a_url_segment(transport):
    transport.queue(body=GEMINI_OK)
    google(transport).complete([Message.user("hi")])
    assert transport.last["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    )


def test_an_already_prefixed_model_is_not_doubled(transport):
    transport.queue(body=GEMINI_OK)
    google(transport).complete([Message.user("hi")], model="models/gemini-2.5-pro")
    url = transport.last["url"]
    assert url.endswith("/v1beta/models/gemini-2.5-pro:generateContent")
    assert "models/models/" not in url


# --- two credential shapes, one provider -----------------------------------
def test_an_api_key_travels_in_the_google_key_header(transport):
    transport.queue(body=GEMINI_OK)
    google(transport).complete([Message.user("hi")])
    headers = transport.last["headers"]
    assert headers["x-goog-api-key"] == KEY
    assert "authorization" not in headers


def test_an_oauth_token_travels_as_a_bearer_instead(transport):
    transport.queue(body=GEMINI_OK)
    GoogleProvider(credential=TOKEN, transport=transport, oauth=True).complete([Message.user("hi")])
    headers = transport.last["headers"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert "x-goog-api-key" not in headers


def test_the_quota_project_is_sent_only_when_it_is_known(transport):
    transport.queue(body=GEMINI_OK).queue(body=GEMINI_OK)
    google(transport, oauth=True, quota_project="rn-agent-dev").complete([Message.user("hi")])
    assert transport.last["headers"]["x-goog-user-project"] == "rn-agent-dev"

    google(transport).complete([Message.user("hi")])
    assert "x-goog-user-project" not in transport.last["headers"]


# --- request body ----------------------------------------------------------
def test_system_text_is_hoisted_out_of_the_conversation(transport):
    transport.queue(body=GEMINI_OK)
    google(transport).complete(
        [Message.system("be terse"), Message.user("hi")], system="you are a linter"
    )
    payload = transport.last["payload"]
    assert payload["systemInstruction"] == {"parts": [{"text": "you are a linter\n\nbe terse"}]}
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_without_system_text_there_is_no_system_instruction(transport):
    transport.queue(body=GEMINI_OK)
    google(transport).complete([Message.user("hi")])
    assert "systemInstruction" not in transport.last["payload"]


def test_the_assistant_role_is_renamed_model(transport):
    transport.queue(body=GEMINI_OK)
    google(transport).complete(
        [Message.user("first"), Message.assistant("second"), Message.user("third")]
    )
    assert transport.last["payload"]["contents"] == [
        {"role": "user", "parts": [{"text": "first"}]},
        {"role": "model", "parts": [{"text": "second"}]},
        {"role": "user", "parts": [{"text": "third"}]},
    ]


def test_generation_settings_go_in_generation_config(transport):
    transport.queue(body=GEMINI_OK)
    google(transport, max_output_tokens=256, temperature=0.4).complete([Message.user("hi")])
    payload = transport.last["payload"]
    assert payload["generationConfig"] == {"temperature": 0.4, "maxOutputTokens": 256}
    assert "temperature" not in payload
    assert "max_output_tokens" not in payload


# --- response parsing ------------------------------------------------------
def test_a_normal_reply_carries_text_and_token_counts(transport):
    transport.queue(body=GEMINI_OK)
    completion = google(transport).complete([Message.user("hi")], task="review")
    assert completion.text == "hello"
    assert completion.provider == "google"
    assert completion.model == "gemini-2.5-flash"
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (11, 7)
    assert completion.stop_reason == "stop"
    assert not completion.truncated
    assert completion.task == "review"


def test_multi_part_text_is_concatenated_and_non_text_parts_ignored(transport):
    transport.queue(
        body={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "diff --git"},
                            {"functionCall": {"name": "apply"}},
                            {"text": " a/App.tsx"},
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    completion = google(transport).complete([Message.user("hi")])
    assert completion.text == "diff --git a/App.tsx"


def test_hitting_the_output_limit_is_reported_as_truncated(transport):
    transport.queue(
        body={
            "candidates": [
                {"content": {"parts": [{"text": "half an ans"}]}, "finishReason": "MAX_TOKENS"}
            ],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3},
        }
    )
    completion = google(transport).complete([Message.user("hi")])
    assert completion.stop_reason == "max_tokens"
    assert completion.truncated


def test_a_missing_usage_block_reports_zero_rather_than_crashing(transport):
    transport.queue(body={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    completion = google(transport).complete([Message.user("hi")])
    assert completion.usage.total_tokens == 0
    assert completion.stop_reason is None
    # No `modelVersion` echoed, so the requested name stands.
    assert completion.model == "gemini-2.5-flash"


def test_a_blocked_prompt_is_an_error_naming_the_reason(transport):
    transport.queue(body={"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(ProviderError) as failure:
        google(transport).complete([Message.user("hi")])
    assert "SAFETY" in failure.value.message
    assert "blocked" in failure.value.message


def test_no_candidates_and_no_reason_is_still_an_error(transport):
    transport.queue(body={"candidates": []})
    with pytest.raises(ProviderError) as failure:
        google(transport).complete([Message.user("hi")])
    assert "no candidate" in failure.value.message


# --- the catalogue ---------------------------------------------------------
def test_list_models_keeps_what_can_generate_and_strips_the_prefix(transport):
    transport.queue(
        body={
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {
                    "name": "models/text-embedding-004",
                    "supportedGenerationMethods": ["embedContent"],
                },
                {
                    "name": "models/gemini-2.5-pro",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {"name": "models/no-methods-listed"},
            ]
        }
    )
    assert google(transport).list_models() == ("gemini-2.5-flash", "gemini-2.5-pro")
    assert transport.last["url"].endswith("/v1beta/models")
    assert transport.last["method"] == "GET"


def test_an_unparseable_catalogue_is_empty_not_a_crash(transport):
    transport.queue(body={"nope": "not a catalogue"})
    assert google(transport).list_models() == ()


def test_verify_reports_the_account_catalogue(transport):
    transport.queue(
        body={
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "supportedGenerationMethods": ["generateContent"],
                }
            ]
        }
    )
    identity = google(transport).verify()
    assert identity.ok
    assert identity.models == ("gemini-2.5-flash",)


# --- inherited guarantees --------------------------------------------------
def test_no_credential_no_provider(transport):
    with pytest.raises(ProviderError) as failure:
        GoogleProvider(transport=transport)
    assert "google" in failure.value.message
    assert "GEMINI_API_KEY" in (failure.value.hint or "")
    assert transport.calls == []


def test_a_rejected_credential_says_to_sign_in_again(transport):
    transport.queue(status=401, body={"error": {"message": "API key not valid"}})
    with pytest.raises(ProviderError) as failure:
        google(transport).complete([Message.user("hi")])
    assert "401" in failure.value.message
    assert "login google" in (failure.value.hint or "")


def test_the_credential_is_never_shown_in_full(transport):
    provider = google(transport)
    assert provider.masked_credential == "…cdef"
    assert KEY not in repr(provider)
