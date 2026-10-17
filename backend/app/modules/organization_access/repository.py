"""SQL for organization_access. Internal to the module."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import RowMapping, delete, exists, select
from sqlalchemy.dialects.postgresql import insert

from app.modules.organization_access.domain import (
    Activation,
    FirmManagerState,
    PersonCategory,
)
from app.modules.organization_access.tables import (
    approved_domains,
    firm_manager_assignments,
    firms,
    user_accounts,
)
from app.platform.auth import VerifiedIdentity
from app.platform.db import Tx
from app.platform.ids import new_id

a = user_accounts.c


async def account_by_id(tx: Tx, firm_id: str, user_id: str) -> RowMapping | None:
    stmt = select(user_accounts).where(a.firm_id == firm_id, a.id == user_id)
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def accounts_by_ids(tx: Tx, firm_id: str, user_ids: list[str]) -> list[RowMapping]:
    if not user_ids:
        return []
    stmt = select(user_accounts).where(a.firm_id == firm_id, a.id.in_(user_ids))
    return list((await tx.session.execute(stmt)).mappings().all())


async def account_by_email(tx: Tx, firm_id: str, email: str) -> RowMapping | None:
    stmt = select(user_accounts).where(a.firm_id == firm_id, a.email == email)
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def account_for(
    tx: Tx, firm_id: str, identity: VerifiedIdentity, *, for_update: bool = False
) -> RowMapping | None:
    """By subject first, then by *verified* email (an unverified claim never matches)."""
    by_subject = select(user_accounts).where(
        a.firm_id == firm_id, a.external_subject == identity.subject
    )
    if for_update:
        by_subject = by_subject.with_for_update()
    row = (await tx.session.execute(by_subject)).mappings().one_or_none()
    if row is not None or not identity.email_verified or identity.email is None:
        return row
    by_email = select(user_accounts).where(a.firm_id == firm_id, a.email == identity.email)
    if for_update:
        by_email = by_email.with_for_update()
    return (await tx.session.execute(by_email)).mappings().one_or_none()


async def insert_account(
    tx: Tx,
    *,
    firm_id: str,
    identity: VerifiedIdentity,
    category: PersonCategory,
    now: datetime,
) -> RowMapping | None:
    """Create an active account; ``None`` when a concurrent activation created it first."""
    assert identity.email is not None  # noqa: S101 - callers require a verified email
    stmt = (
        insert(user_accounts)
        .values(
            id=new_id("usr"),
            firm_id=firm_id,
            external_subject=identity.subject,
            email=identity.email,
            email_verified_at=now,
            display_name=identity.display_name or identity.email.split("@", 1)[0],
            person_category=category.value,
            activation_state=Activation.ACTIVE.value,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(*user_accounts.c)
    )
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def is_firm_manager(tx: Tx, firm_id: str, user_id: str) -> bool:
    f = firm_manager_assignments.c
    stmt = select(
        exists().where(
            f.firm_id == firm_id, f.user_id == user_id, f.state == FirmManagerState.ACTIVE.value
        )
    )
    return bool((await tx.session.execute(stmt)).scalar())


async def any_active_firm_manager(tx: Tx, firm_id: str) -> bool:
    f = firm_manager_assignments.c
    stmt = select(exists().where(f.firm_id == firm_id, f.state == FirmManagerState.ACTIVE.value))
    return bool((await tx.session.execute(stmt)).scalar())


async def insert_firm_manager(
    tx: Tx, *, firm_id: str, user_id: str, approved_by: str, now: datetime
) -> str:
    assignment_id = new_id("fmn")
    await tx.session.execute(
        firm_manager_assignments.insert().values(
            id=assignment_id,
            firm_id=firm_id,
            user_id=user_id,
            state=FirmManagerState.ACTIVE.value,
            approved_by=approved_by,
            approved_at=now,
        )
    )
    return assignment_id


async def lock_firm(tx: Tx, firm_id: str) -> RowMapping | None:
    stmt = select(firms).where(firms.c.id == firm_id).with_for_update()
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def insert_firm(tx: Tx, *, firm_id: str, name: str, stamp: str, now: datetime) -> bool:
    stmt = (
        insert(firms)
        .values(id=firm_id, name=name, stamp=stamp, created_at=now)
        .on_conflict_do_nothing()
        .returning(firms.c.id)
    )
    return (await tx.session.execute(stmt)).scalar_one_or_none() is not None


async def approved_domain_names(tx: Tx, firm_id: str) -> frozenset[str]:
    stmt = select(approved_domains.c.domain).where(approved_domains.c.firm_id == firm_id)
    return frozenset(str(d).lower() for d in (await tx.session.execute(stmt)).scalars())


async def list_domains(tx: Tx, firm_id: str) -> list[RowMapping]:
    stmt = (
        select(approved_domains)
        .where(approved_domains.c.firm_id == firm_id)
        .order_by(approved_domains.c.domain)
    )
    return list((await tx.session.execute(stmt)).mappings().all())


async def insert_domain(
    tx: Tx, *, firm_id: str, domain: str, created_by: str, now: datetime
) -> bool:
    stmt = (
        insert(approved_domains)
        .values(firm_id=firm_id, domain=domain, created_at=now, created_by=created_by)
        .on_conflict_do_nothing()
        .returning(approved_domains.c.domain)
    )
    return (await tx.session.execute(stmt)).scalar_one_or_none() is not None


async def delete_domain(tx: Tx, *, firm_id: str, domain: str) -> bool:
    d = approved_domains.c
    stmt = (
        delete(approved_domains).where(d.firm_id == firm_id, d.domain == domain).returning(d.domain)
    )
    return (await tx.session.execute(stmt)).scalar_one_or_none() is not None
