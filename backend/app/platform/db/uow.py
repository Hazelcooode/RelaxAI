"""Unit of work: one PostgreSQL transaction per ``async with uow() as tx`` (conventions §3.4).

Everything that must commit together — domain change, audit event, job row, notification —
writes through the same ``tx``. The block commits when it exits normally and rolls back
when it raises.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class Tx:
    """Handle on the open transaction. Repositories use ``tx.session``."""

    session: AsyncSession


class UnitOfWork:
    def __init__(self, bind: AsyncEngine | AsyncConnection) -> None:
        # Bound to a connection that already has an outer transaction (tests), each unit of
        # work becomes a SAVEPOINT, so the outer rollback undoes everything.
        self._sessions = async_sessionmaker(
            bind=bind,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[Tx]:
        async with self._sessions() as session, session.begin():
            yield Tx(session=session)
