"""What a job handler sees: ``JobContext`` and the outcomes it may raise."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, NoReturn, TypeVar, cast

from pydantic import BaseModel

from app.modules.governance.domain import Destination, Priority
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork

if TYPE_CHECKING:
    from app.modules.governance.queue import JobQueue
    from app.modules.governance.store import JobStore
    from app.modules.governance.usage import UsageGuard


class RetryableError(Exception):
    """Transient failure: retry with backoff until ``max_attempts``.

    ``code`` is a user-safe code recorded on the job; ``retry_after`` honours an upstream
    hint such as Graph's ``Retry-After``.
    """

    def __init__(self, code: str = "Retryable", *, retry_after: timedelta | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class TerminalError(Exception):
    """Permanent failure: the job moves to ``Failed`` with ``code`` and is not retried."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LeaseLostError(Exception):
    """This worker no longer owns the job (lease expired and was reaped). Stop working."""


class WaitingForUserSignal(BaseException):  # control flow, not an error
    """Raised by ``JobContext.wait_for_user`` to end the attempt; handlers must not catch it."""


S = TypeVar("S")


class ServiceDirectory:
    """Module services a handler may use, looked up by type: ``ctx.service(Notifications)``.

    The composition root registers them, so a module's ``jobs.py`` reaches its own service
    without importing the container (which modules may not depend on).
    """

    def __init__(self) -> None:
        self._services: dict[type, object] = {}

    def register(self, service: object, *, as_type: type | None = None) -> None:
        self._services[as_type or type(service)] = service

    def get(self, kind: type[S]) -> S:
        try:
            return cast(S, self._services[kind])
        except KeyError:
            raise LookupError(f"no service registered for {kind.__name__}") from None


@dataclass(slots=True)
class JobContext:
    job_id: str
    kind: str
    attempt: int
    operation_id: str | None
    tx_factory: UnitOfWork
    clock: Clock
    usage: UsageGuard
    _store: JobStore
    _queue: JobQueue
    _owner: str
    _services: ServiceDirectory = field(default_factory=ServiceDirectory)

    def service(self, kind: type[S]) -> S:
        """A module service registered by the composition root."""
        return self._services.get(kind)

    async def report(
        self,
        stage: str,
        done: int | None = None,
        total: int | None = None,
        *,
        unit: str | None = None,
    ) -> None:
        """Record progress in its own short transaction, so readers see it immediately.

        ``stage`` is a code such as ``"ExtractingStructure"``. Give ``done`` and ``total``
        together only when both are trustworthy (contract §5.4).
        """
        if (done is None) != (total is None):
            raise ValueError("report done and total together, or neither")
        if not await self._store.report(self.job_id, self._owner, stage, done, total, unit):
            raise LeaseLostError(self.job_id)

    async def wait_for_user(self, action_code: str, destination: Destination) -> NoReturn:
        """Park the job in ``WaitingForUser`` and end this attempt.

        Someone resumes it later with ``JobQueue.resume(tx, job_id)`` (for example after a
        privacy review is resolved); the handler then runs again and must detect what is done.
        """
        if not await self._store.set_waiting(self.job_id, self._owner, action_code, destination):
            raise LeaseLostError(self.job_id)
        raise WaitingForUserSignal

    async def enqueue_next(
        self,
        kind: str,
        payload: BaseModel,
        idempotency_key: str,
        *,
        tx: Tx | None = None,
        priority: Priority | None = None,
    ) -> str:
        """Enqueue the next job of the same operation. Pass ``tx`` to commit it together with
        the handler's own writes (recommended); otherwise it commits on its own."""
        if tx is not None:
            return await self._queue.enqueue(
                tx, kind, payload, idempotency_key=idempotency_key,
                operation_id=self.operation_id, priority=priority,
            )  # fmt: skip
        async with self.tx_factory() as own:
            return await self._queue.enqueue(
                own, kind, payload, idempotency_key=idempotency_key,
                operation_id=self.operation_id, priority=priority,
            )  # fmt: skip
