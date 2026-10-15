from __future__ import annotations

import dataclasses

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from app.container import Container
from app.main_api import create_api_app
from app.platform.db.migrations import DatabaseReadiness


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def test_healthz(api_client: httpx.AsyncClient) -> None:
    r = await api_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert "X-Correlation-Id" in r.headers


async def test_readyz_ready(api_client: httpx.AsyncClient) -> None:
    r = await api_client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {"database": "ok", "migrations": "ok"}}
    assert r.headers["Cache-Control"] == "no-store"


async def test_readyz_pending_migration(container: Container) -> None:
    probe = DatabaseReadiness(container.engine, heads=frozenset({"9999_future"}))
    app = create_api_app(dataclasses.replace(container, readiness=probe))
    r = await _get(app, "/readyz")
    assert r.status_code == 503
    assert r.json() == {
        "status": "unavailable",
        "checks": {"database": "ok", "migrations": "pending"},
    }


async def test_readyz_database_unreachable(container: Container) -> None:
    dead = create_async_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        connect_args={"connect_timeout": 1},
    )
    probe = DatabaseReadiness(dead, heads=frozenset({"0001_platform"}))
    app = create_api_app(dataclasses.replace(container, readiness=probe))
    r = await _get(app, "/readyz")
    await dead.dispose()
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": "unavailable", "migrations": "unknown"}


async def test_healthz_does_not_need_database(container: Container) -> None:
    dead = create_async_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    app = create_api_app(
        dataclasses.replace(container, readiness=DatabaseReadiness(dead, frozenset()))
    )
    r = await _get(app, "/healthz")
    await dead.dispose()
    assert r.status_code == 200
