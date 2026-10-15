"""Optimistic concurrency for versioned rows (contract §1.2 ``If-Match``)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel
from sqlalchemy import RowMapping, Table, select, update

from app.platform.db.uow import Tx
from app.platform.errors import NotAvailable, VersionConflict


async def optimistic_update(
    tx: Tx,
    table: Table,
    id: str,
    version: int,
    values: Mapping[str, Any],
    *,
    id_column: str = "id",
    to_current: Callable[[RowMapping], BaseModel] | None = None,
) -> RowMapping:
    """Apply ``values`` only if the row is still at ``version``; bump ``version`` by one.

    Returns the updated row. Raises ``NotAvailable`` when the row does not exist and
    ``VersionConflict`` when it exists at another version — with ``current`` built by
    ``to_current`` when given, and the authoritative row always on ``.row``.
    """
    forbidden = {id_column, "version"} & values.keys()
    if forbidden:
        raise ValueError(f"optimistic_update cannot set {sorted(forbidden)}")

    key = table.c[id_column]
    stmt = (
        update(table)
        .where(key == id, table.c.version == version)
        .values({**values, "version": table.c.version + 1})
        .returning(*table.c)
    )
    updated = (await tx.session.execute(stmt)).mappings().one_or_none()
    if updated is not None:
        return updated

    current = (await tx.session.execute(select(table).where(key == id))).mappings().one_or_none()
    if current is None:
        raise NotAvailable()
    raise VersionConflict(to_current(current) if to_current else None, row=current)
