"""HTTP mapping of errors to the contract's ``ErrorEnvelope`` (contract §2)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from app.platform.errors import DEFAULT_MESSAGES, HTTP_STATUS, AppError, ErrorCode, FieldIssue
from app.platform.schema import CamelModel

_REQUEST_SOURCES = {"body", "query", "path", "header", "cookie"}
# Pydantic error types whose message may embed caller-supplied text.
_UNSAFE_MESSAGE_TYPES = {"value_error", "assertion_error", "json_invalid"}
_GENERIC_FIELD_MESSAGE = "Invalid value."


class ErrorDetail(CamelModel):
    field: str | None = None
    code: str
    message: str


class ErrorBody(CamelModel):
    code: ErrorCode
    message: str
    correlation_id: str
    details: list[ErrorDetail] | None = None
    current: dict[str, Any] | None = Field(
        default=None,
        description="Authoritative resource on VERSION_CONFLICT, CHAT_SCOPE_LOCKED, "
        "UNDO_EXPIRED, REVIEW_ALREADY_RESOLVED.",
    )
    retry_after_seconds: int | None = None
    allowed_transitions: list[str] | None = None


class ErrorEnvelope(CamelModel):
    error: ErrorBody


def envelope_response(
    *,
    code: ErrorCode,
    correlation_id: str,
    message: str | None = None,
    details: Sequence[FieldIssue] = (),
    current: BaseModel | None = None,
    retry_after_seconds: int | None = None,
    allowed_transitions: Sequence[str] | None = None,
    status_code: int | None = None,
) -> JSONResponse:
    body = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message or DEFAULT_MESSAGES[code],
            correlation_id=correlation_id,
            details=[ErrorDetail(field=d.field, code=d.code, message=d.message) for d in details]
            or None,
            current=current.model_dump(mode="json", by_alias=True) if current else None,
            retry_after_seconds=retry_after_seconds,
            allowed_transitions=list(allowed_transitions)
            if allowed_transitions is not None
            else None,
        )
    )
    headers = {"Retry-After": str(retry_after_seconds)} if retry_after_seconds else None
    return JSONResponse(
        status_code=status_code or HTTP_STATUS[code],
        content=body.model_dump(mode="json", exclude_none=True),
        headers=headers,
    )


def internal_error_response(correlation_id: str) -> JSONResponse:
    return envelope_response(code=ErrorCode.INTERNAL_ERROR, correlation_id=correlation_id)


def _correlation_id(request: Request) -> str:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else "unknown"


async def _handle_app_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, AppError)  # noqa: S101 - registered for AppError only
    return envelope_response(
        code=exc.code,
        correlation_id=_correlation_id(request),
        message=exc.message,
        details=exc.details,
        current=exc.current,
        retry_after_seconds=exc.retry_after_seconds,
        allowed_transitions=exc.allowed_transitions,
    )


def validation_issues(errors: Sequence[Any]) -> list[FieldIssue]:
    """Field names and error kinds only — submitted values are never echoed."""
    issues: list[FieldIssue] = []
    for error in errors:
        loc = [str(part) for part in error.get("loc", ())]
        if loc and loc[0] in _REQUEST_SOURCES:
            loc = loc[1:]
        kind = str(error.get("type", "invalid"))
        message = str(error.get("msg", _GENERIC_FIELD_MESSAGE))
        raw_input = error.get("input")
        if kind in _UNSAFE_MESSAGE_TYPES or (
            isinstance(raw_input, str | int | float)
            and str(raw_input)
            and str(raw_input) in message
        ):
            message = _GENERIC_FIELD_MESSAGE
        issues.append(FieldIssue(field=".".join(loc) or None, code=kind, message=message))
    return issues


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    errors = exc.errors()
    if any(e.get("type") == "json_invalid" for e in errors):
        return envelope_response(
            code=ErrorCode.MALFORMED_REQUEST, correlation_id=_correlation_id(request)
        )
    return envelope_response(
        code=ErrorCode.VALIDATION_FAILED,
        correlation_id=_correlation_id(request),
        details=validation_issues(errors),
    )


_STATUS_TO_CODE = {
    400: ErrorCode.MALFORMED_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.CAPABILITY_DENIED,
    404: ErrorCode.NOT_AVAILABLE,
    413: ErrorCode.FILE_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_FORMAT,
    422: ErrorCode.VALIDATION_FAILED,
    428: ErrorCode.PRECONDITION_REQUIRED,
    429: ErrorCode.RATE_LIMITED,
}


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    """Framework-raised HTTP errors (unknown route, wrong method, ...) use the envelope too."""
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    if exc.status_code >= 500:
        return internal_error_response(_correlation_id(request))
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.MALFORMED_REQUEST)
    response = envelope_response(
        code=code, correlation_id=_correlation_id(request), status_code=exc.status_code
    )
    if exc.headers:
        response.headers.update(exc.headers)
    return response


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
