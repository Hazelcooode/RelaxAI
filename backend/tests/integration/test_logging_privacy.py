"""SafeLogFilter and the access log: no request data ever reaches a log line."""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import AsyncIterator, Iterator

import httpx
import orjson
import pytest
from fastapi import APIRouter, FastAPI

from app.api.v1.schemas.base import ApiRequest
from app.platform.ids import new_id
from app.platform.logging import SafeLogFilter, build_handler

SECRET_EMAIL = "secret-name@example.com"


class Person(ApiRequest):
    email: str
    count: int


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Every log line as the production handler would write it."""
    stream = io.StringIO()
    handler = build_handler(stream)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [orjson.loads(line) for line in stream.getvalue().splitlines() if line]


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    router = APIRouter(prefix="/api/v1/_probe")
    log = logging.getLogger("relaxai.test")

    @router.post("/projects/{projectId}/people")
    async def add_person(projectId: str, body: Person) -> dict[str, str]:  # noqa: N803
        # A careless developer passes personal data three different ways:
        log.info("person.added %s", body.email, extra={"email": body.email, "count": body.count})
        return {"ok": "yes"}

    @router.get("/explode")
    async def explode() -> None:
        raise ValueError(f"cannot parse {SECRET_EMAIL}")

    api_app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as http:
        yield http


async def test_request_body_never_logged(client: httpx.AsyncClient, captured: io.StringIO) -> None:
    project_id = new_id("prj")
    r = await client.post(
        f"/api/v1/_probe/projects/{project_id}/people?q={SECRET_EMAIL}",
        json={"email": SECRET_EMAIL, "count": 3},
    )
    assert r.status_code == 200
    raw = captured.getvalue()
    assert SECRET_EMAIL not in raw
    assert project_id not in raw  # raw paths are never logged, only route templates

    access = [line for line in _lines(captured) if line["event"] == "http.request"]
    assert len(access) == 1
    assert access[0]["route"] == "/api/v1/_probe/projects/{projectId}/people"
    assert access[0]["status"] == 200
    assert access[0]["method"] == "POST"
    assert isinstance(access[0]["duration_ms"], float)
    assert access[0]["correlation_id"] == r.headers["X-Correlation-Id"]

    app_line = next(line for line in _lines(captured) if line["logger"] == "relaxai.test")
    assert app_line["event"] == "person.added %s"  # template only, args never interpolated
    assert "email" not in app_line
    assert app_line["count"] == 3


async def test_validation_failure_not_logged(
    client: httpx.AsyncClient, captured: io.StringIO
) -> None:
    r = await client.post(
        f"/api/v1/_probe/projects/{new_id('prj')}/people", json={"email": SECRET_EMAIL}
    )
    assert r.status_code == 422
    assert SECRET_EMAIL not in captured.getvalue()


async def test_unhandled_exception_logs_type_not_message(
    client: httpx.AsyncClient, captured: io.StringIO
) -> None:
    r = await client.get("/api/v1/_probe/explode")
    assert r.status_code == 500
    raw = captured.getvalue()
    assert SECRET_EMAIL not in raw
    assert "cannot parse" not in raw
    error_line = next(
        line for line in _lines(captured) if line["event"] == "http.unhandled_exception"
    )
    assert error_line["exc_type"] == "ValueError"
    assert error_line["correlation_id"] == r.json()["error"]["correlationId"]
    assert "Traceback" not in raw


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "evt %s", (SECRET_EMAIL,), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_filter_drops_non_allowlisted_attributes() -> None:
    opaque = new_id("prj")
    record = _record(
        route="/api/v1/projects/{projectId}",
        status=200,
        project_id=opaque,
        user_id=SECRET_EMAIL,  # *_id must be an opaque identifier
        member_count=4,
        document_text="Quarterly results for Riley",
        email=SECRET_EMAIL,
        headers={"Authorization": "Bearer abc"},
        status_detail=object(),
    )
    assert SafeLogFilter().filter(record)
    kept = record.__dict__
    assert kept["route"] == "/api/v1/projects/{projectId}"
    assert kept["status"] == 200
    assert kept["project_id"] == opaque
    assert kept["member_count"] == 4
    for dropped in ("user_id", "document_text", "email", "headers", "status_detail"):
        assert dropped not in kept
    assert record.getMessage() == "evt %s"


def test_filter_strips_exception_text() -> None:
    try:
        raise KeyError(SECRET_EMAIL)
    except KeyError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    stream = io.StringIO()
    handler = build_handler(stream)
    handler.handle(record)
    out = stream.getvalue()
    assert SECRET_EMAIL not in out
    assert orjson.loads(out)["exc_type"] == "KeyError"
