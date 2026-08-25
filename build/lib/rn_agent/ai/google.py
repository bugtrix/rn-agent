"""Google Gemini, via the Generative Language API.

Three shape differences from the OpenAI-style APIs, absorbed here rather than
leaked to callers: the model is a URL segment
(``/v1beta/models/<model>:generateContent``) instead of a body field, the
assistant role is called ``model`` and system text is a top-level
``systemInstruction``, and token counts arrive under ``usageMetadata``.

Google publishes an OAuth 2.0 flow for this API (ai.google.dev/gemini-api/docs/oauth)
as well as API keys, and the two credentials travel in different headers. The
``oauth`` flag picks the header and nothing else: it never inspects or rewrites
the credential, so a mislabelled one comes back as a clean 401 from Google
rather than as a request that half works.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ..errors import ProviderError
from .provider import AIProvider
from .types import Completion, Message, Usage

#: Gemini resource names are ``models/<id>``; developers type either form.
MODEL_PREFIX = "models/"
#: The one generation method this provider speaks.
GENERATE_CONTENT = "generateContent"


def bare_model_id(name: str) -> str:
    """``models/gemini-2.5-flash`` and ``gemini-2.5-flash`` name one model."""
    identifier = name.strip().lstrip("/")
    while identifier.startswith(MODEL_PREFIX):
        identifier = identifier.removeprefix(MODEL_PREFIX)
    return identifier


class GoogleProvider(AIProvider):
    """Gemini models on ``generativelanguage.googleapis.com``."""

    name: ClassVar[str] = "google"
    label: ClassVar[str] = "Google Gemini"
    env_var: ClassVar[str | None] = "GEMINI_API_KEY"
    requires_credential: ClassVar[bool] = True
    default_model: ClassVar[str] = "gemini-2.5-flash"
    default_base_url: ClassVar[str] = "https://generativelanguage.googleapis.com"
    #: Empty on purpose. This provider discovers its catalogue from the account
    #: (`list_models`); a bundled list would be a guess that goes stale the next
    #: time Google ships or retires a model.
    suggested_models: ClassVar[tuple[str, ...]] = ()
    docs_url: ClassVar[str] = "https://aistudio.google.com/apikey"
    #: Unused: the model is part of the URL, so `_completion_path` builds it.
    completion_path: ClassVar[str] = ""
    models_path: ClassVar[str] = "/v1beta/models"

    def __init__(
        self,
        *,
        oauth: bool = False,
        quota_project: str | None = None,
        **extra: Any,
    ) -> None:
        """``oauth`` chooses the auth header; ``quota_project`` names the payer.

        The base keywords (``credential``, ``model``, ``transport``, ...) are
        forwarded rather than restated, so their defaults keep living in one
        place - including the refusal to exist without a credential.
        """
        super().__init__(**extra)
        self.oauth = oauth
        self.quota_project = quota_project

    def _completion_path(self, model: str) -> str:
        return f"{self.models_path}/{bare_model_id(model)}:{GENERATE_CONTENT}"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.oauth:
            headers["authorization"] = f"Bearer {self._credential}"
        else:
            headers["x-goog-api-key"] = self._credential
        if self.quota_project:
            # An OAuth token can reach several Cloud projects; this says which
            # one is charged for the call.
            headers["x-goog-user-project"] = self.quota_project
        return headers

    def _payload(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None,
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        # The model is in the URL, not the body.
        _ = model
        system_text, chat = self._split_system(messages, system)
        payload: dict[str, Any] = {
            "contents": [
                {
                    # Gemini calls the assistant "model".
                    "role": "model" if message.role == "assistant" else "user",
                    "parts": [{"text": message.content}],
                }
                for message in chat
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
            },
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        return payload

    def _parse_completion(
        self, body: Mapping[str, Any], *, model: str, task: str | None
    ) -> Completion:
        candidate = self._first_candidate(body)
        chunks: list[str] = []
        content = candidate.get("content")
        if isinstance(content, Mapping):
            parts = content.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    # Function calls and inline data are parts too; only text
                    # is an answer.
                    if isinstance(part, Mapping):
                        text = part.get("text")
                        if isinstance(text, str):
                            chunks.append(text)
        reason = candidate.get("finishReason")
        reported = body.get("modelVersion")
        return Completion(
            text="".join(chunks),
            provider=self.name,
            model=reported if isinstance(reported, str) and reported else model,
            usage=self._token_usage(body),
            # `MAX_TOKENS` -> `max_tokens`, which `Completion.truncated` reads.
            stop_reason=reason.lower() if isinstance(reason, str) and reason else None,
            task=task,
        )

    def _first_candidate(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """The candidate to read, or why there is none.

        Google returns no candidates only when something was wrong with the
        prompt, and says what in ``promptFeedback.blockReason``. Parsing that
        into an empty string would hand the caller silence and call it a reply.
        """
        candidates = body.get("candidates")
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, Mapping):
                return first
        feedback = body.get("promptFeedback")
        raw = feedback.get("blockReason") if isinstance(feedback, Mapping) else None
        blocked = raw if isinstance(raw, str) and raw else None
        if blocked:
            raise ProviderError(
                f"{self.name}: the prompt was blocked ({blocked})",
                hint=(
                    "Gemini's safety filters rejected this request. Rephrase it, or "
                    "narrow the code sent as context."
                ),
            )
        raise ProviderError(
            f"{self.name}: the response carried no candidate to read",
            hint="Retry the request; if it persists, try another model.",
        )

    def _token_usage(self, body: Mapping[str, Any]) -> Usage:
        """Counts sit under ``usageMetadata``, so ``_usage`` is given that object."""
        metadata = body.get("usageMetadata")
        return self._usage(
            metadata if isinstance(metadata, Mapping) else {},
            input_key="promptTokenCount",
            output_key="candidatesTokenCount",
        )

    def list_models(self) -> tuple[str, ...]:
        """Models this account may call ``generateContent`` on, in Google's order.

        The catalogue also carries embedding and legacy entries; offering those
        in a model picker would offer a call that cannot succeed. Only the first
        page is read - Google returns 50 models per page unless asked otherwise.
        """
        body = self._request("GET", self.models_path).body
        entries = body.get("models")
        if not isinstance(entries, list):
            return ()
        names: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            methods = entry.get("supportedGenerationMethods")
            if not isinstance(methods, list) or GENERATE_CONTENT not in methods:
                continue
            name = entry.get("name")
            if isinstance(name, str) and name:
                identifier = bare_model_id(name)
                if identifier:
                    names.append(identifier)
        return tuple(names)
