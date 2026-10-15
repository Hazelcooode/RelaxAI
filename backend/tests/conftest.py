"""Shared test harness.

PostgreSQL comes from ``TEST_DATABASE_URL`` (``postgresql+psycopg://...``) or, when unset,
a disposable Testcontainers ``postgres:16`` (needs Docker). Migrations run once per session.
Each test gets one connection inside an outer transaction that is rolled back afterwards;
the ``container``'s unit of work runs as SAVEPOINTs on that connection.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
from alembic import command
from fastapi import FastAPI
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.container import Container, build_container
from app.main_api import create_api_app
from app.platform.clock import FrozenClock
from app.platform.db import UnitOfWork
from app.platform.db.migrations import alembic_config
from app.settings import Environment, Settings

FROZEN_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.postgres import PostgresContainer  # optional; needs Docker

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    config = alembic_config(database_url)
    config.attributes["skip_logging"] = True
    command.upgrade(config, "head")
    return database_url


@pytest.fixture(scope="session")
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database, poolclass=pool.NullPool)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            yield conn
        finally:
            await outer.rollback()


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(FROZEN_NOW)


@pytest.fixture
def settings(migrated_database: str) -> Settings:
    return Settings.model_validate(
        {
            "environment": Environment.TEST,
            "firm_id": "frm_test",
            "db": {"url": migrated_database},
            "crypto": {"cursor_signing_key": "test-cursor-signing-key-0123456789"},
        }
    )


@pytest.fixture
def container(
    settings: Settings,
    engine: AsyncEngine,
    db_connection: AsyncConnection,
    frozen_clock: FrozenClock,
) -> Container:
    """Override any adapter in a test with ``dataclasses.replace(container, x=...)``."""
    return build_container(
        settings, engine=engine, uow=UnitOfWork(db_connection), clock=frozen_clock
    )


@pytest.fixture
def api_app(container: Container) -> FastAPI:
    return create_api_app(container)


@pytest.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
