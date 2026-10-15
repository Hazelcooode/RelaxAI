"""Async SQLAlchemy engine (psycopg 3) built from settings."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.settings import DbSettings


def create_engine(db: DbSettings) -> AsyncEngine:
    """Create the engine lazily — no connection is opened until first use."""
    return create_async_engine(
        db.url.get_secret_value(),
        pool_size=db.pool_size,
        max_overflow=db.max_overflow,
        pool_timeout=db.pool_timeout_seconds,
        pool_pre_ping=True,
        echo=db.echo,
        connect_args={"options": f"-c statement_timeout={db.statement_timeout_ms}"},
    )
