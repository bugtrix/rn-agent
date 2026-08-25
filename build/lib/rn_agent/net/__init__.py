"""The one place the agent speaks HTTP.

Three subsystems need a network now - AI providers, the npm registry
(``upgrade``) and the upstream React Native diffs (``migrate``) - so the
transport seam lives here rather than inside any one of them. Callers take a
:class:`JsonTransport`, which is what lets a test hand them a fake instead of a
socket.
"""

from __future__ import annotations

from .http import (
    DEFAULT_TIMEOUT,
    HttpResponse,
    HttpxTransport,
    JsonTransport,
    TransportError,
    default_transport,
    parse_body,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "HttpResponse",
    "HttpxTransport",
    "JsonTransport",
    "TransportError",
    "default_transport",
    "parse_body",
]
