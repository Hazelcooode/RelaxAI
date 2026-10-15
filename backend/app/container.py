"""Composition root: builds adapters and services from ``Settings``.

Production calls ``build_container(settings)``. Tests pass ``overrides`` to swap any adapter
(clock, engine, unit of work, readiness probe, ...) without touching the code that uses it.
Later tasks add their adapters and services here.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import timedelta
from typing import TypedDict, Unpack

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.governance import (
    DEFAULT_REGISTRY,
    Audit,
    JobQueue,
    JobRegistry,
    JobStore,
    RegexReasonScreen,
    UsageGuard,
    Worker,
)
from app.modules.organization_access import OrganizationAccess
from app.platform.auth import TokenVerifier, entra_token_verifier
from app.platform.clock import Clock, SystemClock
from app.platform.db import UnitOfWork, create_engine
from app.platform.db.migrations import DatabaseReadiness, ReadinessProbe
from app.platform.http.pagination import CursorCodec
from app.settings import Settings


@dataclass(slots=True)
class Container:
    settings: Settings
    clock: Clock
    engine: AsyncEngine
    uow: UnitOfWork
    cursors: CursorCodec
    readiness: ReadinessProbe
    registry: JobRegistry
    queue: JobQueue
    store: JobStore
    usage: UsageGuard
    audit: Audit
    worker: Worker
    http: httpx.AsyncClient
    token_verifier: TokenVerifier
    organization: OrganizationAccess

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()


class ContainerOverrides(TypedDict, total=False):
    clock: Clock
    engine: AsyncEngine
    uow: UnitOfWork
    cursors: CursorCodec
    readiness: ReadinessProbe
    registry: JobRegistry
    worker_concurrency: int
    token_verifier: TokenVerifier


def build_container(settings: Settings, **overrides: Unpack[ContainerOverrides]) -> Container:
    engine = overrides.get("engine") or create_engine(settings.db)
    clock = overrides.get("clock") or SystemClock()
    uow = overrides.get("uow") or UnitOfWork(engine)
    registry = overrides.get("registry") or DEFAULT_REGISTRY
    cursors = overrides.get("cursors") or CursorCodec(
        settings.crypto.cursor_signing_key.get_secret_value().encode()
    )
    firm_id = settings.firm_id
    http = httpx.AsyncClient(follow_redirects=False)
    queue = JobQueue(uow=uow, clock=clock, registry=registry, firm_id=firm_id)
    usage = UsageGuard(
        uow=uow,
        clock=clock,
        firm_id=firm_id,
        settings=settings.usage,
        reservation_ttl=timedelta(seconds=settings.worker.reservation_ttl_seconds),
    )
    worker = Worker(
        store=JobStore(uow=uow, clock=clock, registry=registry, firm_id=firm_id),
        queue=queue,
        usage=usage,
        registry=registry,
        clock=clock,
        uow=uow,
        concurrency=overrides.get("worker_concurrency") or settings.worker.concurrency,
        poll_interval=settings.worker.poll_interval_seconds,
        shutdown_grace=settings.worker.shutdown_grace_seconds,
    )
    audit = Audit(
        uow=uow,
        clock=clock,
        firm_id=firm_id,
        cursors=cursors,
        reason_screen=RegexReasonScreen(),  # T11 replaces this
    )
    signing_key = settings.crypto.cursor_signing_key.get_secret_value().encode()
    organization = OrganizationAccess(
        uow=uow,
        clock=clock,
        firm_id=firm_id,
        stamp=settings.stamp,
        audit=audit,
        operator_subjects=settings.entra.operator_subjects,
        # Domain-separated from the cursor MAC so the two uses never share a key.
        reference_key=hmac.new(signing_key, b"relaxai/subject-reference", hashlib.sha256).digest(),
    )
    return Container(
        settings=settings,
        clock=clock,
        engine=engine,
        uow=uow,
        cursors=cursors,
        readiness=overrides.get("readiness") or DatabaseReadiness(engine),
        registry=registry,
        queue=queue,
        store=JobStore(uow=uow, clock=clock, registry=registry, firm_id=firm_id),
        usage=usage,
        audit=audit,
        worker=worker,
        http=http,
        token_verifier=overrides.get("token_verifier")
        or entra_token_verifier(settings.entra, http, clock),
        organization=organization,
    )
