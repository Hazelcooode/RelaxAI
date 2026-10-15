"""governance operations, idempotency keys, undo tokens; actions notifications

Revision ID: 0004_operations
Revises: 0003_org
Create Date: 2026-10-15
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004_operations"
down_revision: str | Sequence[str] | None = "0003_org"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

G = "governance"
A = "actions"


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _flag(name: str, default: str) -> sa.Column[Any]:
    return sa.Column(name, sa.Boolean, nullable=False, server_default=sa.text(default))


def upgrade() -> None:
    op.create_table(
        "operations",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("project_id", sa.String(40)),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("subject_label", sa.String(200), nullable=False),
        sa.Column("subject_record_id", sa.String(40)),
        sa.Column("initiated_by_user_id", sa.String(40)),
        sa.Column("audience_kind", sa.String(20), nullable=False),
        sa.Column("audience_action_code", sa.String(64)),
        sa.Column("audience_project_id", sa.String(40)),
        sa.Column("destination_kind", sa.String(40), nullable=False),
        sa.Column("destination_path", sa.String(500), nullable=False),
        sa.Column("destination_project_id", sa.String(40)),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("domain_state", sa.String(40), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("progress_done", sa.BigInteger),
        sa.Column("progress_total", sa.BigInteger),
        sa.Column("progress_unit", sa.String(20)),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("failure_message_safe", sa.String(200)),
        _flag("failure_retryable", "false"),
        _flag("work_retained", "false"),
        sa.Column("waiting_action_code", sa.String(64)),
        _ts("next_retry_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("completed_at", nullable=True),
        _flag("badge_visible", "true"),
        sa.PrimaryKeyConstraint("id", name="pk_operations"),
        schema=G,
    )
    op.create_index(
        "ix_operations_initiated_by_user_id_state",
        "operations",
        ["initiated_by_user_id", "state"],
        schema=G,
    )
    op.create_index(
        "ix_operations_firm_id_state_updated_at",
        "operations",
        ["firm_id", "state", "updated_at"],
        schema=G,
    )
    op.create_index(
        "ix_operations_audience_project_id", "operations", ["audience_project_id"], schema=G
    )
    op.create_table(
        "operation_dismissals",
        sa.Column("operation_id", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(40), nullable=False),
        _ts("dismissed_at"),
        sa.PrimaryKeyConstraint("operation_id", "user_id", name="pk_operation_dismissals"),
        schema=G,
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("route_template", sa.String(200), nullable=False),
        sa.Column("idem_key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer),
        sa.Column("response_body", JSONB),
        sa.Column("operation_id", sa.String(40)),
        _ts("created_at"),
        sa.PrimaryKeyConstraint(
            "actor_id", "route_template", "idem_key", name="pk_idempotency_keys"
        ),
        schema=G,
    )
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"], schema=G)
    op.create_table(
        "undo_tokens",
        sa.Column("token", sa.String(40), nullable=False),
        sa.Column("actor_id", sa.String(40), nullable=False),
        sa.Column("command_kind", sa.String(64), nullable=False),
        sa.Column("compensating_payload", JSONB, nullable=False),
        sa.Column("result_payload", JSONB),
        _ts("created_at"),
        _ts("expires_at"),
        _ts("consumed_at", nullable=True),
        sa.PrimaryKeyConstraint("token", name="pk_undo_tokens"),
        schema=G,
    )
    op.create_index("ix_undo_tokens_expires_at", "undo_tokens", ["expires_at"], schema=G)

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("recipient_user_id", sa.String(40), nullable=False),
        sa.Column("project_id", sa.String(40)),
        sa.Column("trigger", sa.String(40), nullable=False),
        sa.Column("template_key", sa.String(64), nullable=False),
        sa.Column("subject_kind", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("safe_context", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("dedup_key", sa.String(200)),
        _ts("created_at"),
        _ts("read_at", nullable=True),
        sa.Column("email_state", sa.String(16)),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        schema=A,
    )
    op.create_index(
        "ix_notifications_recipient_user_id_read_at",
        "notifications",
        ["recipient_user_id", "read_at"],
        schema=A,
    )
    op.create_index(
        "ix_notifications_recipient_user_id_created_at",
        "notifications",
        ["recipient_user_id", "created_at"],
        schema=A,
    )
    op.create_index(
        "uq_notifications_dedup_key",
        "notifications",
        ["dedup_key"],
        unique=True,
        schema=A,
        postgresql_where=sa.text("dedup_key IS NOT NULL"),
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("notification_id", sa.String(40), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(64)),
        _ts("sent_at", nullable=True),
        sa.PrimaryKeyConstraint("notification_id", "channel", name="pk_notification_deliveries"),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["actions.notifications.id"],
            name="fk_notification_deliveries_notification_id_notifications",
            ondelete="CASCADE",
        ),
        schema=A,
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries", schema=A)
    op.drop_table("notifications", schema=A)
    for table in ("undo_tokens", "idempotency_keys", "operation_dismissals", "operations"):
        op.drop_table(table, schema=G)
