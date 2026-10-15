"""``governance`` schema tables owned by this module (conventions §6)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.platform.db import metadata_for

metadata = metadata_for("governance")


def _ts(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


audit_events = Table(
    "audit_events",
    metadata,
    Column("id", String(40), primary_key=True),
    _ts("at"),
    Column("firm_id", String(40), nullable=False),
    Column("project_id", String(40)),
    Column("category", String(20), nullable=False),
    Column("actor_kind", String(20), nullable=False),
    Column("actor_user_id", String(40)),
    Column("actor_label", String(200), nullable=False),
    Column("action", String(64), nullable=False),
    Column("target_type", String(64), nullable=False),
    Column("target_id", String(256), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("reason", String(500)),
    Column("facts", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("correlation_id", String(128)),
    Index("ix_audit_events_firm_id_at", "firm_id", text("at DESC")),
    Index("ix_audit_events_project_id_at", "project_id", text("at DESC")),
    Index("ix_audit_events_action_at", "action", text("at DESC")),
    Index("ix_audit_events_category_at", "firm_id", "category", text("at DESC")),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), nullable=False),
    Column("kind", String(100), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("state", String(20), nullable=False),
    Column("priority", Integer, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False),
    _ts("run_at"),
    Column("lease_owner", String(200)),
    _ts("lease_expires_at", nullable=True),
    Column("idempotency_key", String(200), nullable=False),
    Column("operation_id", String(40)),
    Column("correlation_id", String(128)),
    Column("last_error_code", String(64)),
    Column("progress_stage", String(64)),
    Column("progress_done", BigInteger),
    Column("progress_total", BigInteger),
    Column("progress_unit", String(20)),
    Column("waiting_action", String(64)),
    Column("waiting_destination", JSONB),
    _ts("created_at"),
    _ts("updated_at"),
    _ts("completed_at", nullable=True),
    UniqueConstraint("kind", "idempotency_key"),
    Index("ix_jobs_state_run_at_priority", "state", "run_at", text("priority DESC")),
    Index("ix_jobs_operation_id", "operation_id"),
    Index("ix_jobs_kind_state", "kind", "state"),
)

usage_meters = Table(
    "usage_meters",
    metadata,
    Column("firm_id", String(40), primary_key=True),
    Column("meter", String(32), primary_key=True),
    Column("period", String(16), primary_key=True),
    Column("used", BigInteger, nullable=False, server_default="0"),
    _ts("updated_at"),
)

quota_policies = Table(
    "quota_policies",
    metadata,
    Column("firm_id", String(40), primary_key=True),
    Column("meter", String(32), primary_key=True),
    Column("quota", BigInteger, nullable=False),
    Column("period", String(16), nullable=False),
)

quota_reservations = Table(
    "quota_reservations",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), nullable=False),
    Column("job_id", String(40)),
    Column("meter", String(32), nullable=False),
    Column("period", String(16), nullable=False),
    Column("units", BigInteger, nullable=False),
    Column("state", String(16), nullable=False),
    _ts("created_at"),
    _ts("expires_at"),
    Index("ix_quota_reservations_active", "firm_id", "meter", "period", "state"),
    Index("ix_quota_reservations_job_id", "job_id"),
)
