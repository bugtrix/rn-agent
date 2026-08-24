"""Filesystem primitives: size-capped reads, atomic writes, JSON/YAML."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..constants import MAX_FILE_READ_BYTES


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_text(path: Path, *, limit: int = MAX_FILE_READ_BYTES) -> str | None:
    """Read a text file, tolerating binary junk and missing files.

    Returns ``None`` when the file does not exist or cannot be read - the
    scanner treats "absent" and "unreadable" the same way and never raises for
    one bad file.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError, OSError):
        return None
    if len(raw) > limit:
        raw = raw[:limit]
    return raw.decode("utf-8", errors="replace")


def read_json(path: Path, *, default: Any = None) -> Any:
    text = read_text(path)
    if text is None:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return default


def read_yaml(path: Path, *, default: Any = None) -> Any:
    text = read_text(path)
    if text is None:
        return default
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return default
    return default if loaded is None else loaded


def atomic_write_text(path: Path, content: str) -> Path:
    """Write via a temp file + rename so a crash never truncates the target."""
    ensure_dir(path.parent)
    # delete=False + explicit rename is the atomic-write recipe; a context
    # manager would remove the file before os.replace could rename it.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)  # noqa: PTH105 - atomic rename, not a path op
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def write_json(path: Path, payload: Any, *, indent: int = 2) -> Path:
    text = json.dumps(payload, indent=indent, ensure_ascii=False, default=str, sort_keys=False)
    return atomic_write_text(path, text + "\n")


def write_yaml(path: Path, payload: Any, *, header: str | None = None) -> Path:
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)
    text = f"{header.rstrip()}\n{body}" if header else body
    return atomic_write_text(path, text)


def iter_lines(text: str | None) -> list[str]:
    return [] if text is None else text.splitlines()


def file_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # pragma: no cover - defensive
        return False
