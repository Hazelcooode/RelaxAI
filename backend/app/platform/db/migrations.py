"""Readiness: is the database reachable and is the Alembic head applied?"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


class CheckState(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    database: CheckState
    migrations: CheckState

    @property
    def ready(self) -> bool:
        return self.database is CheckState.OK and self.migrations is CheckState.OK


class ReadinessProbe(Protocol):
    async def check(self) -> ReadinessReport: ...


def alembic_config(url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url is not None:
        config.attributes["url"] = url
    return config


def expected_heads() -> frozenset[str]:
    return frozenset(ScriptDirectory.from_config(alembic_config()).get_heads())


class DatabaseReadiness:
    def __init__(self, engine: AsyncEngine, heads: frozenset[str] | None = None) -> None:
        self._engine = engine
        self._heads = heads if heads is not None else expected_heads()

    async def check(self) -> ReadinessReport:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                try:
                    rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
                    applied = frozenset(str(r[0]) for r in rows)
                except SQLAlchemyError:
                    return ReadinessReport(CheckState.OK, CheckState.PENDING)
        except (SQLAlchemyError, OSError):
            return ReadinessReport(CheckState.UNAVAILABLE, CheckState.UNKNOWN)
        state = CheckState.OK if applied == self._heads else CheckState.PENDING
        return ReadinessReport(CheckState.OK, state)
