"""Anthropic Claude, via the official Messages API.

Two shape differences from the OpenAI-style APIs, both handled here rather than
leaking into callers: the system prompt is a top-level field (not a turn), and
``max_tokens`` is required on every request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from .provider import AIProvider
from .types import Completion, Message


class AnthropicProvider(AIProvider):
    """Claude models on ``api.anthropic.com``."""

    name: ClassVar[str] = "anthropic"
    label: ClassVar[str] = "Anthropic Claude"
    env_var: ClassVar[str | None] = "ANTHROPIC_API_KEY"
    default_model: ClassVar[str] = "claude-sonnet-4-5"
    default_base_url: ClassVar[str] = "https://api.anthropic.com"
    suggested_models: ClassVar[tuple[str, ...]] = (
        "claude-sonnet-4-5",
        "claude-opus-4-1",
        "claude-haiku-4-5",
    )
    docs_url: ClassVar[str] = "https://console.anthropic.com/settings/keys"
    completion_path: ClassVar[str] = "/v1/messages"
    models_path: ClassVar[str] = "/v1/models"
    #: Pinned: the API version is part of the request contract, not a preference.
    api_version: ClassVar[str] = "2023-06-01"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._credential,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
            "accept": "application/json",
        }

    def _payload(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None,
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        system_text, chat = self._split_system(messages, system)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "temperature": temperature,
            "messages": [message.as_dict() for message in chat],
        }
        if system_text:
            payload["system"] = system_text
        return payload

    def _parse_completion(
        self, body: Mapping[str, Any], *, model: str, task: str | None
    ) -> Completion:
        blocks = body.get("content")
        chunks: list[str] = []
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, Mapping) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        reported = body.get("model")
        stop = body.get("stop_reason")
        return Completion(
            text="".join(chunks),
            provider=self.name,
            model=reported if isinstance(reported, str) and reported else model,
            usage=self._usage(body, input_key="input_tokens", output_key="output_tokens"),
            stop_reason=stop if isinstance(stop, str) else None,
            task=task,
        )

    def list_models(self) -> tuple[str, ...]:
        return self._model_list(container="data", key="id")
