"""``JobQueue``: the producer-side interface of the durable job queue (conventions §4.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.modules.governance.domain import JobState, Priority, check_job_payload
from app.modules.governance.registry import JobRegistry
from app.modules.governance.tables import jobs
from app.platform.clock import Clock
from app.platform.correlation import current_correlation_id
from app.platform.db import Tx, UnitOfWork
from app.platform.errors import InvalidTransition, NotAvailable
from app.platform.ids import new_id

_ACTIVE = (JobState.QUEUED.value, JobState.RETRYING.value)


@dataclass(frozen=True, slots=True)
class QueueStat:
    kind: str
    depth: int  # Queued + Retrying
    running: int
    waiting_for_user: int
    oldest_age_seconds: int  # age of the oldest Queued/Retrying job; 0 when empty
    failed_24h: int


class JobQueue:
    def __init__(self, *, uow: UnitOfWork, clock: Clock, registry: JobRegistry, firm_id: str) -> None:
        self._uow = uow
        self._clock = clock
        self._registry = registry
        self._firm_id = firm_id

    async def enqueue(
        self,
        tx: Tx,
        kind: str,
        payload: BaseModel,
        *,
        idempotency_key: str,
        operation_id: str | None = None,
        run_at: datetime | None = None,
        priority: Priority | None = None,
    ) -> str:
        """Insert a ``Queued`` job in the caller's transaction and return its id.

        Re-enqueueing the same ``(kind, idempotency_key)`` returns the existing job's id and
        creates no new work. ``priority`` defaults to the handler's registered priority.
        Raises ``ValueError`` for an unknown kind, a payload of the wrong type, or a payload
        that could carry content or personal data.
        """
        spec = self._registry[kind]
        if not isinstance(payload, spec.payload):
            raise ValueError(f"{kind} expects payload {spec.payload.__name__}")
        check_job_payload(payload)
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency_key must be 1-200 characters")

        now = self._clock.now()
        job_id = new_id("job")
        stmt = (
            insert(jobs)
            .values(
                id=job_id,
                firm_id=self._firm_id,
                kind=kind,
                payload=payload.model_dump(mode="json"),
                state=JobState.QUEUED.value,
                priority=int(priority if priority is not None else spec.priority),
                attempts=0,
                max_attempts=spec.max_attempts,
                run_at=run_at or now,
                idempotency_key=idempotency_key,
                operation_id=operation_id,
                correlation_id=current_correlation_id(),
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["kind", "idempotency_key"])
            .returning(jobs.c.id)
        )
        inserted: str | None = (await tx.session.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            return inserted
        existing = await tx.session.execute(
            select(jobs.c.id).where(jobs.c.kind == kind, jobs.c.idempotency_key == idempotency_key)
        )
        return str(existing.scalar_one())

    async def resume(self, tx: Tx, job_id: str) -> None:
        """Move a ``WaitingForUser`` job back to ``Queued`` (runs as soon as a worker is free)."""
        now = self._clock.now()
        result = await tx.session.execute(
            update(jobs)
            .where(jobs.c.id == job_id, jobs.c.state == JobState.WAITING_FOR_USER.value)
            .values(
                state=JobState.QUEUED.value,
                run_at=now,
                waiting_action=None,
                waiting_destination=None,
                updated_at=now,
            )
            .returning(jobs.c.id)
        )
        if result.scalar_one_or_none() is None:
            state = (
                await tx.session.execute(select(jobs.c.state).where(jobs.c.id == job_id))
            ).scalar_one_or_none()
            if state is None:
                raise NotAvailable()
            raise InvalidTransition(allowed_transitions=[])

    async def stats(self) -> list[QueueStat]:
        """Content-free per-kind queue health for ``/operator/health`` (NFR-13)."""
        now = self._clock.now()
        day_ago = now - timedelta(hours=24)
        state = jobs.c.state
        query = (
            select(
                jobs.c.kind,
                func.count().filter(state.in_(_ACTIVE)),
                func.count().filter(state == JobState.RUNNING.value),
                func.count().filter(state == JobState.WAITING_FOR_USER.value),
                func.min(jobs.c.created_at).filter(state.in_(_ACTIVE)),
                func.count().filter(state == JobState.FAILED.value, jobs.c.completed_at >= day_ago),
            )
            .where(jobs.c.firm_id == self._firm_id)
            .group_by(jobs.c.kind)
        )
        async with self._uow() as tx:
            rows = (await tx.session.execute(query)).all()
        by_kind = {
            kind: QueueStat(
                kind=kind,
                depth=depth,
                running=running,
                waiting_for_user=waiting,
                oldest_age_seconds=max(0, int((now - oldest).total_seconds())) if oldest else 0,
                failed_24h=failed,
            )
            for kind, depth, running, waiting, oldest, failed in rows
        }
        for kind in self._registry.kinds():
            by_kind.setdefault(kind, QueueStat(kind, 0, 0, 0, 0, 0))
        return [by_kind[k] for k in sorted(by_kind)]
