"""``org`` schema tables owned by organization_access (conventions §6)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Table, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import CITEXT

from app.platform.db import metadata_for

metadata = metadata_for("org")


def _ts(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


firms = Table(
    "firms",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("stamp", String(63), nullable=False, unique=True),
    _ts("created_at"),
)

approved_domains = Table(
    "approved_domains",
    metadata,
    Column("firm_id", String(40), ForeignKey("org.firms.id"), primary_key=True),
    Column("domain", CITEXT, primary_key=True),
    _ts("created_at"),
    Column("created_by", String(40), nullable=False),
)

user_accounts = Table(
    "user_accounts",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), ForeignKey("org.firms.id"), nullable=False),
    Column("external_subject", String(128), nullable=False),
    Column("email", CITEXT, nullable=False),
    _ts("email_verified_at", nullable=True),
    Column("display_name", String(200), nullable=False),
    Column("person_category", String(24), nullable=False),
    Column("activation_state", String(16), nullable=False),
    _ts("created_at"),
    _ts("disabled_at", nullable=True),
    UniqueConstraint("firm_id", "external_subject"),
    UniqueConstraint("firm_id", "email"),
)

firm_manager_assignments = Table(
    "firm_manager_assignments",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("firm_id", String(40), ForeignKey("org.firms.id"), nullable=False),
    Column("user_id", String(40), ForeignKey("org.user_accounts.id"), nullable=False),
    Column("state", String(16), nullable=False),
    Column("nominated_by", String(40)),
    Column("approved_by", String(40)),
    _ts("approved_at", nullable=True),
    _ts("removed_at", nullable=True),
    Column("reason", String(500)),
    Index(
        "uq_firm_manager_assignments_active_user",
        "firm_id",
        "user_id",
        unique=True,
        postgresql_where=text("state = 'Active'"),
    ),
)
