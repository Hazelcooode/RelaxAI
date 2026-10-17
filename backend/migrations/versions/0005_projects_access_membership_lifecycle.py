"""projects: Projects, memberships, activity ledger, authorization version (T05);
invitations (T06); archival schedules and deletion coordination (T07)

Revision ID: 0005_projects
Revises: 0004_operations
Create Date: 2026-10-17
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID

revision: str = "0005_projects"
down_revision: str | Sequence[str] | None = "0004_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

P = "projects"


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _project_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["project_id"],
        [f"{P}.projects.id"],
        name=f"fk_{table}_project_id_projects",
    )


def upgrade() -> None:
    # ------------------------------------------------------------------ T05
    op.create_table(
        "projects",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("code", CITEXT, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column("lifecycle", sa.String(20), nullable=False),
        _ts("latest_activity_at", nullable=True),
        sa.Column("created_by", sa.String(40), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("version", sa.Integer, nullable=False),
        _ts("deletion_deadline", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("firm_id", "code", name="uq_projects_firm_id_code"),
        schema=P,
    )
    op.create_index("ix_projects_firm_id_lifecycle", "projects", ["firm_id", "lifecycle"], schema=P)
    op.create_table(
        "project_code_sequences",
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("last_value", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("firm_id", name="pk_project_code_sequences"),
        schema=P,
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("project_id", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        _ts("active_from"),
        _ts("active_to", nullable=True),
        sa.Column("created_by", sa.String(40), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("version", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_memberships"),
        _project_fk("memberships"),
        schema=P,
    )
    op.create_index(
        "uq_memberships_active_project_user",
        "memberships",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("active_to IS NULL"),
        schema=P,
    )
    op.create_index(
        "ix_memberships_user_id_active_to", "memberships", ["user_id", "active_to"], schema=P
    )
    op.create_table(
        "project_activity_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("project_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        _ts("occurred_at"),
        sa.Column("actor_user_id", sa.String(40)),
        sa.Column("source_ref", sa.String(64)),
        sa.PrimaryKeyConstraint("id", name="pk_project_activity_events"),
        _project_fk("project_activity_events"),
        schema=P,
    )
    op.create_index(
        "ix_project_activity_events_project_id_occurred_at",
        "project_activity_events",
        ["project_id", "occurred_at"],
        schema=P,
    )
    op.create_table(
        "authorization_versions",
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("version", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("firm_id", name="pk_authorization_versions"),
        schema=P,
    )

    # ------------------------------------------------------------------ T06
    op.create_table(
        "invitations",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("project_id", sa.String(40), nullable=False),
        sa.Column("email", CITEXT, nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("invited_by", sa.String(40), nullable=False),
        sa.Column("credential_digest", sa.LargeBinary(32), nullable=False),
        sa.Column("credential_id", UUID(as_uuid=True), nullable=False),
        _ts("issued_at"),
        _ts("expires_at"),
        sa.Column("state", sa.String(16), nullable=False),
        _ts("accepted_at", nullable=True),
        sa.Column("accepted_by", sa.String(40)),
        _ts("revoked_at", nullable=True),
        sa.Column("resend_of", sa.String(40)),
        sa.Column("version", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_invitations"),
        sa.UniqueConstraint("credential_digest", name="uq_invitations_credential_digest"),
        sa.UniqueConstraint(
            "project_id", "credential_id", name="uq_invitations_project_id_credential_id"
        ),
        _project_fk("invitations"),
        schema=P,
    )
    op.create_index(
        "uq_invitations_pending_project_email",
        "invitations",
        ["project_id", sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("state = 'Pending'"),
        schema=P,
    )
    op.create_index(
        "ix_invitations_state_expires_at", "invitations", ["state", "expires_at"], schema=P
    )
    op.create_table(
        "invitation_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("invitation_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        _ts("at"),
        sa.Column("actor_user_id", sa.String(40)),
        sa.PrimaryKeyConstraint("id", name="pk_invitation_events"),
        sa.ForeignKeyConstraint(
            ["invitation_id"],
            [f"{P}.invitations.id"],
            name="fk_invitation_events_invitation_id_invitations",
        ),
        schema=P,
    )
    op.create_index(
        "ix_invitation_events_invitation_id", "invitation_events", ["invitation_id"], schema=P
    )

    # ------------------------------------------------------------------ T07
    op.create_table(
        "archival_schedules",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("project_id", sa.String(40), nullable=False),
        sa.Column("scheduled_by", sa.String(40), nullable=False),
        _ts("scheduled_at"),
        _ts("executes_at"),
        _ts("notice_sent_at", nullable=True),
        sa.Column(
            "notice_recipients", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("cancelled_by", sa.String(40)),
        _ts("cancelled_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_archival_schedules"),
        _project_fk("archival_schedules"),
        schema=P,
    )
    op.create_index(
        "uq_archival_schedules_scheduled_project",
        "archival_schedules",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("state = 'Scheduled'"),
        schema=P,
    )
    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.String(40), nullable=False),
        sa.Column("firm_id", sa.String(40), nullable=False),
        sa.Column("project_id", sa.String(40), nullable=False),
        sa.Column("requested_by", sa.String(40), nullable=False),
        _ts("requested_at"),
        _ts("executes_at"),
        sa.Column("cancelled_by", sa.String(40)),
        _ts("cancelled_at", nullable=True),
        _ts("executed_at", nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("operation_id", sa.String(40), nullable=False),
        _ts("confirmation_validated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_deletion_requests"),
        _project_fk("deletion_requests"),
        schema=P,
    )
    op.create_index(
        "uq_deletion_requests_pending_project",
        "deletion_requests",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("state = 'Pending'"),
        schema=P,
    )
    op.create_table(
        "deletion_work_items",
        sa.Column("request_id", sa.String(40), nullable=False),
        sa.Column("store", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error_code", sa.String(64)),
        _ts("started_at", nullable=True),
        _ts("completed_at", nullable=True),
        sa.PrimaryKeyConstraint("request_id", "store", name="pk_deletion_work_items"),
        sa.ForeignKeyConstraint(
            ["request_id"],
            [f"{P}.deletion_requests.id"],
            name="fk_deletion_work_items_request_id_deletion_requests",
        ),
        schema=P,
    )


def downgrade() -> None:
    for table in (
        "deletion_work_items",
        "deletion_requests",
        "archival_schedules",
        "invitation_events",
        "invitations",
        "authorization_versions",
        "project_activity_events",
        "memberships",
        "project_code_sequences",
        "projects",
    ):
        op.drop_table(table, schema=P)
