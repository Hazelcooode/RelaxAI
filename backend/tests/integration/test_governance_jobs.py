"""Job queue behaviour against real PostgreSQL (T02)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import insert, select

from app.container import Container, build_container
from app.platform.clock import FrozenClock
from app.modules.governance import (
    Worker,
    JobState,
    Meter,
    Priority,
    QuotaPeriod,
    Scheduler,
    SweepPayload,
)
from app.modules.governance.registry import DEFAULT_REGISTRY, JobRegistry, sweep_idempotency_key
from app.modules.governance.tables import jobs, quota_policies
from app.modules.governance.worker import TickResult
from tests.handlers import (
    KIND_CHAIN_A,
    KIND_CHAIN_B,
    KIND_FLAKY,
    KIND_NEXT,
    KIND_NOOP,
    KIND_OK,
    KIND_PAID,
    KIND_SLOW,
    KIND_TERMINAL,
    KIND_WAITING,
    ChainPayload,
    FlakyPayload,
    NoopPayload,
    OkPayload,
    PaidPayload,
    SlowPayload,
    TerminalPayload,
    WaitingPayload,
    count_effects,
    effects,
)
from tests.worker_factory import worker_for

Enqueue = Callable[..., Awaitable[str]]


# --------------------------------------------------------------------------- atomicity


async def test_enqueue_in_a_rolled_back_transaction_leaves_no_job(
    container: Container, enqueue: Enqueue, read_job: Callable[[str], Awaitable[Any]]
) -> None:
    class BoomError(Exception):
        pass

    async def enqueue_then_fail() -> None:
        async with container.uow() as tx:
            await container.queue.enqueue(tx, KIND_OK, OkPayload(), idempotency_key="rollback-1")
            raise BoomError

    with pytest.raises(BoomError):
        await enqueue_then_fail()

    async with container.uow() as tx:
        rows = (await tx.session.execute(select(jobs.c.id))).scalars().all()
    assert rows == []


async def test_domain_write_and_job_publish_together(
    container: Container, enqueue: Enqueue, effects: Any
) -> None:
    async def commit_both() -> str:
        async with container.uow() as tx:
            await tx.session.execute(
                insert(effects).values(id="atomic", kind=KIND_OK, value=0, at=container.clock.now())
            )
            return await container.queue.enqueue(
                tx, KIND_OK, OkPayload(), idempotency_key="atomic-1"
            )

    async def fail_both() -> None:
        async with container.uow() as tx:
            await tx.session.execute(
                insert(effects).values(id="gone", kind=KIND_OK, value=0, at=container.clock.now())
            )
            await container.queue.enqueue(tx, KIND_OK, OkPayload(), idempotency_key="atomic-2")
            raise RuntimeError

    await commit_both()
    with pytest.raises(RuntimeError):
        await fail_both()

    async with container.uow() as tx:
        assert await count_effects(tx.session, "atomic") == 1
        assert await count_effects(tx.session, "gone") == 0
        assert (await tx.session.execute(select(jobs.c.id))).scalars().all() != []


# --------------------------------------------------------------------------- idempotency


async def test_duplicate_idempotency_key_returns_the_same_job(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker], effects: Any
) -> None:
    first = await enqueue(KIND_OK, OkPayload(effect_id="once"), key="same-key")
    second = await enqueue(KIND_OK, OkPayload(effect_id="once"), key="same-key")
    assert first == second

    worker = make_worker()
    assert await worker.run_until_idle() == 1
    async with container.uow() as tx:
        assert await count_effects(tx.session, "once") == 1
        assert (await tx.session.execute(select(jobs.c.state))).scalars().all() == ["Succeeded"]


async def test_same_key_different_kind_creates_a_second_job(
    container: Container, enqueue: Enqueue
) -> None:
    first = await enqueue(KIND_OK, OkPayload(), key="shared")
    second = await enqueue(KIND_NOOP, NoopPayload(), key="shared")
    assert first != second


async def test_unknown_kind_and_mismatched_payload_are_programming_errors(
    container: Container
) -> None:
    with pytest.raises(ValueError, match="no handler registered"):
        async with container.uow() as tx:
            await container.queue.enqueue(tx, "nope.nope", NoopPayload(), idempotency_key="k")
    with pytest.raises(ValueError, match="expects payload"):
        async with container.uow() as tx:
            await container.queue.enqueue(tx, KIND_OK, NoopPayload(), idempotency_key="k")


# --------------------------------------------------------------------------- claiming


async def test_two_workers_run_one_job_once(
    isolated_container: Container, isolated_effects: Any
) -> None:
    async with isolated_container.uow() as tx:
        await isolated_container.queue.enqueue(
            tx, KIND_OK, OkPayload(effect_id="shared", steps=2), idempotency_key="two-workers"
        )

    first = worker_for(isolated_container, owner="worker-a")
    second = worker_for(isolated_container, owner="worker-b")
    claimed = await asyncio.gather(first.run_once(), second.run_once())
    assert sum(claimed) == 1

    async with isolated_container.uow() as tx:
        assert await count_effects(tx.session, "shared") == 1
        state = (await tx.session.execute(select(jobs.c.state))).scalar_one()
    assert state == JobState.SUCCEEDED.value


async def test_priority_orders_claiming(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker], effects: Any
) -> None:
    await enqueue(KIND_OK, OkPayload(effect_id="normal"), key="n", priority=Priority.NORMAL)
    await enqueue(KIND_OK, OkPayload(effect_id="high"), key="h", priority=Priority.HIGH)

    worker = make_worker(concurrency=1)
    assert await worker.run_once() == 1
    async with container.uow() as tx:
        assert await count_effects(tx.session, "high") == 1
        assert await count_effects(tx.session, "normal") == 0

    await worker.run_until_idle()
    async with container.uow() as tx:
        assert await count_effects(tx.session, "normal") == 1


async def test_per_kind_concurrency_is_respected(
    isolated_container: Container, isolated_effects: Any
) -> None:
    async with isolated_container.uow() as tx:
        for index in range(3):
            await isolated_container.queue.enqueue(
                tx,
                KIND_SLOW,
                SlowPayload(effect_id=f"slow-{index}", seconds=0.2),
                idempotency_key=f"slow-{index}",
            )
    worker = worker_for(isolated_container, concurrency=5)
    started = asyncio.get_running_loop().time()
    assert await worker.run_until_idle() == 3
    elapsed = asyncio.get_running_loop().time() - started
    # concurrency=1 for KIND_SLOW: three 0.2 s jobs take at least 0.6 s, not 0.2 s.
    assert elapsed >= 0.55

    async with isolated_container.uow() as tx:
        done = (
            (
                await tx.session.execute(
                    select(jobs.c.state, jobs.c.lease_owner).where(jobs.c.kind == KIND_SLOW)
                )
            )
            .all()
        )
    assert {row.state for row in done} == {JobState.SUCCEEDED.value}
    assert {row.lease_owner for row in done} == {None}


async def test_worker_concurrency_runs_different_kinds_together(
    isolated_container: Container, isolated_effects: Any
) -> None:
    async with isolated_container.uow() as tx:
        for index in range(3):
            await isolated_container.queue.enqueue(
                tx,
                KIND_SLOW,
                SlowPayload(effect_id=f"par-{index}", seconds=0.2),
                idempotency_key=f"par-{index}",
            )
    # concurrency=1 per kind, but this worker runs kinds sequentially, so 3 x 0.2 s.
    worker = worker_for(isolated_container, concurrency=1)
    started = asyncio.get_running_loop().time()
    await worker.run_until_idle()
    assert asyncio.get_running_loop().time() - started >= 0.55


async def test_run_at_in_the_future_is_not_claimed_until_due(
    container: Container,
    enqueue: Enqueue,
    make_worker: Callable[..., Worker],
    frozen_clock: FrozenClock,
) -> None:
    await enqueue(
        KIND_OK, OkPayload(effect_id="later"), key="later",
        run_at=frozen_clock.now() + timedelta(minutes=5),
    )  # fmt: skip
    worker = make_worker()
    assert await worker.run_once() == 0

    frozen_clock.advance(timedelta(minutes=5))
    assert await worker.run_once() == 1


async def test_kind_filter_claims_only_registered_kinds(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker], effects: Any
) -> None:
    await enqueue(KIND_OK, OkPayload(effect_id="filtered-ok"), key="a")
    await enqueue(KIND_NOOP, NoopPayload(), key="b")
    worker = make_worker(kinds=[KIND_NOOP])
    assert await worker.run_until_idle() == 1
    async with container.uow() as tx:
        states = dict((await tx.session.execute(select(jobs.c.kind, jobs.c.state))).all())
    assert states == {KIND_OK: JobState.QUEUED.value, KIND_NOOP: JobState.SUCCEEDED.value}


# --------------------------------------------------------------------------- retries


async def test_retry_then_success(
    container: Container,
    enqueue: Enqueue,
    make_worker: Callable[..., Worker],
    effects: Any,
    frozen_clock: FrozenClock,
) -> None:
    job_id = await enqueue(KIND_FLAKY, FlakyPayload(effect_id="flaky", fail_times=2), key="f")
    worker = make_worker()
    assert await worker.run_once() == 1
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    assert row["state"] == JobState.RETRYING.value
    assert row["attempts"] == 1
    assert row["last_error_code"] == "FlakyUpstream"
    assert row["run_at"] > container.clock.now()

    # Backoff progression: the second attempt waits longer than the first.
    first_delay = row["run_at"] - container.clock.now()
    frozen_clock.advance(first_delay)
    await worker.run_once()
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    second_delay = row["run_at"] - container.clock.now()
    assert second_delay > first_delay

    frozen_clock.advance(second_delay)
    await worker.run_once()
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
        assert await count_effects(tx.session, "flaky") == 1
    assert row["state"] == JobState.SUCCEEDED.value
    assert row["attempts"] == 2


async def test_upstream_retry_after_is_honoured(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker], effects: Any
) -> None:
    job_id = await enqueue(
        KIND_FLAKY, FlakyPayload(effect_id="throttled", fail_times=1, retry_after_seconds=45), key="t"
    )
    worker = make_worker()
    await worker.run_once()
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    assert row["run_at"] - container.clock.now() == timedelta(seconds=45)


async def test_failure_after_max_attempts(
    container: Container,
    enqueue: Enqueue,
    make_worker: Callable[..., Worker],
    frozen_clock: FrozenClock,
) -> None:
    job_id = await enqueue(KIND_FLAKY, FlakyPayload(effect_id="never", fail_times=99), key="never")
    worker = make_worker()
    for _ in range(3):
        await worker.run_once()
        frozen_clock.advance(timedelta(minutes=10))
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    assert row["state"] == JobState.FAILED.value
    assert row["attempts"] == 3
    assert row["last_error_code"] == "FlakyUpstream"
    assert row["completed_at"] is not None
    assert await worker.run_once() == 0


async def test_terminal_error_stops_retrying(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker]
) -> None:
    job_id = await enqueue(KIND_TERMINAL, TerminalPayload(code="UnsupportedFormat"), key="term")
    worker = make_worker()
    assert await worker.run_once() == 1
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    assert row["state"] == JobState.FAILED.value
    assert row["attempts"] == 1  # terminal on the first attempt, max_attempts was 5
    assert row["last_error_code"] == "UnsupportedFormat"


async def test_unexpected_exception_retries_then_fails(
    container: Container, frozen_clock: FrozenClock
) -> None:
    registry = JobRegistry()

    class Payload(BaseModel):
        effect_id: str = "boom"

    @registry.handler(
        "test.boom", payload=Payload, max_attempts=2, concurrency=1,
        priority=Priority.NORMAL, timeout=timedelta(seconds=5),
    )  # fmt: skip
    async def boom(ctx: Any, payload: Payload) -> None:
        raise RuntimeError("database said: secret-name@example.com")

    # The queue and store capture the registry at build time, so build a container over it.
    container = build_container(
        container.settings, engine=container.engine, uow=container.uow,
        clock=frozen_clock, registry=registry,
    )  # fmt: skip
    async with container.uow() as tx:
        await container.queue.enqueue(tx, "test.boom", Payload(), idempotency_key="boom")
    worker = worker_for(container)
    await worker.run_once()
    frozen_clock.advance(timedelta(minutes=10))
    await worker.run_once()
    async with container.uow() as tx:
        row = (await tx.session.execute(select(jobs))).mappings().one()
    assert row["state"] == JobState.FAILED.value
    assert row["last_error_code"] == "UnexpectedError"  # never the exception message
    assert "secret-name@example.com" not in str(row["last_error_code"])


# --------------------------------------------------------------------------- leases


async def test_expired_lease_is_requeued_and_a_second_attempt_completes(
    isolated_container: Container, isolated_effects: Any, frozen_clock: FrozenClock
) -> None:
    async with isolated_container.uow() as tx:
        job_id = await isolated_container.queue.enqueue(
            tx, KIND_OK, OkPayload(effect_id="lease", steps=2), idempotency_key="lease"
        )

    stalled = worker_for(isolated_container, owner="worker-stalled")
    claimed = await isolated_container.store.claim_one(stalled.owner)
    assert claimed is not None  # worker died before finishing
    assert claimed.id == job_id

    # The lease is 2 x the 10 s handler timeout; move the clock past it.
    frozen_clock.advance(timedelta(seconds=25))
    assert (await isolated_container.store.reap_expired_leases()) == 1

    async with isolated_container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    assert row["state"] == JobState.RETRYING.value
    assert row["attempts"] == 0  # a dead worker did not consume an attempt
    assert row["last_error_code"] == "LeaseExpired"

    healthy = worker_for(isolated_container, owner="worker-healthy")
    assert await healthy.run_until_idle() == 1
    async with isolated_container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
        assert await count_effects(tx.session, "lease") == 1
    assert row["state"] == JobState.SUCCEEDED.value


async def test_a_reaped_worker_cannot_overwrite_the_new_outcome(
    isolated_container: Container, isolated_effects: Any, frozen_clock: FrozenClock
) -> None:
    async with isolated_container.uow() as tx:
        job_id = await isolated_container.queue.enqueue(
            tx, KIND_OK, OkPayload(effect_id="owned"), idempotency_key="owned"
        )
    stalled = worker_for(isolated_container, owner="worker-stalled")
    claimed = await isolated_container.store.claim_one(stalled.owner)
    assert claimed is not None
    frozen_clock.advance(timedelta(seconds=25))
    await isolated_container.store.reap_expired_leases()

    assert await isolated_container.store.succeed(claimed.id, stalled.owner) is False
    async with isolated_container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    assert row["state"] == JobState.RETRYING.value


# --------------------------------------------------------------------------- waiting for user


async def test_wait_for_user_then_resume(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker]
) -> None:
    job_id = await enqueue(KIND_WAITING, WaitingPayload(), key="wait")
    worker = make_worker()
    assert await worker.run_once() == 1
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    assert row["state"] == JobState.WAITING_FOR_USER.value
    assert row["waiting_action"] == "Review"
    assert row["waiting_destination"]["path"].startswith("/projects/")

    async with container.uow() as tx:
        await container.queue.resume(tx, job_id)
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    assert row["state"] == JobState.QUEUED.value
    assert row["waiting_action"] is None
    # Resuming re-runs the handler, which parks again: a waiting handler must be re-entrant.
    await worker.run_once()
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
    assert row["state"] == JobState.WAITING_FOR_USER.value


# --------------------------------------------------------------------------- chains


async def test_depends_on_holds_a_chained_job_until_its_dependency_finishes(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker], effects: Any
) -> None:
    operation_id = "op_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"
    await enqueue(
        KIND_CHAIN_A, ChainPayload(effect_id="a"), key="a", operation_id=operation_id
    )
    await enqueue(
        KIND_CHAIN_B, ChainPayload(effect_id="b"), key="b", operation_id=operation_id
    )
    worker = make_worker(kinds=[KIND_CHAIN_B])
    assert await worker.run_once() == 0  # B waits for A

    worker_a = make_worker(kinds=[KIND_CHAIN_A], owner="worker-a")
    assert await worker_a.run_once() == 1
    assert await worker.run_once() == 1

    async with container.uow() as tx:
        assert await count_effects(tx.session, "a") == 1
        assert await count_effects(tx.session, "b") == 1


async def test_enqueue_next_runs_under_the_same_operation(
    container: Container,
    enqueue: Enqueue,
    make_worker: Callable[..., Worker],
    effects: Any,
    frozen_clock: FrozenClock,
) -> None:
    operation_id = "op_01J9Z3Q8M8V3W7G2E1N4R5T6Y8"
    await enqueue(
        KIND_OK,
        OkPayload(effect_id="first", enqueue_kind=KIND_NEXT),
        key="first",
        operation_id=operation_id,
    )
    worker = make_worker(concurrency=1)
    await worker.run_until_idle()
    async with container.uow() as tx:
        rows = (
            (await tx.session.execute(select(jobs.c.kind, jobs.c.operation_id, jobs.c.state)))
            .all()
        )
    assert len(rows) == 2
    assert {row.operation_id for row in rows} == {operation_id}
    assert {row.state for row in rows} == {JobState.SUCCEEDED.value}


# --------------------------------------------------------------------------- quota


async def test_paid_job_fails_once_when_quota_is_exhausted(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker], effects: Any
) -> None:
    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 5, QuotaPeriod.TOTAL)
        reservation = await container.usage.reserve(tx, Meter.OCR_PAGES, 5)
    async with container.uow() as tx:
        await container.usage.commit(tx, reservation, 5)

    job_id = await enqueue(KIND_PAID, PaidPayload(effect_id="paid"), key="paid")
    worker = make_worker()
    assert await worker.run_once() == 1
    async with container.uow() as tx:
        row = (
            (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
        )
        assert await count_effects(tx.session, "paid") == 0
    assert row["state"] == JobState.FAILED.value
    assert row["attempts"] == 0  # never ran: no retry loop against an exhausted budget
    assert row["last_error_code"] == "QuotaExceeded"


async def test_paid_job_reserves_commits_and_reports(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker], effects: Any
) -> None:
    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 10, QuotaPeriod.TOTAL)
    await enqueue(KIND_PAID, PaidPayload(effect_id="paid-ok", reserve=4, commit=3), key="paid-ok")
    worker = make_worker()
    assert await worker.run_once() == 1
    async with container.uow() as tx:
        assert await count_effects(tx.session, "paid-ok") == 1
    report = await container.usage.usage()
    ocr = next(m for m in report.meters if m.meter is Meter.OCR_PAGES)
    assert ocr.used == 3
    assert ocr.reserved == 0
    assert ocr.quota == 10


# --------------------------------------------------------------------------- sweeps


async def test_scheduler_enqueues_due_sweeps_once_per_firing(
    isolated_container: Container, isolated_effects: Any, frozen_clock: FrozenClock
) -> None:
    registry = JobRegistry()
    runs: list[datetime] = []

    @registry.sweep("test.daily", cron="0 3 * * *")
    async def daily(ctx: Any, payload: SweepPayload) -> None:
        runs.append(payload.scheduled_for)
        async with ctx.tx_factory() as tx:
            await tx.session.execute(
                insert(effects).values(
                    id=f"sweep-{payload.scheduled_for:%Y%m%d%H%M}",
                    kind="test.daily",
                    value=0,
                    at=ctx.clock.now(),
                )
            )

    registry.validate()
    # The queue and store capture the registry at build time, so build a container over it.
    swept = build_container(
        isolated_container.settings, engine=isolated_container.engine,
        uow=isolated_container.uow, clock=frozen_clock, registry=registry,
    )  # fmt: skip
    queue = swept.queue

    scheduler = Scheduler(
        store=swept.store,
        queue=queue,
        usage=swept.usage,
        registry=registry,
        clock=swept.clock,
        uow=swept.uow,
    )
    frozen_clock.set(datetime(2026, 5, 4, 2, 59, tzinfo=UTC))
    assert await scheduler.tick() == TickResult(0, 0, 0, 0)

    frozen_clock.advance(timedelta(minutes=1))
    tick = await scheduler.tick()
    assert tick.sweeps_enqueued == 1
    async with swept.uow() as tx:
        row = (
            (
                await tx.session.execute(
                    select(jobs).where(jobs.c.kind == "test.daily")
                )
            )
            .mappings()
            .one()
        )
    assert row["idempotency_key"] == sweep_idempotency_key(datetime(2026, 5, 4, 3, 0, tzinfo=UTC))

    # A second worker ticking the same minute does not duplicate the firing.
    other = Scheduler(
        store=swept.store,
        queue=queue,
        usage=swept.usage,
        registry=registry,
        clock=swept.clock,
        uow=swept.uow,
    )
    assert (await other.tick()).sweeps_enqueued == 1
    async with swept.uow() as tx:
        count = (
            (await tx.session.execute(select(jobs.c.id).where(jobs.c.kind == "test.daily")))
            .scalars()
            .all()
        )
    assert len(count) == 1

    worker = worker_for(swept, kinds=["test.daily"])
    assert await worker.run_until_idle() == 1
    assert runs == [datetime(2026, 5, 4, 3, 0, tzinfo=UTC)]


async def test_scheduler_reaps_and_promotes(
    container: Container, enqueue: Enqueue, scheduler: Scheduler, frozen_clock: FrozenClock
) -> None:
    await enqueue(KIND_OK, OkPayload(), key="promote", run_at=frozen_clock.now() + timedelta(minutes=1))
    async with container.uow() as tx:
        await tx.session.execute(
            jobs.update().values(state=JobState.RETRYING.value)
        )
    frozen_clock.advance(timedelta(minutes=1))
    tick = await scheduler.tick()
    assert tick.promoted == 1
    async with container.uow() as tx:
        state = (await tx.session.execute(select(jobs.c.state))).scalar_one()
    assert state == JobState.QUEUED.value


# --------------------------------------------------------------------------- stats


async def test_queue_stats_are_content_free(
    container: Container, enqueue: Enqueue, make_worker: Callable[..., Worker], frozen_clock: FrozenClock,
) -> None:
    await enqueue(KIND_OK, OkPayload(), key="pending")
    await enqueue(KIND_TERMINAL, TerminalPayload(), key="failed")
    worker = make_worker()
    await worker.run_once()  # one of the two
    frozen_clock.advance(timedelta(hours=1))
    await worker.run_once()

    stats = {s.kind: s for s in await container.queue.stats()}
    assert stats[KIND_OK].depth == 1
    assert stats[KIND_OK].oldest_age_seconds >= 3600
    assert stats[KIND_TERMINAL].failed_24h == 1
    # Every registered kind is reported, even with no rows.
    assert KIND_CHAIN_A in stats
    assert stats[KIND_CHAIN_A].depth == 0


async def test_every_registered_kind_is_reported(container: Container) -> None:
    stats = await container.queue.stats()
    assert [s.kind for s in stats] == sorted(container.registry.kinds())


async def test_resume_rejects_unknown_and_active_jobs(
    container: Container, enqueue: Enqueue
) -> None:
    from app.platform.errors import InvalidTransition, NotAvailable

    with pytest.raises(NotAvailable):
        async with container.uow() as tx:
            await container.queue.resume(tx, "job_01J9Z3Q8M8V3W7G2E1N4R5T6Y7")

    job_id = await enqueue(KIND_OK, OkPayload(), key="active")
    with pytest.raises(InvalidTransition):
        async with container.uow() as tx:
            await container.queue.resume(tx, job_id)


async def test_quota_policy_rows_override_settings(
    container: Container, enqueue: Enqueue
) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    async with container.uow() as tx:
        await tx.session.execute(
            pg_insert(quota_policies).values(
                firm_id=container.settings.firm_id,
                meter=Meter.AI_TOKENS_IN.value,
                quota=1234,
                period=QuotaPeriod.TOTAL.value,
            )
        )
    report = await container.usage.usage()
    tokens = next(m for m in report.meters if m.meter is Meter.AI_TOKENS_IN)
    assert tokens.quota == 1234


async def test_usage_guard_rejects_negative_units(container: Container) -> None:
    with pytest.raises(ValueError, match="units must be"):
        async with container.uow() as tx:
            await container.usage.reserve(tx, Meter.OCR_PAGES, -1)
