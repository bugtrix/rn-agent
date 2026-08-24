"""Per-command logging into ``.rn-agent/logs/``.

Requirement §35: each command writes its own log file (``scan.log``,
``health.log``, ...). Every record passes through a redaction filter so a token
that leaks into a subprocess error never lands on disk.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
from pathlib import Path

from ..constants import APP_NAME, LOG_BACKUP_COUNT, LOG_MAX_BYTES
from ..utils.redaction import redact

LOGGER_ROOT = "rn_agent"
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_TAG = "_rn_agent_handler"


class RedactingFilter(logging.Filter):
    """Scrub secret-shaped values from the message and its arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: (redact(value) if isinstance(value, str) else value)
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact(value) if isinstance(value, str) else value for value in record.args
                )
        return True


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_ROOT if name is None else f"{LOGGER_ROOT}.{name}")


def configure_logging(
    log_dir: Path | None,
    *,
    command: str,
    level: str | int = "INFO",
    enabled: bool = True,
) -> logging.Logger:
    """Attach a rotating file handler for one command invocation."""
    root = logging.getLogger(LOGGER_ROOT)
    root.setLevel(_coerce(level))
    root.propagate = False
    _clear(root)

    if not enabled or log_dir is None:
        root.addHandler(logging.NullHandler())
        return root

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / f"{command}.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        # A read-only project must not break the command.
        root.addHandler(logging.NullHandler())
        return root

    handler.setLevel(_coerce(level))
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(RedactingFilter())
    setattr(handler, _TAG, True)
    root.addHandler(handler)
    root.debug("%s logging initialised for command %r", APP_NAME, command)
    return root


def shutdown_logging() -> None:
    _clear(logging.getLogger(LOGGER_ROOT))


def _clear(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if isinstance(handler, logging.NullHandler) or getattr(handler, _TAG, False):
            logger.removeHandler(handler)
            with contextlib.suppress(OSError):
                handler.close()


def _coerce(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return logging.getLevelNamesMapping().get(str(level).upper(), logging.INFO)
