"""Job handler and sweep registration.

Modules declare handlers in their ``jobs.py``::

    @job_handler("intake.extract", payload=ExtractPayload, max_attempts=3, concurrency=1,
                 priority=Priority.NORMAL, timeout=timedelta(minutes=5))
    async def extract(ctx: JobContext, payload: ExtractPayload) -> None: ...

and periodic sweeps with ``@sweep("retention.daily", cron="0 3 * * *")``. A sweep is an
ordinary job whose idempotency key is its scheduled minute, so several worker processes
never run the same firing twice and a failed sweep retries like any job.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, field_validator

from app.modules.governance.domain import Meter, Priority
from app.platform.cron import CronSchedule

if TYPE_CHECKING:
    from app.modules.governance.context import JobContext

type Handler[P: BaseModel] = Callable[[JobContext, P], Awaitable[None]]


class SweepPayload(BaseModel):
    scheduled_for: datetime

    @field_validator("scheduled_for")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled_for must be timezone-aware")
        return value


@dataclass(frozen=True, slots=True)
class HandlerSpec:
    kind: str
    fn: Handler[Any]
    payload: type[BaseModel]
    max_attempts: int
    concurrency: int
    priority: Priority
    paid: bool
    timeout: timedelta
    depends_on: tuple[str, ...]
    meters: tuple[Meter, ...]
    schedule: CronSchedule | None = None

    @property
    def lease_duration(self) -> timedelta:
        return self.timeout * 2


class JobRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, HandlerSpec] = {}

    def handler[P: BaseModel](
        self,
        kind: str,
        *,
        payload: type[P],
        max_attempts: int,
        concurrency: int,
        priority: Priority,
        paid: bool = False,
        timeout: timedelta,
        depends_on: tuple[str, ...] = (),
        meters: tuple[Meter, ...] | None = None,
    ) -> Callable[[Handler[P]], Handler[P]]:
        """Register a handler.

        ``depends_on``: kinds that must reach a terminal state, within the same
        ``operation_id``, before a job of this kind is claimed (ordering inside one chain).
        ``meters``: for ``paid=True`` jobs, the quotas the worker checks before running;
        default all meters.
        """
        if max_attempts < 1 or concurrency < 1 or timeout <= timedelta(0):
            raise ValueError(f"{kind}: max_attempts, concurrency and timeout must be positive")

        def register(fn: Handler[P]) -> Handler[P]:
            self._add(
                HandlerSpec(
                    kind=kind,
                    fn=fn,
                    payload=payload,
                    max_attempts=max_attempts,
                    concurrency=concurrency,
                    priority=priority,
                    paid=paid,
                    timeout=timeout,
                    depends_on=tuple(depends_on),
                    meters=tuple(meters) if meters is not None else tuple(Meter),
                )
            )
            return fn

        return register

    def sweep(
        self,
        kind: str,
        *,
        cron: str,
        max_attempts: int = 3,
        timeout: timedelta = timedelta(minutes=10),
        priority: Priority = Priority.LOW,
    ) -> Callable[[Handler[SweepPayload]], Handler[SweepPayload]]:
        schedule = CronSchedule.parse(cron)

        def register(fn: Handler[SweepPayload]) -> Handler[SweepPayload]:
            self._add(
                HandlerSpec(
                    kind=kind,
                    fn=fn,
                    payload=SweepPayload,
                    max_attempts=max_attempts,
                    concurrency=1,
                    priority=priority,
                    paid=False,
                    timeout=timeout,
                    depends_on=(),
                    meters=(),
                    schedule=schedule,
                )
            )
            return fn

        return register

    def _add(self, spec: HandlerSpec) -> None:
        if spec.kind in self._specs:
            raise ValueError(f"job kind already registered: {spec.kind}")
        # depends_on may name kinds registered later; validate() checks once all are loaded.
        self._specs[spec.kind] = spec

    def validate(self) -> None:
        for spec in self._specs.values():
            missing = set(spec.depends_on) - set(self._specs)
            if missing:
                raise ValueError(f"{spec.kind} depends on unregistered kinds {sorted(missing)}")

    def include(self, other: JobRegistry, *, sweeps: bool = True) -> None:
        """Copy every handler from ``other`` into this registry.

        Used to compose registries — the worker entry point loads each module's ``jobs.py``,
        and tests layer their own handlers over the real ones. ``sweeps=False`` leaves out
        periodic sweeps, so a frozen-clock test never sees a production sweep fire.
        """
        for spec in other:
            if sweeps or spec.schedule is None:
                self._add(spec)

    def get(self, kind: str) -> HandlerSpec | None:
        return self._specs.get(kind)

    def __getitem__(self, kind: str) -> HandlerSpec:
        try:
            return self._specs[kind]
        except KeyError:
            raise ValueError(f"no handler registered for job kind {kind!r}") from None

    def __iter__(self) -> Iterator[HandlerSpec]:
        return iter(self._specs.values())

    def kinds(self) -> list[str]:
        return sorted(self._specs)

    def sweeps(self) -> list[HandlerSpec]:
        return [s for s in self._specs.values() if s.schedule is not None]


def sweep_idempotency_key(scheduled_for: datetime) -> str:
    return scheduled_for.strftime("sweep:%Y-%m-%dT%H:%MZ")


DEFAULT_REGISTRY = JobRegistry()
job_handler = DEFAULT_REGISTRY.handler
sweep = DEFAULT_REGISTRY.sweep
