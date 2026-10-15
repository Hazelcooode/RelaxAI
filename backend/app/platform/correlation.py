"""Correlation IDs and the per-request context middleware.

``RequestContextMiddleware`` is the outermost application middleware. For every HTTP
request it:

* accepts a well-formed inbound ``X-Correlation-Id`` (from Nginx or the client) or mints one,
  stores it in a ``contextvar`` for logs and in ``request.state`` for handlers;
* echoes it on every response, including unexpected-error responses;
* converts any unhandled exception into ``500 INTERNAL_ERROR`` carrying only the correlation
  ID (the exception message is never logged or returned);
* writes one access-log line with the route *template*, status, and duration.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.platform.http.errors import internal_error_response

HEADER = "X-Correlation-Id"
_HEADER_KEY = HEADER.lower().encode("latin-1")
_VALID = re.compile(r"^[A-Za-z0-9._:\-]{8,128}$")

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

access_logger = logging.getLogger("relaxai.access")
error_logger = logging.getLogger("relaxai.http")


def current_correlation_id() -> str | None:
    return correlation_id_var.get()


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def _inbound(scope: Scope) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == _HEADER_KEY:
            candidate: str = value.decode("latin-1")
            return candidate if _VALID.match(candidate) else None
    return None


def route_template(scope: Scope) -> str:
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template if isinstance(template, str) else "<unmatched>"


def _exception_location(exc: BaseException) -> str:
    tb = exc.__traceback__
    if tb is None:
        return "<unknown>"
    while tb.tb_next is not None:
        tb = tb.tb_next
    code = tb.tb_frame.f_code
    return f"{code.co_filename.rsplit('/', 1)[-1]}:{code.co_name}:{tb.tb_lineno}"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = _inbound(scope) or new_correlation_id()
        token = correlation_id_var.set(correlation_id)
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        started_at = time.perf_counter()
        status = 500
        response_started = False

        async def send_with_correlation(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)[HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        except Exception as exc:
            error_logger.error(
                "http.unhandled_exception",
                extra={
                    "exc_type": type(exc).__name__,
                    "exc_location": _exception_location(exc),
                    "route": route_template(scope),
                },
            )
            if response_started:
                raise
            await internal_error_response(correlation_id)(scope, receive, send_with_correlation)
        finally:
            access_logger.info(
                "http.request",
                extra={
                    "method": scope.get("method"),
                    "route": route_template(scope),
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                },
            )
            correlation_id_var.reset(token)
