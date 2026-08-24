"""Values exchanged with a provider.

Runtime values, so plain frozen dataclasses (like ``CommandResult``) rather than
pydantic models - nothing here is serialised into ``.rn-agent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

ROLES: Final[frozenset[str]] = frozenset({"system", "user", "assistant"})


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of a conversation."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown message role: {self.role!r} (expected one of {sorted(ROLES)})")

    @classmethod
    def system(cls, content: str) -> Message:
        return cls("system", content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls("user", content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls("assistant", content)

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting, as reported by the provider."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Completion:
    """One model response."""

    text: str
    provider: str
    model: str
    usage: Usage = Usage()
    stop_reason: str | None = None
    task: str | None = None

    @property
    def truncated(self) -> bool:
        """True when the model stopped because it hit the output limit."""
        return self.stop_reason in {"max_tokens", "length", "limit"}
