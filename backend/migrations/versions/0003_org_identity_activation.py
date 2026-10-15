"""org: firms, approved domains, user accounts, firm manager assignments

Revision ID: 0003_org
Revises: 0002_governance
Create Date: 2026-10-15
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT

revision: str = "0003_org"
down_revision: str | Sequence[str] | None = "0002_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "org"


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    # citext is a trusted extension (PostgreSQL 13+): a database owner may create it.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.create_table(
        "firms",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("stamp", sa.String(63), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_firms"),
        sa.UniqueConstraint("stamp", name="uq_firms_stamp"),
        schema=S,
    )
    op.create_table(
        "approved_domains",
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("domain", CITEXT, nullable=False),
        _ts("created_at"),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("firm_id", "domain", name="pk_approved_domains"),
        sa.ForeignKeyConstraint(
            ["firm_id"], ["org.firms.id"], name="fk_approved_domains_firm_id_firms"
        ),
        schema=S,
    )
    op.create_table(
        "user_accounts",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("external_subject", sa.String(128), nullable=False),
        sa.Column("email", CITEXT, nullable=False),
        _ts("email_verified_at", nullable=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("person_category", sa.String(24), nullable=False),
        sa.Column("activation_state", sa.String(16), nullable=False),
        _ts("created_at"),
        _ts("disabled_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_user_accounts"),
        sa.ForeignKeyConstraint(
            ["firm_id"], ["org.firms.id"], name="fk_user_accounts_firm_id_firms"
        ),
        sa.UniqueConstraint(
            "firm_id", "external_subject", name="uq_user_accounts_firm_id_external_subject"
        ),
        sa.UniqueConstraint("firm_id", "email", name="uq_user_accounts_firm_id_email"),
        schema=S,
    )
    op.create_table(
        "firm_manager_assignments",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("nominated_by", sa.String(40)),
        sa.Column("approved_by", sa.String(40)),
        _ts("approved_at", nullable=True),
        _ts("removed_at", nullable=True),
        sa.Column("reason", sa.String(500)),
        sa.PrimaryKeyConstraint("id", name="pk_firm_manager_assignments"),
        sa.ForeignKeyConstraint(
            ["firm_id"], ["org.firms.id"], name="fk_firm_manager_assignments_firm_id_firms"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["org.user_accounts.id"],
            name="fk_firm_manager_assignments_user_id_user_accounts",
        ),
        schema=S,
    )
    op.create_index(
        "uq_firm_manager_assignments_active_user",
        "firm_manager_assignments",
        ["firm_id", "user_id"],
        unique=True,
        schema=S,
        postgresql_where=sa.text("state = 'Active'"),
    )


def downgrade() -> None:
    for table in ("firm_manager_assignments", "user_accounts", "approved_domains", "firms"):
        op.drop_table(table, schema=S)
    # The citext extension is left in place: it is database-wide and harmless.
