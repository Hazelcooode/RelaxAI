"""One ``MetaData`` per module schema, with deterministic constraint names."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from sqlalchemy import MetaData

SCHEMAS: Final[tuple[str, ...]] = (
    "org",
    "projects",
    "intake",
    "privacy",
    "knowledge",
    "retrieval",
    "intelligence",
    "actions",
    "governance",
)

NAMING_CONVENTION: Final[Mapping[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_METADATA: Final[dict[str, MetaData]] = {
    schema: MetaData(schema=schema, naming_convention=dict(NAMING_CONVENTION)) for schema in SCHEMAS
}


def metadata_for(schema: str) -> MetaData:
    """The shared ``MetaData`` a module's ``tables.py`` declares its tables on."""
    try:
        return _METADATA[schema]
    except KeyError:
        raise ValueError(f"unknown schema: {schema!r}") from None


def all_metadata() -> list[MetaData]:
    """Every module schema's metadata, for Alembic autogenerate."""
    return [_METADATA[schema] for schema in SCHEMAS]
