"""The worker process's claim loop and scheduler.

``Worker.run(stop)`` is the production loop. ``Worker.run_once()`` and
``Worker.run_until_idle()`` drive the same code step by step for deterministic tests with a
``FrozenClock``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError

from app.modules.governance.context import (
    JobContext,
    LeaseLostError,
    RetryableError,
    ServiceDirectory,
    TerminalError,
    WaitingForUserSignal,
)
from app.modules.governance.domain import retry_delay
from app.modules.governance.queue import JobQueue
from app.modules.governance.registry import JobRegistry, SweepPayload, sweep_idempotency_key
from app.modules.governance.store import ClaimedJob, JobStore
from app.modules.governance.usage import QUOTA_EXCEEDED_CODE, UsageGuard
from app.platform.clock import Clock
from app.platform.correlation import correlation_id_var
from app.platform.db import UnitOfWork
from app.platform.errors import QuotaExceeded

log = logging.getLogger("relaxai.worker")

MAX_SWEEP_LOOKBACK = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class TickResult:
    reaped: int
    promoted: int
    reservations_expired: int
    sweeps_enqueued: int


class Scheduler:
    """Lease reaper, retry promotion, reservation expiry, and periodic sweeps."""

    def __init__(
        self,
        *,
        store: JobStore,
        queue: JobQueue,
        usage: UsageGuard,
        registry: JobRegistry,
        clock: Clock,
        uow: UnitOfWork,
    ) -> None:
        self.store = store
        self._store = store
        self._queue = queue
        self._usage = usage
        self._registry = registry
        self._clock = clock
        self._uow = uow
        self._sweeps_checked_until: datetime | None = None

    async def tick(self) -> TickResult:
        reaped = await self._store.reap_expired_leases()
        promoted = await self._store.promote_due_retries()
        expired = await self._usage.release_expired()
        sweeps = await self._enqueue_due_sweeps()
        return TickResult(reaped, promoted, expired, sweeps)

    async def _enqueue_due_sweeps(self) -> int:
        now = self._clock.now()
        after = self._sweeps_checked_until or (now - timedelta(minutes=1))
        after = max(after, now - MAX_SWEEP_LOOKBACK)
        enqueued = 0
        due = [
            (spec.kind, at)
            for spec in self._registry.sweeps()
            if spec.schedule is not None
            for at in spec.schedule.fire_times(after, now)
        ]
        if due:
            async with self._uow() as tx:
                for kind, at in due:
                    await self._queue.enqueue(
                        tx, kind, SweepPayload(scheduled_for=at),
                        idempotency_key=sweep_idempotency_key(at),
                    )  # fmt: skip
                    enqueued += 1
        self._sweeps_checked_until = now
        return enqueued


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Worker:
    def __init__(
        self,
        *,
        store: JobStore,
        queue: JobQueue,
        usage: UsageGuard,
        registry: JobRegistry,
        clock: Clock,
        uow: UnitOfWork,
        concurrency: int,
        poll_interval: float = 1.0,
        shutdown_grace: float = 30.0,
        owner: str | None = None,
        kinds: list[str] | None = None,
        rng: random.Random | None = None,
        scheduler: Scheduler | None = None,
        services: ServiceDirectory | None = None,
    ) -> None:
        self.scheduler = scheduler or Scheduler(
            store=store, queue=queue, usage=usage, registry=registry, clock=clock, uow=uow
        )
        self._store = store
        self._queue = queue
        self._usage = usage
        self._registry = registry
        self._clock = clock
        self._uow = uow
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._shutdown_grace = shutdown_grace
        self.owner = owner or default_owner()
        self._kinds = kinds
        self._rng = rng or random.Random()  # noqa: S311 - jitter only
        self._services = services or ServiceDirectory()

    # ------------------------------------------------------------------ step-wise (tests)

    async def run_once(self) -> int:
        """One worker step: bring the queue up to date, then claim and run due jobs.

        The scheduler step is what production's scheduler loop does once per poll interval:
        reap expired leases, promote due retries, expire reservations, enqueue due sweeps.
        """
        await self.scheduler.tick()
        claimed: list[ClaimedJob] = []
        while len(claimed) < self._concurrency:
            job = await self._store.claim_one(self.owner, self._kinds)
            if job is None:
                break
            claimed.append(job)
        await asyncio.gather(*(self.execute(job) for job in claimed))
        return len(claimed)

    async def run_until_idle(self, *, max_rounds: int = 1000) -> int:
        total = 0
        for _ in range(max_rounds):
            ran = await self.run_once()
            total += ran
            if ran == 0:
                return total
        raise RuntimeError("run_until_idle did not become idle")

    # ------------------------------------------------------------------ production loop

    async def run(self, stop: asyncio.Event) -> None:
        """Claim and run jobs until ``stop`` is set, then shut down gracefully.

        Shutdown: stop claiming, let in-flight jobs finish for up to ``shutdown_grace``
        seconds, then cancel the rest and release their leases (attempt not counted).
        """
        inflight: set[asyncio.Task[None]] = set()
        scheduler_task = asyncio.create_task(self._scheduler_loop(self.scheduler, stop))
        log.info("worker.started", extra={"count": self._concurrency})
        stop_wait = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                try:
                    while len(inflight) < self._concurrency and not stop.is_set():
                        job = await self._store.claim_one(self.owner, self._kinds)
                        if job is None:
                            break
                        task = asyncio.create_task(self.execute(job))
                        inflight.add(task)
                        task.add_done_callback(inflight.discard)
                except Exception as exc:  # database unavailable etc.: keep the process alive
                    log.error("worker.claim_failed", extra={"exc_type": type(exc).__name__})
                await asyncio.wait(
                    {stop_wait, *inflight},
                    timeout=self._poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
        finally:
            stop_wait.cancel()
            if scheduler_task is not None:
                scheduler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await scheduler_task
            if inflight:
                _, pending = await asyncio.wait(set(inflight), timeout=self._shutdown_grace)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            log.info("worker.stopped")

    async def _scheduler_loop(self, scheduler: Scheduler, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await scheduler.tick()
            except Exception as exc:
                log.error("scheduler.tick_failed", extra={"exc_type": type(exc).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)

    # ------------------------------------------------------------------ one attempt

    async def execute(self, job: ClaimedJob) -> None:
        token = correlation_id_var.set(job.correlation_id)
        started = time.perf_counter()
        extra = {"job_kind": job.kind, "job_id": job.id, "attempt": job.attempts + 1}
        try:
            outcome = await self._attempt(job)
            await self._usage.release_for_job(job.id)
            log.info(
                "job.finished",
                extra={
                    **extra,
                    "error_code": outcome,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        finally:
            correlation_id_var.reset(token)

    async def _attempt(self, job: ClaimedJob) -> str | None:
        """Run the handler once; return the recorded error code (None on success)."""
        spec = self._registry[job.kind]
        if spec.paid and await self._usage.exhausted(spec.meters) is not None:
            await self._store.fail(
                job.id, self.owner, QUOTA_EXCEEDED_CODE, count_attempt=False, retryable=True
            )
            return QUOTA_EXCEEDED_CODE
        try:
            payload = spec.payload.model_validate(job.payload)
        except ValidationError:
            await self._store.fail(job.id, self.owner, "InvalidPayload", count_attempt=False)
            return "InvalidPayload"

        ctx = JobContext(
            job_id=job.id,
            kind=job.kind,
            attempt=job.attempts + 1,
            operation_id=job.operation_id,
            tx_factory=self._uow,
            clock=self._clock,
            usage=self._usage,
            _store=self._store,
            _queue=self._queue,
            _owner=self.owner,
            _services=self._services,
        )
        try:
            await asyncio.wait_for(spec.fn(ctx, payload), timeout=spec.timeout.total_seconds())
        except WaitingForUserSignal:
            return "WaitingForUser"
        except LeaseLostError:
            return "LeaseLost"
        except TerminalError as exc:
            await self._store.fail(job.id, self.owner, exc.code)
            return exc.code
        except QuotaExceeded:
            await self._store.fail(job.id, self.owner, QUOTA_EXCEEDED_CODE, retryable=True)
            return QUOTA_EXCEEDED_CODE
        except RetryableError as exc:
            return await self._retry_or_fail(job, exc.code, exc.retry_after)
        except TimeoutError:
            return await self._retry_or_fail(job, "Timeout")
        except asyncio.CancelledError:
            await asyncio.shield(self._store.release(job.id, self.owner))
            raise
        except Exception as exc:
            log.error(
                "job.unexpected_error",
                extra={"job_kind": job.kind, "job_id": job.id, "exc_type": type(exc).__name__},
            )
            return await self._retry_or_fail(job, "UnexpectedError")
        if not await self._store.succeed(job.id, self.owner):
            return "LeaseLost"
        return None

    async def _retry_or_fail(
        self, job: ClaimedJob, code: str, retry_after: timedelta | None = None
    ) -> str:
        failed_attempts = job.attempts + 1
        if failed_attempts >= job.max_attempts:
            # Transient causes exhausted their attempts: a person may retry later.
            await self._store.fail(job.id, self.owner, code, retryable=True)
            return code
        delay = retry_delay(failed_attempts, rng=self._rng, retry_after=retry_after)
        await self._store.retry(job.id, self.owner, code, self._clock.now() + delay)
        return code
