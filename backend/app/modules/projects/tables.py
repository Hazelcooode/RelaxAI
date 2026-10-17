"""``projects`` schema tables (conventions §6): Projects, memberships, the Latest Project
Activity ledger, the authorization version counter (T05); invitations (T06); archival
schedules and deletion coordination (T07)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID

from app.platform.db import metadata_for

metadata = metadata_for("projects")


def _ts(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


projects = Table(
    "projects",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), nullable=False),
    Column("code", CITEXT, nullable=False),
    Column("name", String(120), nullable=False),
    Column("description", String(1000)),
    Column("lifecycle", String(20), nullable=False),
    _ts("latest_activity_at", nullable=True),
    Column("created_by", String(40), nullable=False),
    _ts("created_at"),
    _ts("updated_at"),
    Column("version", Integer, nullable=False),
    _ts("deletion_deadline", nullable=True),
    UniqueConstraint("firm_id", "code"),
    Index("ix_projects_firm_id_lifecycle", "firm_id", "lifecycle"),
)

# Per-firm sequence behind Project codes ("ATL-001").
project_code_sequences = Table(
    "project_code_sequences",
    metadata,
    Column("firm_id", String(40), primary_key=True),
    Column("last_value", Integer, nullable=False),
)

memberships = Table(
    "memberships",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), nullable=False),
    Column("project_id", String(40), ForeignKey("projects.projects.id"), nullable=False),
    Column("user_id", String(40), nullable=False),
    Column("role", String(24), nullable=False),
    _ts("active_from"),
    _ts("active_to", nullable=True),
    Column("created_by", String(40), nullable=False),
    _ts("created_at"),
    _ts("updated_at"),
    Column("version", Integer, nullable=False),
    # A person holds at most one active membership per Project.
    Index(
        "uq_memberships_active_project_user",
        "project_id",
        "user_id",
        unique=True,
        postgresql_where=text("active_to IS NULL"),
    ),
    Index("ix_memberships_user_id_active_to", "user_id", "active_to"),
)

# Append-only Latest Project Activity ledger (PRJ-05).
project_activity_events = Table(
    "project_activity_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("firm_id", String(40), nullable=False),
    Column("project_id", String(40), ForeignKey("projects.projects.id"), nullable=False),
    Column("kind", String(40), nullable=False),
    _ts("occurred_at"),
    Column("actor_user_id", String(40)),
    Column("source_ref", String(64)),
    Index("ix_project_activity_events_project_id_occurred_at", "project_id", "occurred_at"),
)

# Firm-wide counter bumped by every membership, role, or lifecycle change (``Me``).
authorization_versions = Table(
    "authorization_versions",
    metadata,
    Column("firm_id", String(40), primary_key=True),
    Column("version", BigInteger, nullable=False),
)

# --------------------------------------------------------------------------- T06

invitations = Table(
    "invitations",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), nullable=False),
    Column("project_id", String(40), ForeignKey("projects.projects.id"), nullable=False),
    Column("email", CITEXT, nullable=False),
    Column("role", String(24), nullable=False),
    Column("invited_by", String(40), nullable=False),
    Column("credential_digest", LargeBinary(32), nullable=False, unique=True),
    Column("credential_id", UUID(as_uuid=True), nullable=False),
    _ts("issued_at"),
    _ts("expires_at"),
    Column("state", String(16), nullable=False),
    _ts("accepted_at", nullable=True),
    Column("accepted_by", String(40)),
    _ts("revoked_at", nullable=True),
    Column("resend_of", String(40)),
    Column("version", Integer, nullable=False),
    UniqueConstraint("project_id", "credential_id"),
    Index(
        "uq_invitations_pending_project_email",
        "project_id",
        func.lower(text("email")),
        unique=True,
        postgresql_where=text("state = 'Pending'"),
    ),
    Index("ix_invitations_state_expires_at", "state", "expires_at"),
)

invitation_events = Table(
    "invitation_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("invitation_id", String(40), ForeignKey("projects.invitations.id"), nullable=False),
    Column("kind", String(16), nullable=False),
    _ts("at"),
    Column("actor_user_id", String(40)),
    Index("ix_invitation_events_invitation_id", "invitation_id"),
)

# --------------------------------------------------------------------------- T07

archival_schedules = Table(
    "archival_schedules",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), nullable=False),
    Column("project_id", String(40), ForeignKey("projects.projects.id"), nullable=False),
    Column("scheduled_by", String(40), nullable=False),
    _ts("scheduled_at"),
    _ts("executes_at"),
    _ts("notice_sent_at", nullable=True),
    Column("notice_recipients", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("state", String(16), nullable=False),
    Column("cancelled_by", String(40)),
    _ts("cancelled_at", nullable=True),
    # At most one live schedule per Project; executed and cancelled ones stay as history.
    Index(
        "uq_archival_schedules_scheduled_project",
        "project_id",
        unique=True,
        postgresql_where=text("state = 'Scheduled'"),
    ),
)

deletion_requests = Table(
    "deletion_requests",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), nullable=False),
    Column("project_id", String(40), ForeignKey("projects.projects.id"), nullable=False),
    Column("requested_by", String(40), nullable=False),
    _ts("requested_at"),
    _ts("executes_at"),
    Column("cancelled_by", String(40)),
    _ts("cancelled_at", nullable=True),
    _ts("executed_at", nullable=True),
    Column("state", String(16), nullable=False),
    Column("operation_id", String(40), nullable=False),
    _ts("confirmation_validated_at"),
    Index(
        "uq_deletion_requests_pending_project",
        "project_id",
        unique=True,
        postgresql_where=text("state = 'Pending'"),
    ),
)

# Content-free by construction: store names, states, and timestamps only.
deletion_work_items = Table(
    "deletion_work_items",
    metadata,
    Column("request_id", String(40), ForeignKey("projects.deletion_requests.id"), nullable=False),
    Column("store", String(64), nullable=False),
    Column("position", Integer, nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("last_error_code", String(64)),
    _ts("started_at", nullable=True),
    _ts("completed_at", nullable=True),
    PrimaryKeyConstraint("request_id", "store"),
)
