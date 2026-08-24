"""Which providers exist, and how to build one from configuration.

The specs are *derived* from the provider classes, so a provider's defaults are
declared exactly once - adding a backend means adding a class and one line here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..errors import ProviderError
from ..models.config import AIConfig
from .anthropic import AnthropicProvider
from .http import JsonTransport
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .provider import AIProvider

#: Names developers actually type, mapped to the canonical provider name.
ALIASES: dict[str, str] = {
    "claude": "anthropic",
    "claude-code": "anthropic",
    "gpt": "openai",
    "chatgpt": "openai",
    "local": "ollama",
    "llama": "ollama",
}


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Everything the CLI needs to describe a provider without building one."""

    name: str
    label: str
    provider_class: type[AIProvider]
    env_var: str | None
    requires_credential: bool
    default_model: str
    suggested_models: tuple[str, ...]
    base_url: str
    docs_url: str

    @classmethod
    def of(cls, provider_class: type[AIProvider]) -> ProviderSpec:
        return cls(
            name=provider_class.name,
            label=provider_class.label,
            provider_class=provider_class,
            env_var=provider_class.env_var,
            requires_credential=provider_class.requires_credential,
            default_model=provider_class.default_model,
            suggested_models=provider_class.suggested_models,
            base_url=provider_class.default_base_url,
            docs_url=provider_class.docs_url,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "env_var": self.env_var,
            "requires_credential": self.requires_credential,
            "default_model": self.default_model,
            "suggested_models": list(self.suggested_models),
            "base_url": self.base_url,
            "docs_url": self.docs_url,
        }


PROVIDERS: dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec.of(AnthropicProvider),
        ProviderSpec.of(OpenAIProvider),
        ProviderSpec.of(OllamaProvider),
    )
}


def provider_names() -> tuple[str, ...]:
    return tuple(PROVIDERS)


def specs() -> tuple[ProviderSpec, ...]:
    return tuple(PROVIDERS.values())


def canonical_name(name: str) -> str:
    key = name.strip().casefold()
    return key if key in PROVIDERS else ALIASES.get(key, key)


def resolve_spec(name: str | None) -> ProviderSpec:
    """Look a provider up by name or alias; unknown names list the valid ones."""
    if not name:
        raise ProviderError(
            "no AI provider configured",
            hint="Run `rn-agent login <provider>`; see `rn-agent provider --list`.",
        )
    spec = PROVIDERS.get(canonical_name(name))
    if spec is None:
        raise ProviderError(
            f"unknown AI provider: {name}",
            hint=f"Known providers: {', '.join(provider_names())}.",
        )
    return spec


def build_provider(
    config: AIConfig,
    *,
    credential: str | None,
    provider_name: str | None = None,
    model: str | None = None,
    task: str | None = None,
    base_url: str | None = None,
    transport: JsonTransport | None = None,
    logger: logging.Logger | None = None,
) -> AIProvider:
    """Instantiate the configured provider. Never performs a request."""
    spec = resolve_spec(provider_name or config.provider)
    return spec.provider_class(
        credential=credential,
        model=model or config.model_for(task),
        base_url=base_url or config.base_url,
        timeout=config.timeout_seconds,
        max_output_tokens=config.max_output_tokens,
        temperature=config.temperature,
        transport=transport,
        logger=logger,
    )
