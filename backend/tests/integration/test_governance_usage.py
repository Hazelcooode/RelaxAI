"""Usage meters, quota reservations, and the cost report (T02)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.container import Container
from app.platform.clock import FrozenClock
from app.modules.governance import Meter, QuotaPeriod, ReservationState
from app.modules.governance.tables import quota_reservations
from app.platform.errors import QuotaExceeded


async def test_reserve_counts_against_the_quota_and_blocks_at_the_limit(
    container: Container,
) -> None:
    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 10, QuotaPeriod.TOTAL)
        await container.usage.reserve(tx, Meter.OCR_PAGES, 6)

    with pytest.raises(QuotaExceeded):
        async with container.uow() as tx:
            await container.usage.reserve(tx, Meter.OCR_PAGES, 5)

    # Exactly the remaining headroom still works.
    async with container.uow() as tx:
        await container.usage.reserve(tx, Meter.OCR_PAGES, 4)


async def test_commit_records_actual_usage_and_releases_the_reservation(
    container: Container,
) -> None:
    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 10, QuotaPeriod.TOTAL)
        reservation = await container.usage.reserve(tx, Meter.OCR_PAGES, 8, job_id="job_x")
    async with container.uow() as tx:
        await container.usage.commit(tx, reservation, 3)

    report = await container.usage.usage()
    ocr = next(m for m in report.meters if m.meter is Meter.OCR_PAGES)
    assert ocr.used == 3
    assert ocr.reserved == 0
    async with container.uow() as tx:
        state = (
            await tx.session.execute(
                select(quota_reservations.c.state).where(quota_reservations.c.id == reservation.id)
            )
        ).scalar_one()
    assert state == ReservationState.COMMITTED.value


async def test_release_frees_headroom(container: Container) -> None:
    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 5, QuotaPeriod.TOTAL)
        reservation = await container.usage.reserve(tx, Meter.OCR_PAGES, 5)
    with pytest.raises(QuotaExceeded):
        async with container.uow() as tx:
            await container.usage.reserve(tx, Meter.OCR_PAGES, 1)
    async with container.uow() as tx:
        await container.usage.release(tx, reservation)
    async with container.uow() as tx:
        await container.usage.reserve(tx, Meter.OCR_PAGES, 5)


async def test_expired_reservations_are_released_by_the_scheduler(
    container: Container, frozen_clock: FrozenClock
) -> None:
    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 5, QuotaPeriod.TOTAL)
        await container.usage.reserve(tx, Meter.OCR_PAGES, 5)
    with pytest.raises(QuotaExceeded):
        async with container.uow() as tx:
            await container.usage.reserve(tx, Meter.OCR_PAGES, 1)

    frozen_clock.advance(timedelta(seconds=container.settings.worker.reservation_ttl_seconds + 1))
    assert await container.usage.release_expired() == 1
    async with container.uow() as tx:
        await container.usage.reserve(tx, Meter.OCR_PAGES, 5)


async def test_release_for_job_clears_what_a_crashed_handler_left(
    container: Container,
) -> None:
    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 5, QuotaPeriod.TOTAL)
        await container.usage.reserve(tx, Meter.OCR_PAGES, 5, job_id="job_crashed")
    assert await container.usage.release_for_job("job_crashed") == 1
    assert await container.usage.release_for_job("job_crashed") == 0  # idempotent
    async with container.uow() as tx:
        await container.usage.reserve(tx, Meter.OCR_PAGES, 5)


async def test_usage_report_covers_every_meter_with_estimates(container: Container) -> None:
    async with container.uow() as tx:
        reservation = await container.usage.reserve(tx, Meter.OCR_PAGES, 10)
    async with container.uow() as tx:
        await container.usage.commit(tx, reservation, 10)

    report = await container.usage.usage()
    assert [m.meter for m in report.meters] == list(Meter)
    assert report.period == "total"
    ocr = next(m for m in report.meters if m.meter is Meter.OCR_PAGES)
    assert ocr.estimated_eur == pytest.approx(10 * 1.50 / 1_000)
    assert sum(report.planning_ceilings_eur.values()) == 100


async def test_monthly_period_is_separate(container: Container, frozen_clock: FrozenClock) -> None:
    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 5, QuotaPeriod.MONTHLY)
        reservation = await container.usage.reserve(tx, Meter.OCR_PAGES, 5)
    async with container.uow() as tx:
        await container.usage.commit(tx, reservation, 5)
    report = await container.usage.usage()
    assert next(m for m in report.meters if m.meter is Meter.OCR_PAGES).period == "2026-01"

    frozen_clock.advance(timedelta(days=40))
    async with container.uow() as tx:
        await container.usage.reserve(tx, Meter.OCR_PAGES, 5)  # a new month has its own budget


async def test_exhausted_reports_the_blocking_meter(container: Container) -> None:
    assert await container.usage.exhausted([Meter.OCR_PAGES]) is None
    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 0, QuotaPeriod.TOTAL)
    assert await container.usage.exhausted([Meter.OCR_PAGES, Meter.AI_TOKENS_IN]) is Meter.OCR_PAGES


async def test_set_quota_rejects_negative_values(container: Container) -> None:
    with pytest.raises(ValueError, match="quota must be"):
        async with container.uow() as tx:
            await container.usage.set_quota(tx, Meter.OCR_PAGES, -1)
