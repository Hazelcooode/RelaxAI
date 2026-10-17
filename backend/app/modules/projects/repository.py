"""SQL for projects. Internal to the module: other modules reach it only through the
services, and every Project-scoped function here takes a ``ProjectScope`` or an id that a
service has already authorized."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, RowMapping, and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.modules.projects.domain import (
    ActivityKind,
    DeletionState,
    InvitationEventKind,
    InvitationState,
    Lifecycle,
    ProjectRole,
    ScheduleState,
    WorkItemState,
)
from app.modules.projects.tables import (
    archival_schedules,
    authorization_versions,
    deletion_requests,
    deletion_work_items,
    invitation_events,
    invitations,
    memberships,
    project_activity_events,
    project_code_sequences,
    projects,
)
from app.platform.db import Tx
from app.platform.errors import NotAvailable, ProjectReadOnly
from app.platform.ids import new_id

p = projects.c
m = memberships.c


# --------------------------------------------------------------------------- access


def _access_select(firm_id: str, user_id: str) -> Any:
    """Projects joined with the user's *active* membership (if any)."""
    return (
        select(
            p.id,
            p.firm_id,
            p.name,
            p.code,
            p.lifecycle,
            p.latest_activity_at,
            m.id.label("membership_id"),
            m.role,
        )
        .select_from(
            projects.outerjoin(
                memberships,
                and_(m.project_id == p.id, m.user_id == user_id, m.active_to.is_(None)),
            )
        )
        .where(p.firm_id == firm_id)
    )


async def access_row(tx: Tx, firm_id: str, user_id: str, project_id: str) -> RowMapping | None:
    stmt = _access_select(firm_id, user_id).where(p.id == project_id)
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def access_rows(
    tx: Tx, firm_id: str, user_id: str, project_ids: Sequence[str]
) -> list[RowMapping]:
    stmt = _access_select(firm_id, user_id).where(p.id.in_(list(project_ids)))
    return list((await tx.session.execute(stmt)).mappings().all())


async def member_rows(
    tx: Tx, firm_id: str, user_id: str, lifecycles: Collection[Lifecycle]
) -> list[RowMapping]:
    """The user's active memberships on Projects in ``lifecycles``, ordered by name."""
    stmt = (
        _access_select(firm_id, user_id)
        .where(m.id.is_not(None), p.lifecycle.in_([lc.value for lc in lifecycles]))
        .order_by(p.name, p.id)
    )
    return list((await tx.session.execute(stmt)).mappings().all())


# --------------------------------------------------------------------------- authorization version


async def authorization_counter(tx: Tx, firm_id: str) -> int:
    stmt = select(authorization_versions.c.version).where(
        authorization_versions.c.firm_id == firm_id
    )
    value = (await tx.session.execute(stmt)).scalar_one_or_none()
    return int(value or 0)


async def bump_authorization_version(tx: Tx, firm_id: str) -> None:
    v = authorization_versions.c
    await tx.session.execute(
        insert(authorization_versions).values(firm_id=firm_id, version=0).on_conflict_do_nothing()
    )
    await tx.session.execute(
        update(authorization_versions).where(v.firm_id == firm_id).values(version=v.version + 1)
    )


# --------------------------------------------------------------------------- projects


async def next_code_sequence(tx: Tx, firm_id: str) -> int:
    """Next per-firm Project code number; the row lock serializes concurrent creations."""
    c = project_code_sequences.c
    await tx.session.execute(
        insert(project_code_sequences)
        .values(firm_id=firm_id, last_value=0)
        .on_conflict_do_nothing()
    )
    stmt = (
        update(project_code_sequences)
        .where(c.firm_id == firm_id)
        .values(last_value=c.last_value + 1)
        .returning(c.last_value)
    )
    return int((await tx.session.execute(stmt)).scalar_one())


async def insert_project(
    tx: Tx,
    *,
    firm_id: str,
    code: str,
    name: str,
    description: str | None,
    created_by: str,
    now: datetime,
) -> RowMapping:
    stmt = (
        insert(projects)
        .values(
            id=new_id("prj"),
            firm_id=firm_id,
            code=code,
            name=name,
            description=description,
            lifecycle=Lifecycle.ACTIVE.value,
            latest_activity_at=None,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            version=1,
            deletion_deadline=None,
        )
        .returning(*projects.c)
    )
    return (await tx.session.execute(stmt)).mappings().one()


