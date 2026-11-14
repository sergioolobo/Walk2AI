"""Structured (JSON-lines) logging with a per-request correlation id.

Nothing here ever logs request bodies, headers, cookies, tokens or keys. Callers log
identifiers and outcomes only.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes every LogRecord carries; anything else was passed via ``extra=``.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


def current_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str | None) -> None:
    _request_id.set(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = current_request_id()
        if request_id:
            payload["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _Walk2AIHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marker type, so reconfiguring replaces our handler and nobody else's."""


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, _Walk2AIHandler):
            root.removeHandler(existing)
    handler = _Walk2AIHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Uvicorn's own access log would duplicate ours and includes query strings.
    logging.getLogger("uvicorn.access").disabled = True
