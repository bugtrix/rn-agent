"""The provider contract.

One shape for every backend: build a request, send it, parse a
:class:`~rn_agent.ai.types.Completion`. Subclasses supply four small pieces -
headers, request payload, response parsing and the model catalogue - and inherit
credential handling, URL joining, error mapping and logging.

Two rules the base class enforces:

* **Your account, your key.** A provider that needs a credential refuses to be
  constructed without one, so no code path can silently fall back to a shared or
  anonymous endpoint.
* **Nothing sensitive is logged.** Only the model, HTTP status and token counts
  reach the log; every provider message is passed through ``redact()`` before it
  is raised or written.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from ..core.logging import get_logger
from ..errors import ProviderError
from ..utils.redaction import redact
from .http import DEFAULT_TIMEOUT, HttpResponse, JsonTransport, TransportError, default_transport
from .types import Completion, Message, Usage


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """The result of a live credential check."""

    provider: str
    ok: bool
    detail: str
    models: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "ok": self.ok,
            "detail": self.detail,
            "models": list(self.models),
        }


class AIProvider(ABC):
    """A model backend the agent can talk to."""

    #: Config value (`ai.provider`) and keychain account name.
    name: ClassVar[str] = "provider"
    label: ClassVar[str] = "AI provider"
    #: Environment variable holding a credential, checked before the keychain.
    env_var: ClassVar[str | None] = None
    requires_credential: ClassVar[bool] = True
    default_model: ClassVar[str] = ""
    default_base_url: ClassVar[str] = ""
    #: Bundled suggestions, not a catalogue: `rn-agent model --list` asks the API.
    suggested_models: ClassVar[tuple[str, ...]] = ()
    #: Where a developer gets a key.
    docs_url: ClassVar[str] = ""
    completion_path: ClassVar[str] = ""
    models_path: ClassVar[str] = ""
    #: Provider-specific advice when the host cannot be reached at all.
    unreachable_hint: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        credential: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
        transport: JsonTransport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if self.requires_credential and not credential:
            raise ProviderError(
                f"no credential available for {self.name}",
                hint=self.credential_hint(),
            )
        self._credential = credential or ""
        self.model = model or self.default_model
        self.base_url = self.resolve_base_url(base_url)
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._transport = transport or default_transport()
        self._logger = logger or get_logger("ai")

    # -- identity ----------------------------------------------------------
    @classmethod
    def credential_hint(cls) -> str:
        if not cls.requires_credential:
            return f"{cls.label} needs no credential."
        parts = [f"Run `rn-agent login {cls.name}`"]
        if cls.env_var:
            parts.append(f"or export {cls.env_var}")
        suffix = " ".join(parts)
        return f"{suffix}. Keys: {cls.docs_url}" if cls.docs_url else f"{suffix}."

    @classmethod
    def resolve_base_url(cls, base_url: str | None) -> str:
        return (base_url or cls.default_base_url).rstrip("/")

    @property
    def masked_credential(self) -> str | None:
        """Enough to recognise a key, never enough to use it."""
        if not self._credential:
            return None
        tail = self._credential[-4:]
        return f"…{tail}" if len(self._credential) > 8 else "set"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} model={self.model!r} base_url={self.base_url!r}>"

    # -- the contract ------------------------------------------------------
    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        system: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        task: str | None = None,
    ) -> Completion:
        """Send a conversation and return the model's reply."""
        if not messages:
            raise ProviderError("cannot send an empty conversation to a model")
        chosen = model or self.model
        if not chosen:
            raise ProviderError(
                f"no model selected for {self.name}",
                hint=f"Run `rn-agent model {self.default_model or '<name>'}`.",
            )
        payload = self._payload(
            list(messages),
            model=chosen,
            system=system,
            max_output_tokens=max_output_tokens or self.max_output_tokens,
            temperature=self.temperature if temperature is None else temperature,
        )
        response = self._request("POST", self.completion_path, payload=payload)
        completion = self._parse_completion(response.body, model=chosen, task=task)
        self._logger.info(
            "%s %s: %s in / %s out tokens",
            self.name,
            completion.model,
            completion.usage.input_tokens,
            completion.usage.output_tokens,
        )
        return completion

    def verify(self) -> ProviderIdentity:
        """Prove the credential works, using the cheapest real API call."""
        models = self.list_models()
        detail = f"{self.label} reachable"
        if models:
            detail = f"{detail}; {len(models)} model(s) available to this account"
        return ProviderIdentity(provider=self.name, ok=True, detail=detail, models=models)

    @abstractmethod
    def list_models(self) -> tuple[str, ...]:
        """Model ids this account may use, straight from the provider."""

    # -- subclass hooks ----------------------------------------------------
    @abstractmethod
    def _headers(self) -> dict[str, str]:
        """Authentication and content headers for one request."""

    @abstractmethod
    def _payload(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None,
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        """Provider-shaped request body."""

    @abstractmethod
    def _parse_completion(self, body: Mapping[str, Any], *, model: str, task: str | None) -> Completion:
        """Turn a provider response into a :class:`Completion`."""

    # -- plumbing ----------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"

    def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> HttpResponse:
        url = self._url(path)
        try:
            response = self._transport.request(
                method, url, headers=self._headers(), payload=payload, timeout=self.timeout
            )
        except TransportError as exc:
            if self.unreachable_hint:
                raise TransportError(exc.message, hint=self.unreachable_hint) from exc
            raise
        self._logger.debug("%s %s -> %s", method, url, response.status)
        if not response.ok:
            raise self._failure(response)
        return response

    def _failure(self, response: HttpResponse) -> ProviderError:
        """Map an HTTP failure onto an actionable error."""
        detail = redact(self._error_message(response)) or f"HTTP {response.status}"
        hints = {
            400: "The request was rejected as invalid; check the model name and ai.* config.",
            401: f"Credential rejected. Run `rn-agent login {self.name}` again with a fresh key.",
            403: f"Credential lacks access to this model. Check your {self.label} plan.",
            404: "Endpoint or model not found. Run `rn-agent model --list` to see what this account can use.",
            429: "Rate limit or quota reached. Wait and retry, or use a smaller model.",
        }
        hint = hints.get(response.status)
        if hint is None and response.status >= 500:
            hint = f"{self.label} is failing on its side; retry later."
        return ProviderError(f"{self.name}: {detail} (HTTP {response.status})", hint=hint)

    @staticmethod
    def _error_message(response: HttpResponse) -> str:
        """Dig the human-readable message out of the shapes providers use."""
        error = response.body.get("error")
        if isinstance(error, Mapping):
            for key in ("message", "type", "code"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    return value
        elif isinstance(error, str) and error:
            return error
        for key in ("message", "detail"):
            value = response.body.get(key)
            if isinstance(value, str) and value:
                return value
        return response.text.strip()[:200]

    @staticmethod
    def _split_system(messages: list[Message], system: str | None) -> tuple[str | None, list[Message]]:
        """Separate system text from the turns, for APIs that take it apart."""
        collected = [message.content for message in messages if message.role == "system"]
        if system:
            collected.insert(0, system)
        chat = [message for message in messages if message.role != "system"]
        return ("\n\n".join(collected) if collected else None), chat

    @staticmethod
    def _usage(body: Mapping[str, Any], *, input_key: str, output_key: str) -> Usage:
        usage = body.get("usage")
        source: Mapping[str, Any] = usage if isinstance(usage, Mapping) else body
        return Usage(
            input_tokens=_as_int(source.get(input_key)),
            output_tokens=_as_int(source.get(output_key)),
        )

    def _model_list(self, *, container: str, key: str) -> tuple[str, ...]:
        """``{container: [{key: id}, ...]}`` -> ids, in the order given."""
        body = self._request("GET", self.models_path).body
        entries = body.get(container)
        if not isinstance(entries, list):
            return ()
        names: list[str] = []
        for entry in entries:
            if isinstance(entry, Mapping):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    names.append(value)
        return tuple(names)


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
