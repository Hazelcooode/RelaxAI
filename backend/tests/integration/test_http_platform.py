"""Error envelope, correlation IDs, and cursor handling through the real ASGI app."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, Field

from app.api.v1.schemas.base import ApiModel, ApiRequest
from app.platform.errors import AppError, ErrorCode, FieldIssue, NotAvailable, VersionConflict
from app.platform.http.pagination import CursorCodec, Page, PageParams, page_params

SECRET_EMAIL = "secret-name@example.com"


class Contact(ApiRequest):
    display_name: str = Field(min_length=2)
    email_address: str = Field(pattern=r"^[^@]+@[^@]+$")
    age: int


class Echo(ApiModel):
    display_name: str


class TaskView(BaseModel):
    task_id: str
    version: int


def _probe_router(codec: CursorCodec) -> APIRouter:
    router = APIRouter(prefix="/api/v1/_probe")

    @router.post("/contacts")
    async def create_contact(body: Contact) -> Echo:
        return Echo(display_name=body.display_name)

    @router.get("/missing/{thing_id}")
    async def missing(thing_id: str) -> None:
        raise NotAvailable()

    @router.get("/conflict")
    async def conflict() -> None:
        raise VersionConflict(TaskView(task_id="tsk_1", version=4))

    @router.get("/custom")
    async def custom() -> None:
        raise AppError(
            ErrorCode.INVALID_TRANSITION,
            details=[FieldIssue(field="to", code="NotAllowed", message="Not allowed.")],
            allowed_transitions=["InProgress"],
        )

    @router.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"database said: duplicate key {SECRET_EMAIL}")

    @router.get("/items")
    async def items(params: Annotated[PageParams, Depends(page_params)]) -> Page[str]:
        if params.cursor is not None:
            codec.decode(params.cursor)
        return Page[str](items=["a"], next_cursor=codec.encode("a", "itm_x"))

    return router


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    api_app.include_router(_probe_router(api_app.state.container.cursors))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as http:
        yield http


def _assert_envelope(body: dict[str, object], code: str) -> dict[str, object]:
    assert set(body) == {"error"}
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    assert isinstance(error["correlationId"], str)
    return error


async def test_app_error_envelope(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/_probe/missing/prj_x")
    assert r.status_code == 404
    error = _assert_envelope(r.json(), "NOT_AVAILABLE")
    assert error["correlationId"] == r.headers["X-Correlation-Id"]
    assert set(error) == {"code", "message", "correlationId"}


async def test_version_conflict_returns_current(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/_probe/conflict")
    assert r.status_code == 409
    error = _assert_envelope(r.json(), "VERSION_CONFLICT")
    assert error["current"] == {"task_id": "tsk_1", "version": 4}


async def test_details_and_allowed_transitions(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/_probe/custom")
    assert r.status_code == 409
    error = _assert_envelope(r.json(), "INVALID_TRANSITION")
    assert error["allowedTransitions"] == ["InProgress"]
    assert error["details"] == [{"field": "to", "code": "NotAllowed", "message": "Not allowed."}]


async def test_validation_error_lists_fields_without_values(client: httpx.AsyncClient) -> None:
    payload = {"displayName": "x", "emailAddress": "nope-" + SECRET_EMAIL + "@@", "nickname": "Zed"}
    r = await client.post("/api/v1/_probe/contacts", json=payload)
    assert r.status_code == 422
    error = _assert_envelope(r.json(), "VALIDATION_FAILED")
    details = error["details"]
    assert isinstance(details, list)
    assert {d["field"] for d in details} == {"displayName", "emailAddress", "age", "nickname"}
    assert all(set(d) <= {"field", "code", "message"} for d in details)
    assert SECRET_EMAIL not in r.text
    assert "Zed" not in r.text
    assert '"x"' not in r.text


async def test_unparseable_json_is_malformed(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/_probe/contacts",
        content=b'{"displayName": "' + SECRET_EMAIL.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    _assert_envelope(r.json(), "MALFORMED_REQUEST")
    assert SECRET_EMAIL not in r.text


async def test_unexpected_exception_returns_only_correlation_id(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/api/v1/_probe/boom")
    assert r.status_code == 500
    error = _assert_envelope(r.json(), "INTERNAL_ERROR")
    assert set(error) == {"code", "message", "correlationId"}
    assert error["correlationId"] == r.headers["X-Correlation-Id"]
    assert SECRET_EMAIL not in r.text
    assert "duplicate" not in r.text


async def test_unknown_route_uses_envelope(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    _assert_envelope(r.json(), "NOT_AVAILABLE")


async def test_correlation_id_generated_when_absent(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    generated = r.headers["X-Correlation-Id"]
    assert uuid.UUID(generated)
    r2 = await client.get("/healthz")
    assert r2.headers["X-Correlation-Id"] != generated


async def test_correlation_id_echoed(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz", headers={"X-Correlation-Id": "nginx-req-0123456789"})
    assert r.headers["X-Correlation-Id"] == "nginx-req-0123456789"
    r = await client.get("/api/v1/_probe/missing/a", headers={"X-Correlation-Id": "abc-12345678"})
    assert r.json()["error"]["correlationId"] == "abc-12345678"


@pytest.mark.parametrize("bad", ["short", "has spaces in it", "x" * 200, "<script>alert</script>"])
async def test_malformed_inbound_correlation_id_is_replaced(
    client: httpx.AsyncClient, bad: str
) -> None:
    r = await client.get("/healthz", headers={"X-Correlation-Id": bad})
    assert r.headers["X-Correlation-Id"] != bad
    assert uuid.UUID(r.headers["X-Correlation-Id"])


async def test_cursor_round_trip_and_tampering(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/_probe/items")
    assert r.status_code == 200
    cursor = r.json()["nextCursor"]
    assert (await client.get("/api/v1/_probe/items", params={"cursor": cursor})).status_code == 200

    tampered = ("A" if cursor[0] != "A" else "B") + cursor[1:]
    r = await client.get("/api/v1/_probe/items", params={"cursor": tampered})
    assert r.status_code == 400
    _assert_envelope(r.json(), "MALFORMED_REQUEST")


@pytest.mark.parametrize("limit", ["0", "101", "abc"])
async def test_limit_bounds(client: httpx.AsyncClient, limit: str) -> None:
    r = await client.get("/api/v1/_probe/items", params={"limit": limit})
    assert r.status_code == 422
    error = _assert_envelope(r.json(), "VALIDATION_FAILED")
    assert error["details"][0]["field"] == "limit"  # type: ignore[index]
