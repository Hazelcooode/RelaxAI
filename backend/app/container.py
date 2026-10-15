"""Composition root: builds adapters and services from ``Settings``.

Production calls ``build_container(settings)``. Tests pass ``overrides`` to swap any adapter
(clock, engine, unit of work, readiness probe, ...) without touching the code that uses it.
Later tasks add their adapters and services here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict, Unpack

from sqlalchemy.ext.asyncio import AsyncEngine

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

    async def aclose(self) -> None:
        await self.engine.dispose()


class ContainerOverrides(TypedDict, total=False):
    clock: Clock
    engine: AsyncEngine
    uow: UnitOfWork
    cursors: CursorCodec
    readiness: ReadinessProbe


def build_container(settings: Settings, **overrides: Unpack[ContainerOverrides]) -> Container:
    engine = overrides.get("engine") or create_engine(settings.db)
    return Container(
        settings=settings,
        clock=overrides.get("clock") or SystemClock(),
        engine=engine,
        uow=overrides.get("uow") or UnitOfWork(engine),
        cursors=overrides.get("cursors")
        or CursorCodec(settings.crypto.cursor_signing_key.get_secret_value().encode()),
        readiness=overrides.get("readiness") or DatabaseReadiness(engine),
    )
