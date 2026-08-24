"""OpenAI, via the Chat Completions API.

One real-world wrinkle is handled here: reasoning models (o-series, GPT-5)
reject ``max_tokens`` and a non-default ``temperature``, while the older chat
models reject ``max_completion_tokens``. The model name decides which fields go
on the wire, so a wrong pairing never reaches the API.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from .provider import AIProvider
from .types import Completion, Message


class OpenAIProvider(AIProvider):
    """GPT and o-series models on ``api.openai.com`` (or any compatible host)."""

    name: ClassVar[str] = "openai"
    label: ClassVar[str] = "OpenAI"
    env_var: ClassVar[str | None] = "OPENAI_API_KEY"
    default_model: ClassVar[str] = "gpt-5"
    default_base_url: ClassVar[str] = "https://api.openai.com"
    suggested_models: ClassVar[tuple[str, ...]] = ("gpt-5", "gpt-5-mini", "gpt-4.1", "o4-mini")
    docs_url: ClassVar[str] = "https://platform.openai.com/api-keys"
    completion_path: ClassVar[str] = "/v1/chat/completions"
    models_path: ClassVar[str] = "/v1/models"
    #: Model families that use `max_completion_tokens` and a fixed temperature.
    reasoning_prefixes: ClassVar[tuple[str, ...]] = ("o1", "o3", "o4", "gpt-5")

    @classmethod
    def is_reasoning_model(cls, model: str) -> bool:
        return model.startswith(cls.reasoning_prefixes)

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._credential}",
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
        turns = [message.as_dict() for message in chat]
        if system_text:
            turns.insert(0, {"role": "system", "content": system_text})
        payload: dict[str, Any] = {"model": model, "messages": turns}
        if self.is_reasoning_model(model):
            payload["max_completion_tokens"] = max_output_tokens
        else:
            payload["max_tokens"] = max_output_tokens
            payload["temperature"] = temperature
        return payload

    def _parse_completion(
        self, body: Mapping[str, Any], *, model: str, task: str | None
    ) -> Completion:
        text = ""
        stop: str | None = None
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, Mapping):
                message = first.get("message")
                if isinstance(message, Mapping):
                    content = message.get("content")
                    if isinstance(content, str):
                        text = content
                reason = first.get("finish_reason")
                stop = reason if isinstance(reason, str) else None
        reported = body.get("model")
        return Completion(
            text=text,
            provider=self.name,
            model=reported if isinstance(reported, str) and reported else model,
            usage=self._usage(body, input_key="prompt_tokens", output_key="completion_tokens"),
            stop_reason=stop,
            task=task,
        )

    def list_models(self) -> tuple[str, ...]:
        return self._model_list(container="data", key="id")
