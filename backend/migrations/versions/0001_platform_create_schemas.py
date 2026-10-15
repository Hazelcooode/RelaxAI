"""platform: create the nine module schemas

Revision ID: 0001_platform
Revises:
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_platform"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy: migrations must not change meaning when application code changes later.
SCHEMAS = (
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


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


def downgrade() -> None:
    # RESTRICT: refuses to drop a schema that still holds objects from later revisions.
    for schema in reversed(SCHEMAS):
        op.execute(f'DROP SCHEMA IF EXISTS "{schema}" RESTRICT')
