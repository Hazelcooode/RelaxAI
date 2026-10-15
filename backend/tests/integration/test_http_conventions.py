"""[idem] and [ver] through probe routes over a test-only versioned table (T04)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Response
from sqlalchemy import Column, Integer, MetaData, String, Table, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.v1.conventions import Idempotent, IdempotentCommand, request_hash
from app.api.v1.deps import ActiveActor, AppContainer
from app.api.v1.schemas.base import ApiModel, ApiRequest
from app.container import Container
from app.modules.governance.tables import idempotency_keys
from app.modules.organization_access import Actor
from app.platform.db import optimistic_update
from app.platform.errors import AppError, ErrorCode
from app.platform.http.concurrency import IfMatch, set_etag
from app.platform.ids import new_id
from tests.auth import TestTokenIssuer
from tests.fixtures.synthetic.personas import AVERY, MORGAN, Persona

things = Table(
    "t04_things",
    MetaData(schema="governance"),
    Column("id", String(40), primary_key=True),
    Column("name", String(80), nullable=False),
    Column("version", Integer, nullable=False),
)


class ThingCreate(ApiRequest):
    name: str
    fail: bool = False


class ThingRename(ApiRequest):
    name: str


class ThingView(ApiModel):
    thing_id: str
    name: str
    version: int


def _view(row: Any) -> ThingView:
    return ThingView(thing_id=row["id"], name=row["name"], version=row["version"])


def _probe_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/_probe")

    async def create(container: Container, body: ThingCreate) -> ThingView:
        # Stands in for a module service: it opens its own unit of work.
        async with container.uow() as tx:
            row = (
                (
                    await tx.session.execute(
                        things.insert()
                        .values(id=new_id("tsk"), name=body.name, version=1)
                        .returning(*things.c)
                    )
                )
                .mappings()
                .one()
            )
            if body.fail:
                raise AppError(ErrorCode.INVALID_TRANSITION, allowed_transitions=[])
        return _view(row)

    @router.post("/things", status_code=201, response_model=ThingView)
    async def create_thing(
        body: ThingCreate, container: AppContainer, actor: ActiveActor, idem: Idempotent
    ) -> Response:
        return await idem.run(lambda: create(container, body), status_code=201)

    @router.post("/things/{thing_id}/archive", status_code=204)
    async def archive_thing(thing_id: str, actor: ActiveActor, idem: Idempotent) -> Response:
        async def nothing() -> None:
            return None

        return await idem.run(nothing, status_code=204)

    @router.get("/things/{thing_id}", response_model=ThingView)
    async def get_thing(
        thing_id: str, response: Response, container: AppContainer, actor: ActiveActor
    ) -> ThingView:
        async with container.uow() as tx:
            row = (
                (await tx.session.execute(select(things).where(things.c.id == thing_id)))
                .mappings()
                .one()
            )
        set_etag(response, row["version"])
        return _view(row)

    @router.patch("/things/{thing_id}", response_model=ThingView)
    async def rename_thing(
        thing_id: str,
        body: ThingRename,
        version: IfMatch,
        response: Response,
        container: AppContainer,
        actor: ActiveActor,
    ) -> ThingView:
        async with container.uow() as tx:
            row = await optimistic_update(
                tx, things, thing_id, version, {"name": body.name}, to_current=_view
            )
        set_etag(response, row["version"])
        return _view(row)

    return router


@pytest.fixture
async def client(
    api_app: FastAPI, db_connection: AsyncConnection
) -> AsyncIterator[httpx.AsyncClient]:
    await db_connection.run_sync(things.create)
    api_app.include_router(_probe_router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as http:
        yield http


@pytest.fixture
async def as_avery(
    token_issuer: TestTokenIssuer, activate: Callable[[Persona], Awaitable[Actor]]
) -> dict[str, str]:
    await activate(AVERY)
    return token_issuer.headers_for(AVERY)


async def _count(container: Container, table: Table) -> int:
    async with container.uow() as tx:
        return int((await tx.session.execute(select(func.count()).select_from(table))).scalar_one())


# --------------------------------------------------------------------------- [idem]


async def test_replay_returns_the_stored_response(
    client: httpx.AsyncClient, container: Container, as_avery: dict[str, str]
) -> None:
    headers = {**as_avery, "Idempotency-Key": str(uuid.uuid4())}
    first = await client.post("/api/v1/_probe/things", json={"name": "Alpha"}, headers=headers)
    assert first.status_code == 201
    assert "Idempotency-Replayed" not in first.headers

    # Key order and whitespace do not change the request.
    again = await client.post(
        "/api/v1/_probe/things", content=b'{ "name" :"Alpha" }', headers={
            **headers, "Content-Type": "application/json"
        },
    )  # fmt: skip
    assert again.status_code == 201
    assert again.json() == first.json()
    assert again.headers["Idempotency-Replayed"] == "true"
    assert await _count(container, things) == 1  # the command ran once


async def test_same_key_with_a_different_body_is_refused(
    client: httpx.AsyncClient, container: Container, as_avery: dict[str, str]
) -> None:
    headers = {**as_avery, "Idempotency-Key": str(uuid.uuid4())}
    await client.post("/api/v1/_probe/things", json={"name": "Alpha"}, headers=headers)
    r = await client.post("/api/v1/_probe/things", json={"name": "Beta"}, headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert await _count(container, things) == 1


async def test_keys_are_scoped_per_actor_and_route(
    client: httpx.AsyncClient, container: Container, token_issuer: TestTokenIssuer,
    activate: Callable[[Persona], Awaitable[Actor]], as_avery: dict[str, str],
) -> None:  # fmt: skip
    key = str(uuid.uuid4())
    await client.post(
        "/api/v1/_probe/things", json={"name": "A"}, headers={**as_avery, "Idempotency-Key": key}
    )
    await activate(MORGAN)
    morgan = {**token_issuer.headers_for(MORGAN), "Idempotency-Key": key}
    r = await client.post("/api/v1/_probe/things", json={"name": "A"}, headers=morgan)
    assert r.status_code == 201
    assert "Idempotency-Replayed" not in r.headers
    assert await _count(container, things) == 2

    thing_id = r.json()["thingId"]
    archive = await client.post(f"/api/v1/_probe/things/{thing_id}/archive", headers=morgan)
    assert archive.status_code == 204
    replay = await client.post(f"/api/v1/_probe/things/{thing_id}/archive", headers=morgan)
    assert replay.status_code == 204
    assert replay.headers["Idempotency-Replayed"] == "true"


async def test_missing_or_malformed_key_names_the_header(
    client: httpx.AsyncClient, as_avery: dict[str, str]
) -> None:
    r = await client.post("/api/v1/_probe/things", json={"name": "Alpha"}, headers=as_avery)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert r.json()["error"]["details"][0]["field"] == "Idempotency-Key"
    r = await client.post(
        "/api/v1/_probe/things",
        json={"name": "Alpha"},
        headers={**as_avery, "Idempotency-Key": "not-a-uuid"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["code"] == "uuid"


async def test_a_failed_command_stores_no_key_so_a_retry_executes(
    client: httpx.AsyncClient, container: Container, as_avery: dict[str, str]
) -> None:
    headers = {**as_avery, "Idempotency-Key": str(uuid.uuid4())}
    failed = await client.post(
        "/api/v1/_probe/things", json={"name": "Alpha", "fail": True}, headers=headers
    )
    assert failed.status_code == 409
    assert await _count(container, idempotency_keys) == 0
    assert await _count(container, things) == 0  # the command's own write rolled back too
    # A retry with the same key is a fresh attempt (it may even change the body).
    ok = await client.post("/api/v1/_probe/things", json={"name": "Alpha"}, headers=headers)
    assert ok.status_code == 201
    assert "Idempotency-Replayed" not in ok.headers


async def test_unauthenticated_and_not_activated_callers_never_reach_the_key(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, firm: str
) -> None:
    key = {"Idempotency-Key": str(uuid.uuid4())}
    r = await client.post("/api/v1/_probe/things", json={"name": "A"}, headers=key)
    assert r.status_code == 401
    r = await client.post(
        "/api/v1/_probe/things",
        json={"name": "A"},
        headers={**key, **token_issuer.headers_for(AVERY)},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ACCOUNT_NOT_ACTIVATED"


def test_request_hash_covers_route_params_and_canonical_body() -> None:
    base = request_hash("POST", "/x/{id}", {"id": "a"}, b'{"b": 1, "a": 2}')
    assert base == request_hash("post", "/x/{id}", {"id": "a"}, b'{"a":2,"b":1}')
    assert base != request_hash("POST", "/x/{id}", {"id": "b"}, b'{"a":2,"b":1}')
    assert base != request_hash("POST", "/y/{id}", {"id": "a"}, b'{"a":2,"b":1}')
    assert base != request_hash("POST", "/x/{id}", {"id": "a"}, b'{"a":2,"b":2}')


async def test_the_key_row_is_invisible_until_the_command_commits(
    isolated_container: Container,
) -> None:
    """Commits for real: a second connection must not see the key mid-command."""
    started, release = asyncio.Event(), asyncio.Event()
    command = IdempotentCommand(
        container=isolated_container,
        actor_id="usr_01J9Z3Q8M8V3W7G2E1N4R5T6Y7",
        route="/probe",
        key=str(uuid.uuid4()),
        request_hash="h" * 64,
    )

    async def slow() -> ThingView:
        started.set()
        await release.wait()
        return ThingView(thing_id="tsk_1", name="x", version=1)

    task = asyncio.create_task(command.run(slow, status_code=201))
    await started.wait()
    async with isolated_container.engine.connect() as other:
        seen = (await other.execute(select(func.count()).select_from(idempotency_keys))).scalar()
    release.set()
    response = await task
    assert seen == 0
    assert response.status_code == 201
    async with isolated_container.engine.connect() as other:
        after = (await other.execute(select(func.count()).select_from(idempotency_keys))).scalar()
    assert after == 1
    async with isolated_container.engine.begin() as conn:
        await conn.execute(idempotency_keys.delete())


async def test_expired_keys_are_forgotten(
    client: httpx.AsyncClient, container: Container, as_avery: dict[str, str],
    frozen_clock: Any, token_issuer: TestTokenIssuer,
) -> None:  # fmt: skip
    from datetime import timedelta

    key = {"Idempotency-Key": str(uuid.uuid4())}
    await client.post("/api/v1/_probe/things", json={"name": "Alpha"}, headers={**as_avery, **key})
    frozen_clock.advance(timedelta(hours=25))
    headers = {**token_issuer.headers_for(AVERY), **key}  # a fresh token at the new time
    r = await client.post("/api/v1/_probe/things", json={"name": "Beta"}, headers=headers)
    assert r.status_code == 201  # a fresh attempt, not IDEMPOTENCY_KEY_REUSED
    assert await container.idempotency.purge_expired() == 0
    frozen_clock.advance(timedelta(hours=25))
    assert await container.idempotency.purge_expired() == 1


# --------------------------------------------------------------------------- [ver]


async def test_if_match_conflicts_and_etags(
    client: httpx.AsyncClient, as_avery: dict[str, str]
) -> None:
    created = await client.post(
        "/api/v1/_probe/things",
        json={"name": "Alpha"},
        headers={**as_avery, "Idempotency-Key": str(uuid.uuid4())},
    )
    thing_id = created.json()["thingId"]
    path = f"/api/v1/_probe/things/{thing_id}"
    got = await client.get(path, headers=as_avery)
    assert got.headers["ETag"] == '"1"'

    missing = await client.patch(path, json={"name": "Beta"}, headers=as_avery)
    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "PRECONDITION_REQUIRED"

    renamed = await client.patch(
        path, json={"name": "Beta"}, headers={**as_avery, "If-Match": got.headers["ETag"]}
    )
    assert renamed.status_code == 200
    assert renamed.headers["ETag"] == '"2"'
    assert renamed.json()["version"] == 2

    stale = await client.patch(
        path, json={"name": "Gamma"}, headers={**as_avery, "If-Match": '"1"'}
    )
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "VERSION_CONFLICT"
    # ``current`` is the authoritative resource, valid against the GET schema.
    current = ThingView.model_validate(error["current"])
    assert current == ThingView.model_validate((await client.get(path, headers=as_avery)).json())
    assert current.name == "Beta"

    weak = await client.patch(
        path, json={"name": "Delta"}, headers={**as_avery, "If-Match": 'W/"2"'}
    )
    assert weak.status_code == 200
    bad = await client.patch(path, json={"name": "Eps"}, headers={**as_avery, "If-Match": "three"})
    assert bad.status_code == 422
    assert bad.json()["error"]["details"][0]["field"] == "If-Match"
