"""Ollama: models running on the developer's own machine.

No credential, no network egress, no cost - which makes it the honest default
for anyone who cannot send source code to a third party. ``OLLAMA_HOST`` is
respected, including the bare ``host:port`` form the Ollama CLI accepts.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, ClassVar

from .provider import AIProvider
from .types import Completion, Message

ENV_OLLAMA_HOST = "OLLAMA_HOST"


class OllamaProvider(AIProvider):
    """A local (or self-hosted) Ollama server."""

    name: ClassVar[str] = "ollama"
    label: ClassVar[str] = "Ollama (local)"
    env_var: ClassVar[str | None] = None
    requires_credential: ClassVar[bool] = False
    default_model: ClassVar[str] = "llama3.1"
    default_base_url: ClassVar[str] = "http://127.0.0.1:11434"
    suggested_models: ClassVar[tuple[str, ...]] = ("llama3.1", "qwen2.5-coder", "deepseek-r1")
    docs_url: ClassVar[str] = "https://ollama.com/download"
    completion_path: ClassVar[str] = "/api/chat"
    models_path: ClassVar[str] = "/api/tags"
    unreachable_hint: ClassVar[str | None] = (
        "Start the server with `ollama serve`, or point ai.base_url at the machine running it."
    )

    @classmethod
    def resolve_base_url(cls, base_url: str | None) -> str:
        """Explicit config, then ``OLLAMA_HOST``, then localhost."""
        raw = base_url or os.environ.get(ENV_OLLAMA_HOST) or cls.default_base_url
        candidate = raw.strip().rstrip("/")
        if not candidate:
            return cls.default_base_url
        if "://" not in candidate:
            candidate = f"http://{candidate}"
        return candidate

    def _headers(self) -> dict[str, str]:
        return {"content-type": "application/json", "accept": "application/json"}

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
        return {
            "model": model,
            "messages": turns,
            # The agent needs whole answers, not a token stream.
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_output_tokens},
        }

    def _parse_completion(
        self, body: Mapping[str, Any], *, model: str, task: str | None
    ) -> Completion:
        text = ""
        message = body.get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                text = content
        reported = body.get("model")
        stop = body.get("done_reason")
        return Completion(
            text=text,
            provider=self.name,
            model=reported if isinstance(reported, str) and reported else model,
            # Ollama reports counts at the top level, not under `usage`.
            usage=self._usage(body, input_key="prompt_eval_count", output_key="eval_count"),
            stop_reason=stop if isinstance(stop, str) else None,
            task=task,
        )

    def list_models(self) -> tuple[str, ...]:
        return self._model_list(container="models", key="name")
