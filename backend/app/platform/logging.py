"""Structured JSON logging with a privacy allowlist (conventions §3.5).

``SafeLogFilter`` is installed on the root handler, so it applies to every logger
(including uvicorn's). It:

* keeps only allowlisted ``extra`` attributes (route template, status, duration,
  correlation ID, job kind, attempt, error code, counts, and opaque identifiers);
* never interpolates ``%``-arguments — the message *template* is logged, so values passed
  as args cannot leak;
* drops exception text and tracebacks (they can carry data) and keeps only the exception
  type name.

Log messages must therefore be static event names such as ``"http.request"``.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import Any

import orjson

from app.platform.correlation import current_correlation_id
from app.platform.ids import is_opaque_id

ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "route", "method", "status", "duration_ms", "correlation_id", "job_kind", "attempt",
        "error_code", "count", "exc_type", "exc_location", "operation_state", "store",
    }
)  # fmt: skip

# Attributes every LogRecord has; anything else arrived through ``extra=``.
_STANDARD_ATTRIBUTES: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _allowed(name: str, value: Any) -> bool:
    if name in ALLOWED_ATTRIBUTES:
        return value is None or isinstance(value, str | int | float | bool)
    if name.endswith("_id"):
        return is_opaque_id(value)
    if name.endswith("_count"):
        return isinstance(value, int) and not isinstance(value, bool)
    return False


class SafeLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for name in list(record.__dict__):
            if name in _STANDARD_ATTRIBUTES:
                continue
            if not _allowed(name, record.__dict__[name]):
                del record.__dict__[name]

        if record.exc_info and record.exc_info[1] is not None:
            record.__dict__.setdefault("exc_type", type(record.exc_info[1]).__name__)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None

        record.msg = str(record.msg)
        record.args = None

        if getattr(record, "correlation_id", None) is None:
            correlation_id = current_correlation_id()
            if correlation_id is not None:
                record.correlation_id = correlation_id
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for name, value in record.__dict__.items():
            if name not in _STANDARD_ATTRIBUTES:
                payload[name] = value
        return orjson.dumps(payload, default=str).decode()


def build_handler(stream: Any = None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(SafeLogFilter())
    handler.setFormatter(JsonFormatter())
    return handler


def configure_logging(level: str = "INFO") -> None:
    """Route every logger through one safe JSON handler. Idempotent."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(build_handler())
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.asgi"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # uvicorn's access log prints raw paths and query strings; ours logs route templates.
    logging.getLogger("uvicorn.access").disabled = True
