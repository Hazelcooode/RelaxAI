"""``UndoService``: short-lived undo tokens for reversible commands (SYS-16, contract §1.6).

The owning module issues a token inside its command transaction with a content-free
compensating payload, and registers a ``Compensator`` for its ``command_kind``. ``undo``
runs the compensation as a normal command *against current state*: the compensator
re-checks authorization, lifecycle, and versions itself, returns the current resource, and
never deletes domain records. Consuming the token and compensating commit together.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy import delete, select, update

from app.modules.governance.tables import undo_tokens
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork
from app.platform.errors import AppError, ErrorCode, NotAvailable
from app.platform.ids import new_id
from app.platform.schema import CamelModel

UNDO_WINDOW = timedelta(seconds=8)
_RETAIN_CONSUMED = timedelta(hours=1)


class UndoRef(CamelModel):
    token: str
    expires_at: datetime


class UndoActor(Protocol):
    @property
    def user_id(self) -> str | None: ...


class Compensator(Protocol):
    async def compensate(self, actor: Any, payload: Mapping[str, Any]) -> BaseModel:
        """Reverse the command against current state; return the current resource."""
        ...

    async def current(self, actor: Any, payload: Mapping[str, Any]) -> BaseModel:
        """The resource as it stands now (for consumed or expired tokens)."""
        ...


class UndoService:
    def __init__(self, *, uow: UnitOfWork, clock: Clock, window: timedelta = UNDO_WINDOW) -> None:
        self._uow = uow
        self._clock = clock
        self._window = window
        self._compensators: dict[str, Compensator] = {}

    def register(self, command_kind: str, compensator: Compensator) -> None:
        if command_kind in self._compensators:
            raise ValueError(f"undo compensator already registered: {command_kind}")
        self._compensators[command_kind] = compensator

    async def issue(
        self,
        tx: Tx,
        *,
        actor_id: str,
        command_kind: str,
        compensating_payload: Mapping[str, Any],
        result_payload: Mapping[str, Any] | None = None,
    ) -> UndoRef:
        if command_kind not in self._compensators:
            raise ValueError(f"no undo compensator for {command_kind}")
        now = self._clock.now()
        token = new_id("und")
        expires_at = now + self._window
        await tx.session.execute(
            undo_tokens.insert().values(
                token=token,
                actor_id=actor_id,
                command_kind=command_kind,
                compensating_payload=dict(compensating_payload),
                result_payload=dict(result_payload) if result_payload is not None else None,
                created_at=now,
                expires_at=expires_at,
            )
        )
        return UndoRef(token=token, expires_at=expires_at)

    async def undo(self, actor: UndoActor, token: str) -> BaseModel:
        """Idempotent: a consumed token returns the current resource without acting again.
        Another actor's token is ``404 NOT_AVAILABLE``; an expired one ``409 UNDO_EXPIRED``
        with ``current``."""
        if actor.user_id is None:
            raise NotAvailable()
        t = undo_tokens.c
        async with self._uow.ambient() as tx:
            row = (
                (
                    await tx.session.execute(
                        select(undo_tokens)
                        .where(t.token == token, t.actor_id == actor.user_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotAvailable()
            compensator = self._compensators[row["command_kind"]]
            payload = row["compensating_payload"]
            if row["consumed_at"] is not None:
                return await compensator.current(actor, payload)
            now = self._clock.now()
            if now >= row["expires_at"]:
                raise AppError(
                    ErrorCode.UNDO_EXPIRED, current=await compensator.current(actor, payload)
                )
            await tx.session.execute(
                update(undo_tokens).where(t.token == token).values(consumed_at=now)
            )
            return await compensator.compensate(actor, payload)

    async def purge_expired(self) -> int:
        async with self._uow() as tx:
            result = await tx.session.execute(
                delete(undo_tokens)
                .where(undo_tokens.c.expires_at <= self._clock.now() - _RETAIN_CONSUMED)
                .returning(undo_tokens.c.token)
            )
            return len(result.all())
