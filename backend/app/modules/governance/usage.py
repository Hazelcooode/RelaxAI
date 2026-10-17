"""``UsageGuard``: metered usage and quota enforcement for paid AI/OCR/storage work (SYS-09).

Paid work reserves units before calling the provider, then commits the actual units or
releases the reservation. Reservations count against the quota while open, so concurrent
workers cannot jointly overshoot it. The meter row is locked (``FOR UPDATE``) during a
reservation, which serialises reservations per meter across processes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.modules.governance.domain import (
    JobState,
    Meter,
    QuotaPeriod,
    ReservationState,
    period_key,
)
from app.modules.governance.tables import jobs, quota_policies, quota_reservations, usage_meters
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork
from app.platform.errors import QuotaExceeded
from app.platform.ids import new_id
from app.settings import UsageSettings

QUOTA_EXCEEDED_CODE = "QuotaExceeded"
HALTED_WORK_WINDOW = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Reservation:
    id: str
    meter: Meter
    units: int
    period: str
    job_id: str | None


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    meter: Meter
    quota: int
    period: QuotaPeriod


@dataclass(frozen=True, slots=True)
class MeterUsage:
    meter: Meter
    period: str
    used: int
    reserved: int
    quota: int
    estimated_eur: float


@dataclass(frozen=True, slots=True)
class HaltedWork:
    kind: str
    count: int


@dataclass(frozen=True, slots=True)
class UsageReport:
    period: str
    meters: list[MeterUsage]
    halted_work: list[HaltedWork]
    planning_ceilings_eur: dict[str, float]


class UsageGuard:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        firm_id: str,
        settings: UsageSettings,
        reservation_ttl: timedelta,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._firm_id = firm_id
        self._settings = settings
        self._ttl = reservation_ttl

    # ------------------------------------------------------------------ policy

    async def policy(self, tx: Tx, meter: Meter) -> QuotaPolicy:
        row = (
            await tx.session.execute(
                select(quota_policies.c.quota, quota_policies.c.period).where(
                    quota_policies.c.firm_id == self._firm_id, quota_policies.c.meter == meter.value
                )
            )
        ).one_or_none()
        if row is not None:
            return QuotaPolicy(meter, int(row.quota), QuotaPeriod(row.period))
        return QuotaPolicy(
            meter,
            int(self._settings.default_quotas.get(meter.value, 0)),
            QuotaPeriod(self._settings.period),
        )

    async def set_quota(
        self, tx: Tx, meter: Meter, quota: int, period: QuotaPeriod | None = None
    ) -> None:
        """Operator quota change (``PUT /operator/quotas/{meter}``, T32)."""
        if quota < 0:
            raise ValueError("quota must be >= 0")
        values = {"quota": quota, "period": (period or QuotaPeriod(self._settings.period)).value}
        await tx.session.execute(
            insert(quota_policies)
            .values(firm_id=self._firm_id, meter=meter.value, **values)
            .on_conflict_do_update(index_elements=["firm_id", "meter"], set_=values)
        )

    # ------------------------------------------------------------------ reservations

    async def _lock_used(self, tx: Tx, meter: Meter, period: str) -> int:
        now = self._clock.now()
        await tx.session.execute(
            insert(usage_meters)
            .values(firm_id=self._firm_id, meter=meter.value, period=period, used=0, updated_at=now)
            .on_conflict_do_nothing()
        )
        used = await tx.session.execute(
            select(usage_meters.c.used)
            .where(
                usage_meters.c.firm_id == self._firm_id,
                usage_meters.c.meter == meter.value,
                usage_meters.c.period == period,
            )
            .with_for_update()
        )
        return int(used.scalar_one())

    async def _reserved(self, tx: Tx, meter: Meter, period: str) -> int:
        total = await tx.session.execute(
            select(func.coalesce(func.sum(quota_reservations.c.units), 0)).where(
                quota_reservations.c.firm_id == self._firm_id,
                quota_reservations.c.meter == meter.value,
                quota_reservations.c.period == period,
                quota_reservations.c.state == ReservationState.RESERVED.value,
                quota_reservations.c.expires_at > self._clock.now(),
            )
        )
        return int(total.scalar_one())

    async def reserve(
        self, tx: Tx, meter: Meter, units: int, *, job_id: str | None = None
    ) -> Reservation:
        """Reserve ``units`` or raise ``QuotaExceeded`` (nothing is written in that case)."""
        if units < 0:
            raise ValueError("units must be >= 0")
        policy = await self.policy(tx, meter)
        period = period_key(policy.period, self._clock.now())
        used = await self._lock_used(tx, meter, period)
        reserved = await self._reserved(tx, meter, period)
        if used + reserved + units > policy.quota:
            raise QuotaExceeded()
        now = self._clock.now()
        reservation = Reservation(new_id("rsv"), meter, units, period, job_id)
        await tx.session.execute(
            quota_reservations.insert().values(
                id=reservation.id,
                firm_id=self._firm_id,
                job_id=job_id,
                meter=meter.value,
                period=period,
                units=units,
                state=ReservationState.RESERVED.value,
                created_at=now,
                expires_at=now + self._ttl,
            )
        )
        return reservation

    async def commit(self, tx: Tx, reservation: Reservation, actual_units: int) -> None:
        """Record what was really consumed. Usage is counted even if the reservation expired:
        the provider has already charged for it."""
        if actual_units < 0:
            raise ValueError("actual_units must be >= 0")
        await self._lock_used(tx, reservation.meter, reservation.period)
        await tx.session.execute(
            update(usage_meters)
            .where(
                usage_meters.c.firm_id == self._firm_id,
                usage_meters.c.meter == reservation.meter.value,
                usage_meters.c.period == reservation.period,
            )
            .values(used=usage_meters.c.used + actual_units, updated_at=self._clock.now())
        )
        await tx.session.execute(
            update(quota_reservations)
            .where(quota_reservations.c.id == reservation.id)
            .values(state=ReservationState.COMMITTED.value)
        )

    async def release(self, tx: Tx, reservation: Reservation) -> None:
        await tx.session.execute(
            update(quota_reservations)
            .where(
                quota_reservations.c.id == reservation.id,
                quota_reservations.c.state == ReservationState.RESERVED.value,
            )
            .values(state=ReservationState.RELEASED.value)
        )

    async def release_for_job(self, job_id: str) -> int:
        """Release anything a finished job left reserved."""
        async with self._uow() as tx:
            result = await tx.session.execute(
                update(quota_reservations)
                .where(
                    quota_reservations.c.job_id == job_id,
                    quota_reservations.c.state == ReservationState.RESERVED.value,
                )
                .values(state=ReservationState.RELEASED.value)
                .returning(quota_reservations.c.id)
            )
            return len(result.all())

    async def release_expired(self) -> int:
        """Scheduler: expire reservations whose holder never committed or released them."""
        async with self._uow() as tx:
            result = await tx.session.execute(
                update(quota_reservations)
                .where(
                    quota_reservations.c.state == ReservationState.RESERVED.value,
                    quota_reservations.c.expires_at <= self._clock.now(),
                )
                .values(state=ReservationState.EXPIRED.value)
                .returning(quota_reservations.c.id)
            )
            return len(result.all())

    async def exhausted(self, meters: Iterable[Meter]) -> Meter | None:
        """The first meter with no headroom left, if any (worker pre-check for paid jobs)."""
        async with self._uow() as tx:
            for meter in meters:
                policy = await self.policy(tx, meter)
                period = period_key(policy.period, self._clock.now())
                used = (
                    await tx.session.execute(
                        select(usage_meters.c.used).where(
                            usage_meters.c.firm_id == self._firm_id,
                            usage_meters.c.meter == meter.value,
                            usage_meters.c.period == period,
                        )
                    )
                ).scalar_one_or_none() or 0
                if used + await self._reserved(tx, meter, period) >= policy.quota:
                    return meter
        return None

    # ------------------------------------------------------------------ reporting

    async def usage(self) -> UsageReport:
        now = self._clock.now()
        meters: list[MeterUsage] = []
        async with self._uow() as tx:
            for meter in Meter:
                policy = await self.policy(tx, meter)
                period = period_key(policy.period, now)
                used = (
                    await tx.session.execute(
                        select(usage_meters.c.used).where(
                            usage_meters.c.firm_id == self._firm_id,
                            usage_meters.c.meter == meter.value,
                            usage_meters.c.period == period,
                        )
                    )
                ).scalar_one_or_none() or 0
                price = self._settings.unit_price_eur.get(meter.value, 0.0)
                meters.append(
                    MeterUsage(
                        meter=meter,
                        period=period,
                        used=int(used),
                        reserved=await self._reserved(tx, meter, period),
                        quota=policy.quota,
                        estimated_eur=round(int(used) * price, 4),
                    )
                )
            halted = (
                await tx.session.execute(
                    select(jobs.c.kind, func.count())
                    .where(
                        jobs.c.firm_id == self._firm_id,
                        jobs.c.state == JobState.FAILED.value,
                        jobs.c.last_error_code == QUOTA_EXCEEDED_CODE,
                        jobs.c.completed_at >= now - HALTED_WORK_WINDOW,
                    )
                    .group_by(jobs.c.kind)
                    .order_by(jobs.c.kind)
                )
            ).all()
        return UsageReport(
            period=period_key(QuotaPeriod(self._settings.period), now),
            meters=meters,
            halted_work=[HaltedWork(kind, count) for kind, count in halted],
            planning_ceilings_eur=dict(self._settings.planning_ceilings_eur),
        )
