"""AI providers: your account, your keys, your choice of model.

The agent never proxies a request through a vendor of its own and never invents
a fallback endpoint - a provider without a credential refuses to be built.

The transport lives in :mod:`rn_agent.net`, not here: the npm registry and the
upstream React Native diffs use the same seam.
"""

from __future__ import annotations

from .anthropic import AnthropicProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .provider import AIProvider, ProviderIdentity
from .registry import (
    PROVIDERS,
    ProviderSpec,
    build_provider,
    canonical_name,
    provider_names,
    resolve_spec,
    specs,
)
from .types import Completion, Message, Usage

__all__ = [
    "PROVIDERS",
    "AIProvider",
    "AnthropicProvider",
    "Completion",
    "Message",
    "OllamaProvider",
    "OpenAIProvider",
    "ProviderIdentity",
    "ProviderSpec",
    "Usage",
    "build_provider",
    "canonical_name",
    "provider_names",
    "resolve_spec",
    "specs",
]
