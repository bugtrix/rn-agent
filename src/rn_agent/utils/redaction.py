"""Secret hygiene.

Two independent guards:

* :func:`is_secret_path` - files the agent refuses to read into AI context
  (``.env``, keystores, provisioning profiles, ``google-services.json``, ...).
* :func:`redact` - scrubs token-shaped strings out of anything written to a log
  or rendered to the terminal.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePath

from ..constants import REDACTED, SECRET_FILE_PATTERNS, SECRET_VALUE_PATTERNS

_COMPILED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern) for pattern in SECRET_VALUE_PATTERNS
)


def is_secret_path(path: str | PurePath, *, patterns: tuple[str, ...] = SECRET_FILE_PATTERNS) -> bool:
    """True when a path matches a known secret-bearing name."""
    pure = PurePath(path)
    name = pure.name.casefold()
    for pattern in patterns:
        folded = pattern.casefold()
        if fnmatch.fnmatchcase(name, folded):
            return True
        if "/" in folded and fnmatch.fnmatchcase(pure.as_posix().casefold(), f"*{folded}"):
            return True
    return False


def redact(text: str | None) -> str:
    """Replace token-shaped substrings with ``[redacted]``."""
    if not text:
        return ""
    cleaned = text
    for pattern in _COMPILED:
        cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Copy an environment mapping with suspicious values masked."""
    sensitive = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")
    return {
        key: (REDACTED if any(marker in key.upper() for marker in sensitive) else value)
        for key, value in env.items()
    }


def safe_display_path(path: Path, *, root: Path | None = None) -> str:
    """Project-relative when possible, ``~``-shortened otherwise."""
    try:
        if root is not None:
            return str(path.relative_to(root))
    except ValueError:
        pass
    text = str(path)
    home = str(Path.home())
    return f"~{text[len(home):]}" if text.startswith(home) else text
