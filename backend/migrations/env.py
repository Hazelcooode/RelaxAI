"""Alembic environment: one migration stream across every module schema.

The database URL comes from ``config.attributes["url"]`` (tests, programmatic use) or from
application settings (``RELAXAI__DB__URL``). A caller may also pass an open sync connection
in ``config.attributes["connection"]``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.platform.db.metadata import all_metadata
from app.settings import load_settings

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = all_metadata()


def _url() -> str:
    url = config.attributes.get("url")
    if isinstance(url, str):
        return url
    return load_settings().db.url.get_secret_value()


def _configure(connection: Connection | None = None, *, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_schemas=True,
        compare_type=True,
        transaction_per_migration=True,
        literal_binds=connection is None,
    )


def _run_sync(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    _configure(url=_url())
    with context.begin_transaction():
        context.run_migrations()
elif isinstance(existing := config.attributes.get("connection"), Connection):
    _run_sync(existing)
else:
    asyncio.run(_run_async())