async def project_row(
    tx: Tx, firm_id: str, project_id: str, *, for_update: bool = False
) -> RowMapping | None:
    stmt = select(projects).where(p.firm_id == firm_id, p.id == project_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def lock_writable_project(tx: Tx, project_id: str) -> None:
    """Share-lock the Project row and require it Active, so a membership or invitation
    write cannot commit alongside a concurrent archive or deletion request (those take the
    row ``FOR UPDATE``). Archived -> ``PROJECT_READ_ONLY``; gone -> ``NOT_AVAILABLE``."""
    stmt = select(p.lifecycle).where(p.id == project_id).with_for_update(read=True)
    lifecycle = (await tx.session.execute(stmt)).scalar_one_or_none()
    if lifecycle is None or lifecycle in (
        Lifecycle.PENDING_DELETION.value,
        Lifecycle.DELETED.value,
    ):
        raise NotAvailable()
    if lifecycle != Lifecycle.ACTIVE.value:
        raise ProjectReadOnly()


async def project_rows(tx: Tx, firm_id: str, project_ids: Sequence[str]) -> list[RowMapping]:
    stmt = select(projects).where(p.firm_id == firm_id, p.id.in_(list(project_ids)))
    return list((await tx.session.execute(stmt)).mappings().all())


async def page_member_projects(
    tx: Tx,
    firm_id: str,
    user_id: str,
    lifecycles: Collection[Lifecycle],
    *,
    after: tuple[str, str] | None,
    limit: int,
) -> list[RowMapping]:
    """Projects (full rows) the user is an active member of, by ``(name, id)``."""
    stmt = (
        select(projects, m.role)
        .select_from(
            projects.join(
                memberships,
                and_(m.project_id == p.id, m.user_id == user_id, m.active_to.is_(None)),
            )
        )
        .where(p.firm_id == firm_id, p.lifecycle.in_([lc.value for lc in lifecycles]))
        .order_by(p.name, p.id)
        .limit(limit)
    )
    if after is not None:
        name, last_id = after
        stmt = stmt.where(or_(p.name > name, and_(p.name == name, p.id > last_id)))
    return list((await tx.session.execute(stmt)).mappings().all())


async def set_lifecycle(
    tx: Tx,
    project_id: str,
    lifecycle: Lifecycle,
    *,
    now: datetime,
    deletion_deadline: datetime | None = None,
) -> RowMapping:
    """Move the lifecycle and bump the version (callers have checked the transition)."""
    stmt = (
        update(projects)
        .where(p.id == project_id)
        .values(
            lifecycle=lifecycle.value,
            deletion_deadline=deletion_deadline,
            updated_at=now,
            version=p.version + 1,
        )
        .returning(*projects.c)
    )
    return (await tx.session.execute(stmt)).mappings().one()


# --------------------------------------------------------------------------- memberships


async def insert_membership(
    tx: Tx,
    *,
    firm_id: str,
    project_id: str,
    user_id: str,
    role: ProjectRole,
    created_by: str,
    now: datetime,
) -> RowMapping:
    stmt = (
        insert(memberships)
        .values(
            id=new_id("mem"),
            firm_id=firm_id,
            project_id=project_id,
            user_id=user_id,
            role=role.value,
            active_from=now,
            active_to=None,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            version=1,
        )
        .returning(*memberships.c)
    )
    return (await tx.session.execute(stmt)).mappings().one()


async def active_membership_for_user(tx: Tx, project_id: str, user_id: str) -> RowMapping | None:
    stmt = select(memberships).where(
        m.project_id == project_id, m.user_id == user_id, m.active_to.is_(None)
    )
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def active_membership(
    tx: Tx, project_id: str, membership_id: str, *, for_update: bool = False
) -> RowMapping | None:
    stmt = select(memberships).where(
        m.project_id == project_id, m.id == membership_id, m.active_to.is_(None)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def active_members(tx: Tx, project_id: str) -> list[RowMapping]:
    stmt = (
        select(memberships)
        .where(m.project_id == project_id, m.active_to.is_(None))
        .order_by(m.active_from, m.id)
    )
    return list((await tx.session.execute(stmt)).mappings().all())


async def page_active_members(
    tx: Tx, project_id: str, *, after: tuple[datetime, str] | None, limit: int
) -> list[RowMapping]:
    stmt = (
        select(memberships)
        .where(m.project_id == project_id, m.active_to.is_(None))
        .order_by(m.active_from, m.id)
        .limit(limit)
    )
    if after is not None:
        at, last_id = after
        stmt = stmt.where(or_(m.active_from > at, and_(m.active_from == at, m.id > last_id)))
    return list((await tx.session.execute(stmt)).mappings().all())


async def lock_administrators(tx: Tx, project_id: str) -> list[RowMapping]:
    """Lock the active Administrator rows: serializes final-administrator checks."""
    stmt = (
        select(memberships)
        .where(
            m.project_id == project_id,
            m.active_to.is_(None),
            m.role == ProjectRole.ADMINISTRATOR.value,
        )
        .order_by(m.id)
        .with_for_update()
    )
    return list((await tx.session.execute(stmt)).mappings().all())


async def administrator_user_ids(tx: Tx, project_id: str) -> list[str]:
    stmt = (
        select(m.user_id)
        .where(
            m.project_id == project_id,
            m.active_to.is_(None),
            m.role == ProjectRole.ADMINISTRATOR.value,
        )
        .order_by(m.user_id)
    )
    return [str(u) for u in (await tx.session.execute(stmt)).scalars()]


# --------------------------------------------------------------------------- activity ledger


async def record_activity(
    tx: Tx,
    *,
    firm_id: str,
    project_id: str,
    kind: ActivityKind,
    occurred_at: datetime,
    actor_user_id: str | None,
    source_ref: str | None,
) -> None:
    await tx.session.execute(
        project_activity_events.insert().values(
            firm_id=firm_id,
            project_id=project_id,
            kind=kind.value,
            occurred_at=occurred_at,
            actor_user_id=actor_user_id,
            source_ref=source_ref,
        )
    )
    await tx.session.execute(
        update(projects)
        .where(p.id == project_id)
        .values(latest_activity_at=func.greatest(func.coalesce(p.latest_activity_at, occurred_at),
                                                 occurred_at))
    )  # fmt: skip


# --------------------------------------------------------------------------- lifecycle reads


async def scheduled_archivals(tx: Tx, project_ids: Sequence[str]) -> dict[str, RowMapping]:
    a = archival_schedules.c
    if not project_ids:
        return {}
    stmt = select(archival_schedules).where(
        a.project_id.in_(list(project_ids)), a.state == ScheduleState.SCHEDULED.value
    )
    return {r["project_id"]: r for r in (await tx.session.execute(stmt)).mappings()}


async def pending_deletions(tx: Tx, project_ids: Sequence[str]) -> dict[str, RowMapping]:
    d = deletion_requests.c
    if not project_ids:
        return {}
    stmt = select(deletion_requests).where(
        d.project_id.in_(list(project_ids)), d.state == DeletionState.PENDING.value
    )
    return {r["project_id"]: r for r in (await tx.session.execute(stmt)).mappings()}


# --------------------------------------------------------------------------- invitations

i = invitations.c


async def insert_invitation(
    tx: Tx,
    *,
    firm_id: str,
    project_id: str,
    email: str,
    role: ProjectRole,
    invited_by: str,
    digest: bytes,
    now: datetime,
    expires_at: datetime,
    resend_of: str | None,
) -> RowMapping:
    stmt = (
        insert(invitations)
        .values(
            id=new_id("inv"),
            firm_id=firm_id,
            project_id=project_id,
            email=email,
            role=role.value,
            invited_by=invited_by,
            credential_digest=digest,
            credential_id=uuid.uuid4(),
            issued_at=now,
            expires_at=expires_at,
            state=InvitationState.PENDING.value,
            resend_of=resend_of,
            version=1,
        )
        .returning(*invitations.c)
    )
    return (await tx.session.execute(stmt)).mappings().one()


async def invitation(
    tx: Tx, project_id: str, invitation_id: str, *, for_update: bool = False
) -> RowMapping | None:
    stmt = select(invitations).where(i.project_id == project_id, i.id == invitation_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def invitation_by_digest(
    tx: Tx, firm_id: str, digest: bytes, *, for_update: bool = False
) -> RowMapping | None:
    stmt = select(invitations).where(i.firm_id == firm_id, i.credential_digest == digest)
    if for_update:
        stmt = stmt.with_for_update()
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def pending_invitation_for_email(tx: Tx, project_id: str, email: str) -> RowMapping | None:
    stmt = select(invitations).where(
        i.project_id == project_id,
        func.lower(i.email) == email.lower(),
        i.state == InvitationState.PENDING.value,
    )
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def page_invitations(
    tx: Tx,
    project_id: str,
    *,
    states: Collection[InvitationState] | None,
    expired_before: datetime,
    after: tuple[datetime, str] | None,
    limit: int,
) -> list[RowMapping]:
    """Newest first. ``states`` filters on the *effective* state (lazy expiry applied)."""
    stmt = select(invitations).where(i.project_id == project_id)
    if states is not None:
        conditions: list[ColumnElement[bool]] = []
        stored = {s for s in states if s is not InvitationState.PENDING}
        stored.discard(InvitationState.EXPIRED)
        if stored:
            conditions.append(i.state.in_([s.value for s in stored]))
        if InvitationState.PENDING in states:
            conditions.append(
                and_(i.state == InvitationState.PENDING.value, i.expires_at > expired_before)
            )
        if InvitationState.EXPIRED in states:
            conditions.append(i.state == InvitationState.EXPIRED.value)
            conditions.append(
                and_(i.state == InvitationState.PENDING.value, i.expires_at <= expired_before)
            )
        stmt = stmt.where(or_(*conditions))
    if after is not None:
        at, last_id = after
        stmt = stmt.where(or_(i.issued_at < at, and_(i.issued_at == at, i.id < last_id)))
    stmt = stmt.order_by(i.issued_at.desc(), i.id.desc()).limit(limit)
    return list((await tx.session.execute(stmt)).mappings().all())


async def set_invitation_state(
    tx: Tx, invitation_id: str, state: InvitationState, **values: Any
) -> RowMapping:
    stmt = (
        update(invitations)
        .where(i.id == invitation_id)
        .values(state=state.value, version=i.version + 1, **values)
        .returning(*invitations.c)
    )
    return (await tx.session.execute(stmt)).mappings().one()


async def expire_due_invitations(
    tx: Tx, now: datetime, *, project_id: str | None = None, email: str | None = None
) -> list[tuple[str, str]]:
    """Move Pending invitations past expiry to Expired; returns ``(id, project_id)``."""
    stmt = update(invitations).where(i.state == InvitationState.PENDING.value, i.expires_at <= now)
    if project_id is not None:
        stmt = stmt.where(i.project_id == project_id)
    if email is not None:
        stmt = stmt.where(func.lower(i.email) == email.lower())
    stmt = stmt.values(state=InvitationState.EXPIRED.value, version=i.version + 1).returning(
        i.id, i.project_id
    )
    return [(str(r.id), str(r.project_id)) for r in await tx.session.execute(stmt)]


async def record_invitation_event(
    tx: Tx,
    invitation_id: str,
    kind: InvitationEventKind,
    *,
    at: datetime,
    actor_user_id: str | None,
) -> None:
    await tx.session.execute(
        invitation_events.insert().values(
            invitation_id=invitation_id, kind=kind.value, at=at, actor_user_id=actor_user_id
        )
    )


# --------------------------------------------------------------------------- archival schedules


async def insert_schedule(
    tx: Tx,
    *,
    firm_id: str,
    project_id: str,
    scheduled_by: str,
    now: datetime,
    executes_at: datetime,
    recipients: Sequence[str],
) -> RowMapping:
    stmt = (
        insert(archival_schedules)
        .values(
            id=new_id("arc"),
            firm_id=firm_id,
            project_id=project_id,
            scheduled_by=scheduled_by,
            scheduled_at=now,
            executes_at=executes_at,
            notice_sent_at=now if recipients else None,
            notice_recipients=list(recipients),
            state=ScheduleState.SCHEDULED.value,
        )
        .returning(*archival_schedules.c)
    )
    return (await tx.session.execute(stmt)).mappings().one()


async def schedule(tx: Tx, schedule_id: str, *, for_update: bool = False) -> RowMapping | None:
    stmt = select(archival_schedules).where(archival_schedules.c.id == schedule_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def set_schedule_state(tx: Tx, schedule_id: str, state: ScheduleState, **values: Any) -> None:
    await tx.session.execute(
        update(archival_schedules)
        .where(archival_schedules.c.id == schedule_id)
        .values(state=state.value, **values)
    )


# --------------------------------------------------------------------------- deletion


async def insert_deletion_request(
    tx: Tx,
    *,
    firm_id: str,
    project_id: str,
    requested_by: str,
    now: datetime,
    executes_at: datetime,
    operation_id: str,
) -> RowMapping:
    stmt = (
        insert(deletion_requests)
        .values(
            id=new_id("dlr"),
            firm_id=firm_id,
            project_id=project_id,
            requested_by=requested_by,
            requested_at=now,
            executes_at=executes_at,
            state=DeletionState.PENDING.value,
            operation_id=operation_id,
            confirmation_validated_at=now,
        )
        .returning(*deletion_requests.c)
    )
    return (await tx.session.execute(stmt)).mappings().one()


async def deletion_request(tx: Tx, request_id: str) -> RowMapping | None:
    stmt = select(deletion_requests).where(deletion_requests.c.id == request_id)
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def latest_deletion_request(
    tx: Tx, project_id: str, state: DeletionState
) -> RowMapping | None:
    d = deletion_requests.c
    stmt = (
        select(deletion_requests)
        .where(d.project_id == project_id, d.state == state.value)
        .order_by(d.requested_at.desc(), d.id.desc())
        .limit(1)
    )
    return (await tx.session.execute(stmt)).mappings().one_or_none()


async def set_request_state(tx: Tx, request_id: str, state: DeletionState, **values: Any) -> None:
    await tx.session.execute(
        update(deletion_requests)
        .where(deletion_requests.c.id == request_id)
        .values(state=state.value, **values)
    )


async def ensure_work_items(tx: Tx, request_id: str, stores: Sequence[str]) -> None:
    """One work item per registered store, in purge order; existing items are kept."""
    if not stores:
        return
    await tx.session.execute(
        insert(deletion_work_items)
        .values(
            [
                {
                    "request_id": request_id,
                    "store": store,
                    "position": position,
                    "state": WorkItemState.PENDING.value,
                    "attempts": 0,
                }
                for position, store in enumerate(stores)
            ]
        )
        .on_conflict_do_nothing(index_elements=["request_id", "store"])
    )


async def work_items(tx: Tx, request_id: str) -> list[RowMapping]:
    w = deletion_work_items.c
    stmt = (
        select(deletion_work_items).where(w.request_id == request_id).order_by(w.position, w.store)
    )
    return list((await tx.session.execute(stmt)).mappings().all())


async def start_work_item(tx: Tx, request_id: str, store: str, *, now: datetime) -> None:
    w = deletion_work_items.c
    await tx.session.execute(
        update(deletion_work_items)
        .where(w.request_id == request_id, w.store == store)
        .values(
            state=WorkItemState.RUNNING.value,
            attempts=w.attempts + 1,
            started_at=func.coalesce(w.started_at, now),
        )
    )


async def update_work_item(tx: Tx, request_id: str, store: str, **values: Any) -> None:
    w = deletion_work_items.c
    await tx.session.execute(
        update(deletion_work_items)
        .where(w.request_id == request_id, w.store == store)
        .values(**values)
    )


async def purge_project_metadata(tx: Tx, project_id: str) -> None:
    """Deletion's last store: Project-scoped metadata beyond the tombstone row."""
    invitation_ids = select(i.id).where(i.project_id == project_id).scalar_subquery()
    await tx.session.execute(
        delete(invitation_events).where(invitation_events.c.invitation_id.in_(invitation_ids))
    )
    await tx.session.execute(delete(invitations).where(i.project_id == project_id))
    await tx.session.execute(
        delete(project_activity_events).where(project_activity_events.c.project_id == project_id)
    )
    await tx.session.execute(
        delete(archival_schedules).where(archival_schedules.c.project_id == project_id)
    )


async def project_metadata_remains(tx: Tx, project_id: str) -> bool:
    checks = [
        select(i.id).where(i.project_id == project_id),
        select(project_activity_events.c.id).where(
            project_activity_events.c.project_id == project_id
        ),
        select(archival_schedules.c.id).where(archival_schedules.c.project_id == project_id),
    ]
    for check in checks:
        if (await tx.session.execute(check.limit(1))).first() is not None:
            return True
    return False
