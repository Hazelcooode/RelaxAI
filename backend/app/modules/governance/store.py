"""Worker-side job state transitions. Internal to the governance module.

Every transition of a claimed job is guarded by ``lease_owner`` and ``state = 'Running'``,
so a worker whose lease was reaped can never overwrite the outcome of a newer attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, false, func, or_, select, text, update
from sqlalchemy.orm import aliased

from app.modules.governance.domain import TERMINAL_STATES, Destination, JobState
from app.modules.governance.operations import JobEvent, project_job_event
from app.modules.governance.registry import JobRegistry
from app.modules.governance.tables import jobs
from app.platform.clock import Clock
from app.platform.db import UnitOfWork

# Claims are serialised firm-wide with a transaction-scoped advisory lock so the per-kind
# concurrency count and the claim happen atomically across worker processes. The claim
# itself is a short indexed query, so the lock is held for milliseconds.
_CLAIM_LOCK_KEY = 0x52454C41_4A4F4253 & 0x7FFFFFFFFFFFFFFF  # "RELAJOBS"
_TERMINAL = [s.value for s in TERMINAL_STATES]


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    operation_id: str | None
    correlation_id: str | None


class JobStore:
    def __init__(
        self, *, uow: UnitOfWork, clock: Clock, registry: JobRegistry, firm_id: str
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._registry = registry
        self._firm_id = firm_id

    # ------------------------------------------------------------------ claim

    def _claimable(self, kinds: list[str]) -> ColumnElement[bool]:
        clauses: list[ColumnElement[bool]] = []
        for kind in kinds:
            deps = self._registry[kind].depends_on
            if not deps:
                clauses.append(jobs.c.kind == kind)
                continue
            other = aliased(jobs)
            blocking = exists().where(
                other.c.operation_id == jobs.c.operation_id,
                other.c.kind.in_(deps),
                other.c.state.not_in(_TERMINAL),
            )
            clauses.append(and_(jobs.c.kind == kind, or_(jobs.c.operation_id.is_(None), ~blocking)))
        return or_(*clauses) if clauses else false()

    async def claim_one(self, owner: str, kinds: list[str] | None = None) -> ClaimedJob | None:
        """Claim the most urgent due job this worker can run, honouring per-kind concurrency."""
        now = self._clock.now()
        candidates = kinds if kinds is not None else self._registry.kinds()
        async with self._uow() as tx:
            session = tx.session
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CLAIM_LOCK_KEY})
            running: dict[str, int] = dict(
                (
                    await session.execute(
                        select(jobs.c.kind, func.count())
                        .where(jobs.c.state == JobState.RUNNING.value, jobs.c.kind.in_(candidates))
                        .group_by(jobs.c.kind)
                    )
                ).all()
            )
            free = [k for k in candidates if running.get(k, 0) < self._registry[k].concurrency]
            if not free:
                return None
            row = (
                (
                    await session.execute(
                        select(jobs)
                        .where(
                            jobs.c.firm_id == self._firm_id,
                            jobs.c.state == JobState.QUEUED.value,
                            jobs.c.run_at <= now,
                            self._claimable(free),
                        )
                        .order_by(
                            jobs.c.priority.desc(), jobs.c.run_at, jobs.c.created_at, jobs.c.id
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            lease = self._registry[row["kind"]].lease_duration
            await session.execute(
                update(jobs)
                .where(jobs.c.id == row["id"])
                .values(
                    state=JobState.RUNNING.value,
                    lease_owner=owner,
                    lease_expires_at=now + lease,
                    updated_at=now,
                )
            )
            if row["operation_id"] is not None:
                await project_job_event(tx, row["operation_id"], "claimed", now=now)
        return ClaimedJob(
            id=row["id"],
            kind=row["kind"],
            payload=row["payload"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            operation_id=row["operation_id"],
            correlation_id=row["correlation_id"],
        )

    # ------------------------------------------------------------------ owned transitions

    async def _owned_update(
        self, job_id: str, owner: str, event: JobEvent, projection: dict[str, Any], **values: Any
    ) -> bool:
        """Apply ``values`` while this worker still owns the job, and fold ``event`` into the
        job's operation (if any) in the same transaction."""
        now = self._clock.now()
        async with self._uow() as tx:
            row = (
                await tx.session.execute(
                    update(jobs)
                    .where(
                        jobs.c.id == job_id,
                        jobs.c.lease_owner == owner,
                        jobs.c.state == JobState.RUNNING.value,
                    )
                    .values(updated_at=now, **values)
                    .returning(jobs.c.id, jobs.c.operation_id)
                )
            ).one_or_none()
            if row is None:
                return False
            if row.operation_id is not None:
                await project_job_event(tx, row.operation_id, event, now=now, **projection)
            return True

    async def succeed(self, job_id: str, owner: str) -> bool:
        return await self._owned_update(
            job_id, owner, "succeeded", {},
            state=JobState.SUCCEEDED.value, completed_at=self._clock.now(),
            lease_owner=None, lease_expires_at=None, last_error_code=None,
        )  # fmt: skip

    async def fail(
        self,
        job_id: str,
        owner: str,
        code: str,
        *,
        count_attempt: bool = True,
        retryable: bool = False,
    ) -> bool:
        """Fail the job. ``count_attempt=False`` for a refusal before the handler ran
        (quota pre-check, invalid payload): nothing was attempted, so nothing is counted.
        ``retryable`` marks the operation's failure as one a user may retry."""
        return await self._owned_update(
            job_id, owner, "failed", {"error_code": code, "retryable": retryable},
            state=JobState.FAILED.value, completed_at=self._clock.now(),
            attempts=jobs.c.attempts + (1 if count_attempt else 0), last_error_code=code,
            lease_owner=None, lease_expires_at=None,
        )  # fmt: skip

    async def retry(self, job_id: str, owner: str, code: str, run_at: datetime) -> bool:
        return await self._owned_update(
            job_id, owner, "retry", {"run_at": run_at},
            state=JobState.RETRYING.value, run_at=run_at,
            attempts=jobs.c.attempts + 1, last_error_code=code,
            lease_owner=None, lease_expires_at=None,
        )  # fmt: skip

    async def release(self, job_id: str, owner: str) -> bool:
        """Give the job back without counting an attempt (graceful shutdown)."""
        return await self._owned_update(
            job_id, owner, "requeued", {},
            state=JobState.QUEUED.value, run_at=self._clock.now(),
            lease_owner=None, lease_expires_at=None,
        )  # fmt: skip

    async def report(
        self,
        job_id: str,
        owner: str,
        stage: str,
        done: int | None,
        total: int | None,
        unit: str | None,
    ) -> bool:
        return await self._owned_update(
            job_id, owner, "report", {"stage": stage, "done": done, "total": total, "unit": unit},
            progress_stage=stage, progress_done=done, progress_total=total, progress_unit=unit,
        )  # fmt: skip

    async def set_waiting(
        self, job_id: str, owner: str, action: str, destination: Destination
    ) -> bool:
        stored = destination.model_dump(mode="json")
        return await self._owned_update(
            job_id, owner, "waiting", {"waiting_action": action, "destination": stored},
            state=JobState.WAITING_FOR_USER.value,
            waiting_action=action, waiting_destination=stored,
            lease_owner=None, lease_expires_at=None,
        )  # fmt: skip

    # ------------------------------------------------------------------ scheduler

    async def promote_due_retries(self) -> int:
        now = self._clock.now()
        async with self._uow() as tx:
            result = await tx.session.execute(
                update(jobs)
                .where(jobs.c.state == JobState.RETRYING.value, jobs.c.run_at <= now)
                .values(state=JobState.QUEUED.value, updated_at=now)
                .returning(jobs.c.id)
            )
            return len(result.all())

    async def reap_expired_leases(self) -> int:
        """Expired ``Running`` jobs go back to ``Retrying`` without counting an attempt."""
        now = self._clock.now()
        async with self._uow() as tx:
            result = await tx.session.execute(
                update(jobs)
                .where(jobs.c.state == JobState.RUNNING.value, jobs.c.lease_expires_at < now)
                .values(
                    state=JobState.RETRYING.value,
                    run_at=now,
                    last_error_code="LeaseExpired",
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
                .returning(jobs.c.id, jobs.c.operation_id)
            )
            reaped = result.all()
            for row in reaped:
                if row.operation_id is not None:
                    await project_job_event(tx, row.operation_id, "retry", now=now, run_at=now)
            return len(reaped)
