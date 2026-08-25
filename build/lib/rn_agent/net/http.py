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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..errors import TransportError

DEFAULT_TIMEOUT = 120.0

__all__ = [
    "DEFAULT_TIMEOUT",
    "FileTransport",
    "HttpResponse",
    "HttpxDownloader",
    "HttpxTransport",
    "JsonTransport",
    "TransportError",
    "default_downloader",
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


@runtime_checkable
class FileTransport(Protocol):
    """Streams a URL to a path. Separate from :class:`JsonTransport` on purpose.

    A JSON response is parsed and small; an artefact is neither. Keeping the two
    apart means the JSON seam never grows a "sometimes it is bytes" branch, and a
    test can hand a caller a fake downloader that writes a file from a fixture.
    """

    def download(
        self,
        url: str,
        target: Path,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class HttpxDownloader:
    """Streams to disk in chunks, so a 75 MB artefact never sits in memory."""

    chunk_bytes: int = 1 << 16

    def download(
        self,
        url: str,
        target: Path,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        import httpx

        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        # A partial file must never look like a finished one, so the bytes land
        # beside the target and are renamed once the stream closes cleanly.
        staging = target.with_name(f"{target.name}.part")
        try:
            with (
                httpx.Client(timeout=timeout, follow_redirects=True) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code >= 400:
                    raise TransportError(
                        f"GET {url} failed (HTTP {response.status_code})",
                        hint="The artefact may not exist for this platform.",
                    )
                total = int(response.headers.get("content-length") or 0)
                with staging.open("wb") as handle:
                    for chunk in response.iter_bytes(self.chunk_bytes):
                        handle.write(chunk)
                        written += len(chunk)
                        if on_progress is not None:
                            on_progress(written, total)
        except httpx.TimeoutException as exc:
            staging.unlink(missing_ok=True)
            raise TransportError(
                f"downloading {url} timed out after {timeout:g}s",
                hint="Try again, or check a proxy that may be truncating the transfer.",
            ) from exc
        except httpx.HTTPError as exc:
            staging.unlink(missing_ok=True)
            raise TransportError(
                f"cannot download {url}: {exc}", hint="Check your network and proxy settings."
            ) from exc
        staging.replace(target)
        return written


def default_downloader() -> FileTransport:
    return HttpxDownloader()


def default_transport() -> JsonTransport:
    """The transport a caller uses when it is not given one."""
    return HttpxTransport()
