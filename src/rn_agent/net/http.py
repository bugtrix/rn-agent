"""HTTP transport: one seam for every request the agent makes.

Every outbound request - a model completion, an npm registry lookup, an
upstream React Native diff - goes through a :class:`JsonTransport`, for the same
reason every external tool goes through ``CommandRunner``: timeouts, error
mapping and testability live in one place, and a test can hand a caller a fake
transport instead of monkeypatching a client library.

Nothing here knows about credentials beyond passing the headers it is given, and
no response body is ever logged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..errors import TransportError

DEFAULT_TIMEOUT = 120.0

__all__ = [
    "DEFAULT_TIMEOUT",
    "HttpResponse",
    "HttpxTransport",
    "JsonTransport",
    "TransportError",
    "default_transport",
    "parse_body",
]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One response: status, parsed JSON body, and the raw text as received.

    ``text`` matters as much as ``body``: the migration sources fetch unified
    diffs, which are text, through the same seam as JSON APIs.
    """

    status: int
    body: dict[str, Any]
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@runtime_checkable
class JsonTransport(Protocol):
    """Sends one request and returns the response, or raises TransportError."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse: ...


def parse_body(text: str) -> dict[str, Any]:
    """JSON object or ``{}`` - an HTML error page is not a reason to crash."""
    try:
        loaded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


@dataclass(frozen=True, slots=True)
class HttpxTransport:
    """Default transport: one short-lived ``httpx`` client per request."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse:
        import httpx

        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.request(method, url, headers=dict(headers), json=payload)
        except httpx.TimeoutException as exc:
            raise TransportError(
                f"{method} {url} timed out after {timeout:g}s",
                hint="Raise ai.timeout_seconds in .rn-agent/config.yaml, or try again.",
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                f"cannot reach {url}: {exc}",
                hint="Check your network and proxy settings.",
            ) from exc
        return HttpResponse(
            status=response.status_code, body=parse_body(response.text), text=response.text
        )


def default_transport() -> JsonTransport:
    """The transport a caller uses when it is not given one."""
    return HttpxTransport()
