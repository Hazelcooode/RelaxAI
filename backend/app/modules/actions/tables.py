"""``actions`` schema tables owned by this module (T04: notifications; T25/T26 add tasks)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.platform.db import metadata_for

metadata = metadata_for("actions")


def _ts(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


notifications = Table(
    "notifications",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), nullable=False),
    Column("recipient_user_id", String(40), nullable=False),
    Column("project_id", String(40)),
    Column("trigger", String(40), nullable=False),
    Column("template_key", String(64), nullable=False),
    Column("subject_kind", String(40), nullable=False),
    Column("subject_id", String(64), nullable=False),
    Column("safe_context", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("dedup_key", String(200)),
    _ts("created_at"),
    _ts("read_at", nullable=True),
    Column("email_state", String(16)),
    Index("ix_notifications_recipient_user_id_read_at", "recipient_user_id", "read_at"),
    Index("ix_notifications_recipient_user_id_created_at", "recipient_user_id", "created_at"),
    Index(
        "uq_notifications_dedup_key",
        "dedup_key",
        unique=True,
        postgresql_where=text("dedup_key IS NOT NULL"),
    ),
)

notification_deliveries = Table(
    "notification_deliveries",
    metadata,
    Column(
        "notification_id",
        String(40),
        ForeignKey("actions.notifications.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("channel", String(16), nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error_code", String(64)),
    _ts("sent_at", nullable=True),
    PrimaryKeyConstraint("notification_id", "channel"),
)
