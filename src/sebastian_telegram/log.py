"""stderr logging: key=value lines with the token redacted everywhere."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import IO

from .secret import Secret, scrub


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Log one `key=value` line; callers never pass message text."""
    parts = [f"event={event}"]
    parts.extend(f"{key}={value}" for key, value in fields.items())
    logger.log(level, " ".join(parts))


class RedactTokenFilter(logging.Filter):
    """Last line of defence: the token never reaches a handler record."""

    def __init__(self, secret: Secret) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = scrub(message, self._secret)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class KeyValueFormatter(logging.Formatter):
    def __init__(self, secret: Secret) -> None:
        super().__init__()
        self._secret = secret

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
        body = f"ts={timestamp} level={record.levelname.lower()} {record.getMessage()}"
        return scrub(body, self._secret)


def configure_logging(
    secret: Secret,
    *,
    stream: IO[str] | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(KeyValueFormatter(secret))
    handler.addFilter(RedactTokenFilter(secret))

    logger = logging.getLogger("sebastian_telegram")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
