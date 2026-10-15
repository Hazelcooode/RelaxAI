"""Unit of work: one PostgreSQL transaction per ``async with uow() as tx`` (conventions §3.4).

Everything that must commit together — domain change, audit event, job row, notification —
writes through the same ``tx``. The block commits when it exits normally and rolls back
when it raises.

``uow.ambient()`` opens a transaction that every ``uow()`` entered in the same task joins as
a SAVEPOINT until it closes. Request-level conventions use it to make a service's own units
of work part of one larger commit without changing the service: the ``Idempotency-Key`` row
and the command it records (SYS-20), or an undo token's consumption and its compensation.
Code outside an ambient block is unaffected, so the worker's independent commits (progress
reports, lease transitions) keep their semantics. Do not run concurrent tasks against one
ambient transaction: they would share a session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class Tx:
    """Handle on the open transaction. Repositories use ``tx.session``."""

    session: AsyncSession


_ambient: ContextVar[tuple[UnitOfWork, Tx] | None] = ContextVar("ambient_tx", default=None)


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

    def _joined(self) -> Tx | None:
        current = _ambient.get()
        return current[1] if current is not None and current[0] is self else None

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[Tx]:
        joined = self._joined()
        if joined is not None:
            async with joined.session.begin_nested():
                yield joined
            return
        async with self._sessions() as session, session.begin():
            yield Tx(session=session)

    @asynccontextmanager
    async def ambient(self) -> AsyncIterator[Tx]:
        """Open (or join) the task's ambient transaction; see the module docstring."""
        if self._joined() is not None:
            async with self() as tx:
                yield tx
            return
        async with self._sessions() as session, session.begin():
            tx = Tx(session=session)
            token = _ambient.set((self, tx))
            try:
                yield tx
            finally:
                _ambient.reset(token)
