"""``Audit``: the minimized audit log (GOV-01..03, conventions §4.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import and_, or_, select

from app.modules.governance.domain import (
    AUDIT_CATALOG,
    MAX_REASON_LENGTH,
    ActorRef,
    AuditAction,
    AuditCategory,
    Outcome,
    PersonalDataScreen,
    SafeFacts,
    SystemActor,
    TargetRef,
    actor_columns,
    personal_data_screen,
)
from app.modules.governance.tables import audit_events
from app.platform.clock import Clock
from app.platform.correlation import current_correlation_id
from app.platform.db import Tx, UnitOfWork
from app.platform.errors import AppError, ErrorCode, FieldIssue, ValidationFailed
from app.platform.http.pagination import CursorCodec, Page
from app.platform.ids import new_id
from app.platform.schema import CamelModel

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


class ReasonScreen(Protocol):
    """Flags personal data in a free-text reason.

    Integration point for T11: the privacy module's detector replaces ``RegexReasonScreen``
    in ``container.py``; nothing else changes.
    """

    def contains_personal_data(self, text: str) -> bool: ...


class RegexReasonScreen:
    """Interim local check (emails, phone numbers, registered fixture names)."""

    def __init__(self, screen: PersonalDataScreen = personal_data_screen) -> None:
        self._screen = screen

    def contains_personal_data(self, text: str) -> bool:
        return self._screen.contains_personal_data(text)


# --------------------------------------------------------------------------- viewers


@dataclass(frozen=True, slots=True)
class FirmManagerViewer:
    """Sees the firm's governance actions only (no Project activity)."""


@dataclass(frozen=True, slots=True)
class ProjectAdministratorViewer:
    """Sees one Project's events. T05: build this only from a ``ProjectScope`` minted by
    ``ProjectAccess`` with ``audit.project.read``; routes must never pass a raw request id."""

    project_id: str


@dataclass(frozen=True, slots=True)
class PlatformOperatorViewer:
    """Sees operational events (domains, quotas, jobs, rebuilds, retention, configuration)."""


type AuditViewer = FirmManagerViewer | ProjectAdministratorViewer | PlatformOperatorViewer


@dataclass(frozen=True, slots=True)
class AuditFilters:
    actions: frozenset[AuditAction] = field(default_factory=frozenset)
    from_: datetime | None = None
    to: datetime | None = None
    actor_user_id: str | None = None
    limit: int = DEFAULT_PAGE_SIZE


# --------------------------------------------------------------------------- views


class AuditActorView(CamelModel):
    kind: Literal["User", "System", "PlatformOperator"]
    user_id: str | None = None
    label: str


class AuditTargetView(CamelModel):
    type: str
    id: str


class AuditEventView(CamelModel):
    """Contract §12 ``AuditEvent``."""

    audit_id: str
    at: datetime
    actor: AuditActorView
    action: str
    target: AuditTargetView
    project_id: str | None = None
    outcome: Outcome
    reason: str | None = None
    facts: dict[str, str | int | float | bool | None]


_VIEWER_CATEGORIES: dict[type[object], AuditCategory] = {
    FirmManagerViewer: AuditCategory.GOVERNANCE,
    PlatformOperatorViewer: AuditCategory.OPERATIONAL,
}


class Audit:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        firm_id: str,
        cursors: CursorCodec,
        reason_screen: ReasonScreen,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._firm_id = firm_id
        self._cursors = cursors
        self._reason_screen = reason_screen

    async def record(
        self,
        tx: Tx,
        *,
        actor: ActorRef | SystemActor,
        action: AuditAction,
        target: TargetRef,
        outcome: Outcome,
        project_id: str | None = None,
        reason: str | None = None,
        facts: SafeFacts | None = None,
    ) -> None:
        """Write one event in the caller's transaction (it commits or rolls back with them).

        Raises ``ValueError`` for programming errors (fact keys outside the action's
        allowlist) and ``AppError`` for user-entered reasons that are missing, too long, or
        contain personal data.
        """
        spec = AUDIT_CATALOG[action]
        facts = facts or SafeFacts()
        unknown = set(facts) - spec.facts
        if unknown:
            raise ValueError(f"audit facts {sorted(unknown)} are not allowed for {action.value}")
        reason = self._checked_reason(reason, required=spec.reason_required)
        actor_kind, actor_user_id, actor_label = actor_columns(actor)
        await tx.session.execute(
            audit_events.insert().values(
                id=new_id("aud"),
                at=self._clock.now(),
                firm_id=self._firm_id,
                project_id=project_id,
                category=spec.category.value,
                actor_kind=actor_kind,
                actor_user_id=actor_user_id,
                actor_label=actor_label,
                action=action.value,
                target_type=target.type,
                target_id=target.id,
                outcome=outcome.value,
                reason=reason,
                facts=facts.to_json(),
                correlation_id=current_correlation_id(),
            )
        )

    def _checked_reason(self, reason: str | None, *, required: bool) -> str | None:
        if reason is not None:
            reason = reason.strip() or None
        if reason is None:
            if required:
                raise AppError(ErrorCode.REASON_REQUIRED)
            return None
        if len(reason) > MAX_REASON_LENGTH:
            raise ValidationFailed(
                [FieldIssue(field="reason", code="string_too_long", message="Reason is too long.")]
            )
        if self._reason_screen.contains_personal_data(reason):
            raise AppError(ErrorCode.REASON_CONTAINS_PERSONAL_DATA)
        return reason

    async def query(
        self, viewer: AuditViewer, filters: AuditFilters, cursor: str | None
    ) -> Page[AuditEventView]:
        """Newest first, scoped by viewer. Ordinary actors have no viewer type at all."""
        e = audit_events.c
        conditions: list[Any] = [e.firm_id == self._firm_id]
        if isinstance(viewer, ProjectAdministratorViewer):
            conditions.append(e.project_id == viewer.project_id)
        else:
            conditions.append(e.category == _VIEWER_CATEGORIES[type(viewer)].value)
        if filters.actions:
            conditions.append(e.action.in_(sorted(a.value for a in filters.actions)))
        if filters.from_ is not None:
            conditions.append(e.at >= filters.from_)
        if filters.to is not None:
            conditions.append(e.at < filters.to)
        if filters.actor_user_id is not None:
            conditions.append(e.actor_user_id == filters.actor_user_id)
        if cursor is not None:
            position = self._cursors.decode(cursor)
            if not isinstance(position.k, str):
                raise ValidationFailed([FieldIssue(field="cursor", code="invalid", message="Invalid cursor.")])
            at = datetime.fromisoformat(position.k)
            conditions.append(or_(e.at < at, and_(e.at == at, e.id < position.id)))

        limit = max(1, min(filters.limit, MAX_PAGE_SIZE))
        async with self._uow() as tx:
            rows = (
                (
                    await tx.session.execute(
                        select(audit_events)
                        .where(*conditions)
                        .order_by(e.at.desc(), e.id.desc())
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
        items = [_to_view(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = self._cursors.encode(last["at"].isoformat(), last["id"])
        return Page[AuditEventView](items=items, next_cursor=next_cursor)


def _to_view(row: Any) -> AuditEventView:
    return AuditEventView(
        audit_id=row["id"],
        at=row["at"],
        actor=AuditActorView(
            kind=row["actor_kind"], user_id=row["actor_user_id"], label=row["actor_label"]
        ),
        action=row["action"],
        target=AuditTargetView(type=row["target_type"], id=row["target_id"]),
        project_id=row["project_id"],
        outcome=Outcome(row["outcome"]),
        reason=row["reason"],
        facts=row["facts"],
    )

