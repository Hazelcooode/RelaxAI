"""``IdempotencyKeys``: stored responses for **[idem]** commands (SYS-07, SYS-20).

A key row is written inside the command's own transaction, together with the response it
produced, so a failed command stores nothing (a retry is a fresh attempt) and a response is
visible to replays only once the command has committed. A concurrent duplicate blocks on
the primary key until the first commits, then replays its response. Rows store the request
hash and the response only — never the request body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.modules.governance.tables import idempotency_keys
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork
from app.platform.errors import AppError, ErrorCode

KEY_TTL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: Any


class IdempotencyKeys:
    def __init__(self, *, uow: UnitOfWork, clock: Clock, ttl: timedelta = KEY_TTL) -> None:
        self._uow = uow
        self._clock = clock
        self._ttl = ttl

    async def begin(
        self, tx: Tx, *, actor_id: str, route: str, key: str, request_hash: str
    ) -> StoredResponse | None:
        """Claim ``key`` for this request, or return the response to replay.

        Raises ``422 IDEMPOTENCY_KEY_REUSED`` when the key was used for a different request.
        """
        k = idempotency_keys.c
        now = self._clock.now()
        where = (k.actor_id == actor_id, k.route_template == route, k.idem_key == key)
        # An expired key is forgotten: the request is a fresh attempt.
        await tx.session.execute(
            delete(idempotency_keys).where(*where, k.created_at <= now - self._ttl)
        )
        claimed = (
            await tx.session.execute(
                insert(idempotency_keys)
                .values(
                    actor_id=actor_id,
                    route_template=route,
                    idem_key=key,
                    request_hash=request_hash,
                    created_at=now,
                )
                .on_conflict_do_nothing()
                .returning(k.idem_key)
            )
        ).scalar_one_or_none()
        if claimed is not None:
            return None
        row = (await tx.session.execute(select(idempotency_keys).where(*where))).mappings().one()
        if row["request_hash"] != request_hash or row["response_status"] is None:
            raise AppError(ErrorCode.IDEMPOTENCY_KEY_REUSED)
        return StoredResponse(status=row["response_status"], body=row["response_body"])

    async def complete(
        self,
        tx: Tx,
        *,
        actor_id: str,
        route: str,
        key: str,
        status: int,
        body: Any,
        operation_id: str | None = None,
    ) -> None:
        k = idempotency_keys.c
        await tx.session.execute(
            update(idempotency_keys)
            .where(k.actor_id == actor_id, k.route_template == route, k.idem_key == key)
            .values(response_status=status, response_body=body, operation_id=operation_id)
        )

    async def purge_expired(self) -> int:
        """Retention: forget keys older than the 24-hour window."""
        async with self._uow() as tx:
            result = await tx.session.execute(
                delete(idempotency_keys)
                .where(idempotency_keys.c.created_at <= self._clock.now() - self._ttl)
                .returning(idempotency_keys.c.idem_key)
            )
            return len(result.all())
