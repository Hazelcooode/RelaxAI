"""governance: durable jobs, audit events, usage meters and quotas

Revision ID: 0002_governance
Revises: 0001_platform
Create Date: 2026-10-15
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_governance"
down_revision: str | Sequence[str] | None = "0001_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "governance"


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(40), nullable=False),
        _ts("at"),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("project_id", sa.String(40)),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_user_id", sa.String(40)),
        sa.Column("actor_label", sa.String(200), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(256), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("facts", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("correlation_id", sa.String(128)),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        schema=S,
    )
    op.create_index(
        "ix_audit_events_firm_id_at", "audit_events", ["firm_id", sa.text("at DESC")], schema=S
    )
    op.create_index(
        "ix_audit_events_project_id_at",
        "audit_events",
        ["project_id", sa.text("at DESC")],
        schema=S,
    )
    op.create_index(
        "ix_audit_events_action_at", "audit_events", ["action", sa.text("at DESC")], schema=S
    )
    op.create_index(
        "ix_audit_events_category_at",
        "audit_events",
        ["firm_id", "category", sa.text("at DESC")],
        schema=S,
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        _ts("run_at"),
        sa.Column("lease_owner", sa.String(200)),
        _ts("lease_expires_at", nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("operation_id", sa.String(40)),
        sa.Column("correlation_id", sa.String(128)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("progress_stage", sa.String(64)),
        sa.Column("progress_done", sa.BigInteger),
        sa.Column("progress_total", sa.BigInteger),
        sa.Column("progress_unit", sa.String(20)),
        sa.Column("waiting_action", sa.String(64)),
        sa.Column("waiting_destination", JSONB),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("completed_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.UniqueConstraint("kind", "idempotency_key", name="uq_jobs_kind_idempotency_key"),
        schema=S,
    )
    op.create_index(
        "ix_jobs_state_run_at_priority",
        "jobs",
        ["state", "run_at", sa.text("priority DESC")],
        schema=S,
    )
    op.create_index("ix_jobs_operation_id", "jobs", ["operation_id"], schema=S)
    op.create_index("ix_jobs_kind_state", "jobs", ["kind", "state"], schema=S)

    op.create_table(
        "usage_meters",
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("meter", sa.String(32), nullable=False),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("used", sa.BigInteger, nullable=False, server_default="0"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("firm_id", "meter", "period", name="pk_usage_meters"),
        schema=S,
    )
    op.create_table(
        "quota_policies",
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("meter", sa.String(32), nullable=False),
        sa.Column("quota", sa.BigInteger, nullable=False),
        sa.Column("period", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("firm_id", "meter", name="pk_quota_policies"),
        schema=S,
    )
    op.create_table(
        "quota_reservations",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("job_id", sa.String(40)),
        sa.Column("meter", sa.String(32), nullable=False),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("units", sa.BigInteger, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        _ts("created_at"),
        _ts("expires_at"),
        sa.PrimaryKeyConstraint("id", name="pk_quota_reservations"),
        schema=S,
    )
    op.create_index(
        "ix_quota_reservations_active",
        "quota_reservations",
        ["firm_id", "meter", "period", "state"],
        schema=S,
    )
    op.create_index("ix_quota_reservations_job_id", "quota_reservations", ["job_id"], schema=S)


def downgrade() -> None:
    for table in ("quota_reservations", "quota_policies", "usage_meters", "jobs", "audit_events"):
        op.drop_table(table, schema=S)
