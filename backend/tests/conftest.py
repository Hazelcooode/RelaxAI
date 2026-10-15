"""Shared test harness.

PostgreSQL comes from ``TEST_DATABASE_URL`` (``postgresql+psycopg://...``) or, when unset,
a disposable Testcontainers ``postgres:16`` (needs Docker). Migrations run once per session.
Each test gets one connection inside an outer transaction that is rolled back afterwards;
the ``container``'s unit of work runs as SAVEPOINTs on that connection.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from alembic import command
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import Table, pool, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.container import Container, build_container
from app.main_api import create_api_app
from app.modules.governance import Priority, Scheduler, Worker
from app.modules.governance.tables import jobs
from app.platform.clock import FrozenClock
from app.platform.db import UnitOfWork
from app.platform.db.migrations import alembic_config
from app.settings import Environment, Settings
from tests.handlers import effects as effects_table
from tests.handlers import registry as test_registry
from tests.worker_factory import worker_for

FROZEN_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

# The prototype fixture personas (conventions §8). Registering them makes the personal-data
# screen treat any appearance of these names as a leak.
FIXTURE_PERSONAS = ("Avery", "Morgan", "Riley", "Jordan", "Sam", "Taylor")


@pytest.fixture(scope="session", autouse=True)
def register_fixture_personas() -> None:
    from app.modules.governance import personal_data_screen

    personal_data_screen.add_names(FIXTURE_PERSONAS)


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
    """Governance services whose writes roll back with the test's transaction.

    Override any adapter in a test with ``dataclasses.replace(container, x=...)``.
    """
    return build_container(
        settings,
        engine=engine,
        uow=UnitOfWork(db_connection),
        clock=frozen_clock,
        registry=test_registry,
    )


@pytest.fixture
async def effects(db_connection: AsyncConnection) -> Table:
    """The handlers' test-only effects table, created inside the test's transaction."""
    await db_connection.run_sync(effects_table.create)
    return effects_table


@pytest.fixture
async def isolated_container(
    settings: Settings, frozen_clock: FrozenClock
) -> AsyncIterator[Container]:
    """A container whose writes really commit, for tests that need concurrent connections
    (two workers claiming at once). Each unit of work opens its own connection."""
    engine = create_async_engine(settings.db.url.get_secret_value(), poolclass=pool.NullPool)
    container = build_container(
        settings,
        engine=engine,
        uow=UnitOfWork(engine),
        clock=frozen_clock,
        registry=test_registry,
    )

    async def clean() -> None:
        # Table names come from this constant tuple only; nothing user-supplied reaches the SQL.
        statements = [
            'DELETE FROM governance."quota_reservations"',
            'DELETE FROM governance."quota_policies"',
            'DELETE FROM governance."usage_meters"',
            'DELETE FROM governance."jobs"',
            'DELETE FROM governance."audit_events"',
        ]
        async with container.uow() as tx:
            for statement in statements:
                await tx.session.execute(text(statement))

    await clean()
    yield container
    await clean()
    await container.aclose()


@pytest.fixture
async def isolated_effects(isolated_container: Container) -> AsyncIterator[Table]:
    async with isolated_container.engine.begin() as conn:
        await conn.run_sync(effects_table.create)
    yield effects_table
    async with isolated_container.engine.begin() as conn:
        await conn.run_sync(effects_table.drop)


@pytest.fixture
def enqueue(container: Container) -> Callable[..., Awaitable[str]]:
    """``await enqueue(kind, payload, key=..., priority=..., run_at=..., operation_id=...)``."""

    async def _enqueue(
        kind: str,
        payload: BaseModel,
        *,
        key: str | None = None,
        priority: Priority | None = None,
        run_at: datetime | None = None,
        operation_id: str | None = None,
    ) -> str:
        async with container.uow() as tx:
            return await container.queue.enqueue(
                tx,
                kind,
                payload,
                idempotency_key=key or str(uuid.uuid4()),
                priority=priority,
                run_at=run_at,
                operation_id=operation_id,
            )

    return _enqueue


@pytest.fixture
def make_worker(container: Container) -> Callable[..., Worker]:
    """``make_worker(owner=..., concurrency=..., kinds=[...]) -> Worker``."""

    def _make(**kwargs: Any) -> Worker:
        return worker_for(container, **kwargs)

    return _make


@pytest.fixture
def scheduler(container: Container) -> Scheduler:
    return Scheduler(
        store=container.store,
        queue=container.queue,
        usage=container.usage,
        registry=container.registry,
        clock=container.clock,
        uow=container.uow,
    )


@pytest.fixture
def read_job(container: Container) -> Callable[[str], Awaitable[Any]]:
    async def _read(job_id: str) -> Any:
        async with container.uow() as tx:
            return (
                (await tx.session.execute(select(jobs).where(jobs.c.id == job_id)))
                .mappings()
                .one()
            )

    return _read


@pytest.fixture
def api_app(container: Container) -> FastAPI:
    return create_api_app(container)


@pytest.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
