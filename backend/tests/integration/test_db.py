"""Unit of work, optimistic updates, and migrations against real PostgreSQL."""

from __future__ import annotations

import pytest
from alembic import command
from pydantic import BaseModel
from sqlalchemy import Column, Integer, MetaData, RowMapping, String, Table, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.container import Container
from app.platform.db import SCHEMAS, metadata_for, optimistic_update
from app.platform.db.migrations import alembic_config, expected_heads
from app.platform.errors import NotAvailable, VersionConflict

# Test-only table, created inside the per-test transaction, so it is rolled back.
_probe_metadata = MetaData(schema="governance")
probe = Table(
    "t01_probe",
    _probe_metadata,
    Column("id", String, primary_key=True),
    Column("title", String, nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
)


class ProbeView(BaseModel):
    id: str
    title: str
    version: int


@pytest.fixture
async def probe_table(db_connection: AsyncConnection) -> Table:
    await db_connection.run_sync(_probe_metadata.create_all)
    await db_connection.execute(insert(probe).values(id="tsk_1", title="Draft", version=1))
    return probe


async def test_schemas_exist(db_connection: AsyncConnection) -> None:
    rows = await db_connection.execute(
        text("SELECT schema_name FROM information_schema.schemata WHERE schema_name = ANY(:s)"),
        {"s": list(SCHEMAS)},
    )
    assert {r[0] for r in rows} == set(SCHEMAS)


async def test_metadata_per_schema_has_naming_convention() -> None:
    meta = metadata_for("projects")
    assert meta.schema == "projects"
    assert meta.naming_convention["pk"] == "pk_%(table_name)s"
    with pytest.raises(ValueError, match="unknown schema"):
        metadata_for("public")


async def test_optimistic_update_increments_version(
    container: Container, probe_table: Table
) -> None:
    async with container.uow() as tx:
        row = await optimistic_update(tx, probe_table, "tsk_1", 1, {"title": "Ready"})
    assert row["version"] == 2
    assert row["title"] == "Ready"

    async with container.uow() as tx:
        row = await optimistic_update(tx, probe_table, "tsk_1", 2, {"title": "Done"})
    assert row["version"] == 3


async def test_optimistic_update_stale_version_conflicts(
    container: Container, probe_table: Table
) -> None:
    async with container.uow() as tx:
        await optimistic_update(tx, probe_table, "tsk_1", 1, {"title": "Theirs"})

    def to_view(r: RowMapping) -> ProbeView:
        return ProbeView.model_validate(dict(r))

    with pytest.raises(VersionConflict) as caught:
        async with container.uow() as tx:
            await optimistic_update(
                tx, probe_table, "tsk_1", 1, {"title": "Mine"}, to_current=to_view
            )
    assert caught.value.current == ProbeView(id="tsk_1", title="Theirs", version=2)
    assert caught.value.row is not None
    assert caught.value.row["version"] == 2

    async with container.uow() as tx:
        stored = (await tx.session.execute(select(probe_table))).mappings().one()
    assert stored["title"] == "Theirs"
    assert stored["version"] == 2


async def test_optimistic_update_missing_row(container: Container, probe_table: Table) -> None:
    with pytest.raises(NotAvailable):
        async with container.uow() as tx:
            await optimistic_update(tx, probe_table, "tsk_nope", 1, {"title": "x"})


async def test_optimistic_update_refuses_version_and_id(
    container: Container, probe_table: Table
) -> None:
    with pytest.raises(ValueError, match="cannot set"):
        async with container.uow() as tx:
            await optimistic_update(tx, probe_table, "tsk_1", 1, {"version": 9})


async def test_uow_rolls_back_on_error(container: Container, probe_table: Table) -> None:
    class BoomError(Exception):
        pass

    async def insert_then_fail() -> None:
        async with container.uow() as tx:
            await tx.session.execute(insert(probe_table).values(id="tsk_2", title="Temp"))
            raise BoomError

    with pytest.raises(BoomError):
        await insert_then_fail()

    async with container.uow() as tx:
        ids = (await tx.session.execute(select(probe_table.c.id))).scalars().all()
    assert ids == ["tsk_1"]


def test_migrations_are_reversible(migrated_database: str) -> None:
    """Downgrade to base and back to head; leaves the database migrated for other tests."""
    config = alembic_config(migrated_database)
    config.attributes["skip_logging"] = True
    command.downgrade(config, "base")
    command.upgrade(config, "head")


async def test_single_alembic_head(engine: AsyncEngine) -> None:
    assert expected_heads() == {"0001_platform"}
    async with engine.connect() as conn:
        applied = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars()
        assert set(applied) == {"0001_platform"}
