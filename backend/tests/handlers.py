"""Handlers used by the governance tests, registered on a registry of their own.

Each handler records what it did in a test-only table created inside the test's transaction,
so assertions read the database rather than in-process state.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.governance import (
    DEFAULT_REGISTRY,
    Destination,
    JobContext,
    JobRegistry,
    Meter,
    Priority,
    RetryableError,
    TerminalError,
)

registry = JobRegistry()
registry.include(DEFAULT_REGISTRY)  # tests layer their handlers over the real ones

KIND_OK = "test.ok"
KIND_FLAKY = "test.flaky"
KIND_TERMINAL = "test.terminal"
KIND_SLOW = "test.slow"
KIND_WAITING = "test.waiting"
KIND_CHAIN_A = "test.chain_a"
KIND_CHAIN_B = "test.chain_b"
KIND_PAID = "test.paid"
KIND_NOOP = "test.noop"
KIND_NEXT = "test.next"

effects = Table(
    "t02_effects",
    MetaData(schema="governance"),
    Column("id", String(80), primary_key=True),
    Column("kind", String(64), nullable=False),
    Column("value", Integer, nullable=False, server_default="0"),
    Column("at", DateTime(timezone=True), nullable=False),
)

PAYLOAD = "probe-value"  # a payload token, not a credential


class OkPayload(BaseModel):
    effect_id: str = "effect-1"
    steps: int = 3
    enqueue_kind: str | None = None


class FlakyPayload(BaseModel):
    effect_id: str = "flaky-1"
    fail_times: int = 1
    retry_after_seconds: int | None = None


class TerminalPayload(BaseModel):
    code: str = "Permanent"


class SlowPayload(BaseModel):
    effect_id: str = "slow-1"
    seconds: float = 0.2


class WaitingPayload(BaseModel):
    effect_id: str = "waiting-1"


class ChainPayload(BaseModel):
    effect_id: str


class PaidPayload(BaseModel):
    effect_id: str = "paid-1"
    meter: Meter = Meter.OCR_PAGES
    reserve: int = 1
    commit: int = 1


class NoopPayload(BaseModel):
    effect_id: str = "noop-1"


async def _record(ctx: JobContext, effect_id: str, kind: str, value: int = 0) -> None:
    async with ctx.tx_factory() as tx:
        await tx.session.execute(
            effects.insert().values(id=effect_id, kind=kind, value=value, at=ctx.clock.now())
        )


async def count_effects(tx_session: AsyncSession, effect_id: str) -> int:
    return int(
        (
            await tx_session.execute(
                select(func.count()).select_from(effects).where(effects.c.id == effect_id)
            )
        ).scalar_one()
    )


@registry.handler(
    KIND_OK,
    payload=OkPayload,
    max_attempts=3,
    concurrency=1,
    priority=Priority.NORMAL,
    timeout=timedelta(seconds=10),
)
async def ok(ctx: JobContext, payload: OkPayload) -> None:
    for step in range(1, payload.steps + 1):
        await ctx.report("Working", done=step, total=payload.steps, unit="Records")
    await _record(ctx, payload.effect_id, KIND_OK)
    if payload.enqueue_kind is not None:
        async with ctx.tx_factory() as tx:
            await ctx.enqueue_next(
                payload.enqueue_kind,
                ChainPayload(effect_id=f"{payload.effect_id}-next"),
                idempotency_key=f"{ctx.job_id}:next",
                tx=tx,
            )


@registry.handler(
    KIND_FLAKY,
    payload=FlakyPayload,
    max_attempts=3,
    concurrency=1,
    priority=Priority.NORMAL,
    timeout=timedelta(seconds=10),
)
async def flaky(ctx: JobContext, payload: FlakyPayload) -> None:
    if ctx.attempt <= payload.fail_times:
        retry_after = (
            timedelta(seconds=payload.retry_after_seconds)
            if payload.retry_after_seconds is not None
            else None
        )
        raise RetryableError("FlakyUpstream", retry_after=retry_after)
    await _record(ctx, payload.effect_id, KIND_FLAKY)


@registry.handler(
    KIND_TERMINAL,
    payload=TerminalPayload,
    max_attempts=5,
    concurrency=1,
    priority=Priority.NORMAL,
    timeout=timedelta(seconds=10),
)
async def terminal(ctx: JobContext, payload: TerminalPayload) -> None:
    raise TerminalError(payload.code)


@registry.handler(
    KIND_SLOW,
    payload=SlowPayload,
    max_attempts=1,
    concurrency=1,
    priority=Priority.LOW,
    timeout=timedelta(seconds=10),
)
async def slow(ctx: JobContext, payload: SlowPayload) -> None:
    await asyncio.sleep(payload.seconds)
    await _record(ctx, payload.effect_id, KIND_SLOW)


@registry.handler(
    KIND_WAITING,
    payload=WaitingPayload,
    max_attempts=2,
    concurrency=1,
    priority=Priority.NORMAL,
    timeout=timedelta(seconds=10),
)
async def waiting(ctx: JobContext, payload: WaitingPayload) -> None:
    await ctx.wait_for_user(
        "Review", Destination(kind="Review", path="/projects/prj_x/privacy/reviews")
    )


@registry.handler(
    KIND_CHAIN_A,
    payload=ChainPayload,
    max_attempts=2,
    concurrency=1,
    priority=Priority.HIGH,
    timeout=timedelta(seconds=10),
)
async def chain_a(ctx: JobContext, payload: ChainPayload) -> None:
    await _record(ctx, payload.effect_id, KIND_CHAIN_A)


@registry.handler(
    KIND_CHAIN_B,
    payload=ChainPayload,
    max_attempts=2,
    concurrency=1,
    priority=Priority.HIGH,
    timeout=timedelta(seconds=10),
    depends_on=(KIND_CHAIN_A,),
)
async def chain_b(ctx: JobContext, payload: ChainPayload) -> None:
    await _record(ctx, payload.effect_id, KIND_CHAIN_B)


@registry.handler(
    KIND_PAID,
    payload=PaidPayload,
    max_attempts=3,
    concurrency=1,
    priority=Priority.NORMAL,
    paid=True,
    meters=(Meter.OCR_PAGES,),
    timeout=timedelta(seconds=10),
)
async def paid(ctx: JobContext, payload: PaidPayload) -> None:
    async with ctx.tx_factory() as tx:
        reservation = await ctx.usage.reserve(tx, payload.meter, payload.reserve, job_id=ctx.job_id)
        await ctx.usage.commit(tx, reservation, payload.commit)
    await _record(ctx, payload.effect_id, KIND_PAID)


@registry.handler(
    KIND_NEXT,
    payload=ChainPayload,
    max_attempts=2,
    concurrency=1,
    priority=Priority.NORMAL,
    timeout=timedelta(seconds=10),
)
async def chain_next(ctx: JobContext, payload: ChainPayload) -> None:
    await _record(ctx, payload.effect_id, KIND_NEXT)


@registry.handler(
    KIND_NOOP,
    payload=NoopPayload,
    max_attempts=1,
    concurrency=1,
    priority=Priority.LOW,
    timeout=timedelta(seconds=10),
)
async def noop(ctx: JobContext, payload: NoopPayload) -> None:
    del ctx, payload
