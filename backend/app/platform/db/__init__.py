"""Database access: engine, unit of work, per-schema metadata, optimistic updates."""

from app.platform.db.engine import create_engine
from app.platform.db.metadata import NAMING_CONVENTION, SCHEMAS, all_metadata, metadata_for
from app.platform.db.optimistic import optimistic_update
from app.platform.db.uow import Tx, UnitOfWork

__all__ = [
    "NAMING_CONVENTION",
    "SCHEMAS",
    "Tx",
    "UnitOfWork",
    "all_metadata",
    "create_engine",
    "metadata_for",
    "optimistic_update",
]
