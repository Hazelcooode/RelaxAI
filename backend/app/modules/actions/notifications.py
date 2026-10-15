"""``Notifications``: the notification store with in-product and email delivery (T04).

``enqueue`` writes the notification, one delivery row per channel, and the email delivery
job inside the caller's transaction: in-product visibility is immediate once committed (the
five-second target for a polling client), and email is a ``HIGH`` priority job (the
two-minute target). Owning modules decide *when* to notify; this module decides what may be
said and to whom.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.modules.actions.domain import (
    ALL_CHANNELS,
    TEMPLATES,
    Channel,
    DeliveryState,
    NotificationTemplate,
    NotificationTrigger,
    SafeSubjectRef,
    dedup_key,
)
from app.modules.actions.tables import notification_deliveries, notifications
from app.modules.governance import (
    Destination,
    JobQueue,
    Priority,
    ProjectLabel,
    UndoRef,
    UndoService,
)
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork
from app.platform.email import EmailMessage, EmailSender
from app.platform.errors import NotAvailable, UpstreamError
from app.platform.http.pagination import CursorCodec, Page
from app.platform.ids import new_id
from app.platform.schema import CamelModel, OmitWhenNone

EMAIL_DELIVER: Final = "notifications.email_deliver"
UNDO_KIND: Final = "notification.read"
_PAGE_SIZE = 25
_EMAIL_FOOTER = "\n\nOpen RelaxAI to see the details."


class EmailDeliveryPayload(BaseModel):
    notification_id: str


# --------------------------------------------------------------------------- ports


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: str
    email: str
    client_collaborator: bool
    active: bool


class RecipientDirectory(Protocol):
    """Who a ``usr_`` id is. organization_access supplies the adapter."""

    async def recipient(self, user_id: str) -> Recipient | None: ...


class NotificationActor(Protocol):
    @property
    def user_id(self) -> str | None: ...


class DestinationAccess(Protocol):
    """Reauthorization seam shared with ``Operations``; T05's ``ProjectAccess`` adapter."""

    async def destination_authorized(self, actor: Any, destination: Destination) -> bool: ...

    async def project_label(self, actor: Any, project_id: str) -> ProjectLabel | None: ...


# --------------------------------------------------------------------------- views


class NotificationView(OmitWhenNone):
    """Contract §5.3 ``Notification``; ``project`` is omitted unless authorized."""

    omit_when_none = frozenset({"project"})

    notification_id: str
    trigger: NotificationTrigger
    project: ProjectLabel | None = None
    title: str
    body: str
    created_at: datetime
    read_at: datetime | None


class NotificationTarget(CamelModel):
    """``{available: false}`` says nothing about what the target was (NOTE-07)."""

    available: bool
    destination: Destination | None = None


class NotificationReadState(CamelModel):
    unread_count: int


# --------------------------------------------------------------------------- service


class Notifications:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        firm_id: str,
        queue: JobQueue,
        recipients: RecipientDirectory,
        email: EmailSender,
        undo: UndoService,
        cursors: CursorCodec,
        access: DestinationAccess,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._firm_id = firm_id
        self._queue = queue
        self._recipients = recipients
        self._email = email
        self._undo = undo
        self._cursors = cursors
        self.access = access
        undo.register(UNDO_KIND, _ReadStateCompensator(self))

    # ------------------------------------------------------------------ producers

    async def enqueue(
        self,
        tx: Tx,
        *,
        recipient_user_id: str,
        trigger: NotificationTrigger,
        template_key: str,
        subject_ref: SafeSubjectRef,
        safe_context: Mapping[str, str | int],
        project_id: str | None,
        channels: frozenset[Channel] = ALL_CHANNELS,
    ) -> str:
        """Write a notification in the caller's transaction and return its id.

        Raises ``ValueError`` (a programming error in the caller) for an unknown template, a
        trigger that does not match it, context outside the template's allowlist or
        patterns, an unknown or inactive recipient, or an internal-only template addressed
        to a Client Collaborator. Repeatable triggers update the day's existing notification
        instead of adding another.
        """
        template = TEMPLATES.get(template_key)
        if template is None or template.trigger is not trigger:
            raise ValueError(f"unknown notification template {template_key!r} for {trigger}")
        context = template.validate(safe_context)
        if not channels or not channels <= ALL_CHANNELS:
            raise ValueError("notification channels must be a non-empty subset of the channels")
        recipient = await self._recipients.recipient(recipient_user_id)
        if recipient is None or not recipient.active:
            raise ValueError("notification recipient is not an active account")
        if recipient.client_collaborator and not template.client_visible:
            raise ValueError(f"{template_key} is internal and cannot reach a Client Collaborator")

        now = self._clock.now()
        key = dedup_key(template, recipient_user_id, subject_ref, now)
        n = notifications.c
        if key is not None:
            existing = (
                await tx.session.execute(
                    update(notifications)
                    .where(n.dedup_key == key)
                    .values(safe_context=context)
                    .returning(n.id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return str(existing)
        notification_id = new_id("ntf")
        email = Channel.EMAIL in channels
        inserted = (
            await tx.session.execute(
                insert(notifications)
                .values(
                    id=notification_id,
                    firm_id=self._firm_id,
                    recipient_user_id=recipient_user_id,
                    project_id=project_id,
                    trigger=trigger.value,
                    template_key=template_key,
                    subject_kind=subject_ref.kind,
                    subject_id=subject_ref.id,
                    safe_context=context,
                    dedup_key=key,
                    created_at=now,
                    email_state=DeliveryState.PENDING.value if email else None,
                )
                .on_conflict_do_nothing()
                .returning(n.id)
            )
        ).scalar_one_or_none()
        if inserted is None:  # a concurrent enqueue created the day's notification
            existing_id = (
                await tx.session.execute(select(n.id).where(n.dedup_key == key))
            ).scalar_one()
            return str(existing_id)
        await tx.session.execute(
            notification_deliveries.insert(),
            [
                {
                    "notification_id": notification_id,
                    "channel": channel.value,
                    "state": (
                        DeliveryState.DELIVERED if channel is Channel.IN_PRODUCT
                        else DeliveryState.PENDING
                    ).value,
                    "sent_at": now if channel is Channel.IN_PRODUCT else None,
                }
                for channel in sorted(channels)
            ],
        )  # fmt: skip
        if email:
            await self._queue.enqueue(
                tx,
                EMAIL_DELIVER,
                EmailDeliveryPayload(notification_id=notification_id),
                idempotency_key=f"email:{notification_id}",
                priority=Priority.HIGH,
            )
        return notification_id

    # ------------------------------------------------------------------ readers

    async def list(
        self,
        actor: NotificationActor,
        *,
        unread_only: bool,
        cursor: str | None,
        limit: int = _PAGE_SIZE,
    ) -> tuple[Page[NotificationView], int]:
        """The actor's own notifications, newest first, plus the unread count."""
        if actor.user_id is None:
            raise NotAvailable()
        n = notifications.c
        mine = [n.firm_id == self._firm_id, n.recipient_user_id == actor.user_id]
        conditions = list(mine)
        if unread_only:
            conditions.append(n.read_at.is_(None))
        if cursor is not None:
            position = self._cursors.decode(cursor)
            if not isinstance(position.k, str):
                raise NotAvailable()
            at = datetime.fromisoformat(position.k)
            conditions.append(or_(n.created_at < at, and_(n.created_at == at, n.id < position.id)))
        async with self._uow() as tx:
            rows = (
                (
                    await tx.session.execute(
                        select(notifications)
                        .where(*conditions)
                        .order_by(n.created_at.desc(), n.id.desc())
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            unread = (
                await tx.session.execute(select(func.count()).where(*mine, n.read_at.is_(None)))
            ).scalar_one()
        items = [await self._view(actor, row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = self._cursors.encode(last["created_at"].isoformat(), last["id"])
        return Page[NotificationView](items=items, next_cursor=next_cursor), int(unread)

    async def target(self, actor: NotificationActor, notification_id: str) -> NotificationTarget:
        """Non-enumerating: missing, someone else's, and no-longer-authorized targets all
        answer ``{available: false}`` (NOTE-07)."""
        async with self._uow() as tx:
            row = await self._own_row(tx, actor, notification_id)
        if row is None:
            return NotificationTarget(available=False)
        template = TEMPLATES[row["template_key"]]
        destination = template.destination(
            row["project_id"], SafeSubjectRef(row["subject_kind"], row["subject_id"])
        )
        if destination is None or not await self.access.destination_authorized(actor, destination):
            return NotificationTarget(available=False)
        return NotificationTarget(available=True, destination=destination)

    # ------------------------------------------------------------------ read state

    async def mark_read(self, actor: NotificationActor, notification_id: str | None) -> UndoRef:
        """Mark one (or, with ``None``, every unread) notification read. Changes read state
        only (NOTE-06) and is undoable for eight seconds."""
        user_id = actor.user_id
        if user_id is None:
            raise NotAvailable()
        n = notifications.c
        now = self._clock.now()
        async with self._uow() as tx:
            if notification_id is not None:
                row = await self._own_row(tx, actor, notification_id)
                if row is None:
                    raise NotAvailable()
            conditions = [
                n.firm_id == self._firm_id,
                n.recipient_user_id == user_id,
                n.read_at.is_(None),
            ]
            if notification_id is not None:
                conditions.append(n.id == notification_id)
            changed = (
                (
                    await tx.session.execute(
                        update(notifications).where(*conditions).values(read_at=now).returning(n.id)
                    )
                )
                .scalars()
                .all()
            )
            return await self._undo.issue(
                tx,
                actor_id=user_id,
                command_kind=UNDO_KIND,
                compensating_payload={
                    "ids": list(changed),
                    "readAt": now.isoformat(),
                    "notificationId": notification_id,
                },
            )

    async def restore_unread(
        self, actor: NotificationActor, ids: Sequence[str], read_at: datetime
    ) -> None:
        """Undo compensation: only rows still carrying the read mark we set are restored."""
        n = notifications.c
        if actor.user_id is None or not ids:
            return
        async with self._uow() as tx:
            await tx.session.execute(
                update(notifications)
                .where(
                    n.id.in_(list(ids)),
                    n.recipient_user_id == actor.user_id,
                    n.read_at == read_at,
                )
                .values(read_at=None)
            )

    async def get(self, actor: NotificationActor, notification_id: str) -> NotificationView:
        async with self._uow() as tx:
            row = await self._own_row(tx, actor, notification_id)
        if row is None:
            raise NotAvailable()
        return await self._view(actor, row)

    async def unread_count(self, actor: NotificationActor) -> int:
        n = notifications.c
        async with self._uow() as tx:
            count = (
                await tx.session.execute(
                    select(func.count()).where(
                        n.firm_id == self._firm_id,
                        n.recipient_user_id == actor.user_id,
                        n.read_at.is_(None),
                    )
                )
            ).scalar_one()
        return int(count)

    # ------------------------------------------------------------------ email delivery

    async def deliver_email(self, notification_id: str) -> DeliveryState:
        """Send one notification's email (the ``notifications.email_deliver`` job).

        Completion check: a delivery already ``Sent`` or ``Skipped`` is not sent again, so
        the job is safe to re-run. Raises ``UpstreamError`` after recording the attempt when
        the provider fails (the job retries).
        """
        n, d = notifications.c, notification_deliveries.c
        async with self._uow() as tx:
            row = (
                (
                    await tx.session.execute(
                        select(notifications, d.state.label("delivery_state"))
                        .join(notification_deliveries, d.notification_id == n.id)
                        .where(n.id == notification_id, d.channel == Channel.EMAIL.value)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return DeliveryState.SKIPPED
        state = DeliveryState(row["delivery_state"])
        if state in (DeliveryState.SENT, DeliveryState.SKIPPED):
            return state
        recipient = await self._recipients.recipient(row["recipient_user_id"])
        if recipient is None or not recipient.active:
            await self._record_delivery(notification_id, DeliveryState.SKIPPED)
            return DeliveryState.SKIPPED
        template = TEMPLATES[row["template_key"]]
        title, body = template.render(row["safe_context"])
        try:
            await self._email.send(
                EmailMessage(to=recipient.email, subject=title, text=body + _EMAIL_FOOTER)
            )
        except UpstreamError:
            await self._record_delivery(
                notification_id, DeliveryState.PENDING, error_code="EmailUnavailable"
            )
            raise
        await self._record_delivery(notification_id, DeliveryState.SENT)
        return DeliveryState.SENT

    async def _record_delivery(
        self, notification_id: str, state: DeliveryState, *, error_code: str | None = None
    ) -> None:
        d = notification_deliveries.c
        now = self._clock.now()
        async with self._uow() as tx:
            await tx.session.execute(
                update(notification_deliveries)
                .where(d.notification_id == notification_id, d.channel == Channel.EMAIL.value)
                .values(
                    state=state.value,
                    attempts=d.attempts + 1,
                    last_error_code=error_code,
                    sent_at=now if state is DeliveryState.SENT else None,
                )
            )
            await tx.session.execute(
                update(notifications)
                .where(notifications.c.id == notification_id)
                .values(email_state=state.value)
            )

    # ------------------------------------------------------------------ internals

    async def _own_row(self, tx: Tx, actor: NotificationActor, notification_id: str) -> Any:
        if actor.user_id is None:
            return None
        n = notifications.c
        stmt = select(notifications).where(
            n.id == notification_id,
            n.firm_id == self._firm_id,
            n.recipient_user_id == actor.user_id,
        )
        return (await tx.session.execute(stmt)).mappings().one_or_none()

    async def _view(self, actor: NotificationActor, row: Any) -> NotificationView:
        template: NotificationTemplate = TEMPLATES[row["template_key"]]
        title, body = template.render(row["safe_context"])
        project = None
        if row["project_id"] is not None:
            project = await self.access.project_label(actor, row["project_id"])
        return NotificationView(
            notification_id=row["id"],
            trigger=NotificationTrigger(row["trigger"]),
            project=project,
            title=title,
            body=body,
            created_at=row["created_at"],
            read_at=row["read_at"],
        )


class _ReadStateCompensator:
    """``notification.read`` undo: restore unread state for the recipient only."""

    def __init__(self, notifications: Notifications) -> None:
        self._notifications = notifications

    async def compensate(self, actor: Any, payload: Mapping[str, Any]) -> BaseModel:
        await self._notifications.restore_unread(
            actor, list(payload["ids"]), datetime.fromisoformat(payload["readAt"])
        )
        return await self.current(actor, payload)

    async def current(self, actor: Any, payload: Mapping[str, Any]) -> BaseModel:
        single = payload.get("notificationId")
        if single is not None:
            return await self._notifications.get(actor, single)
        return NotificationReadState(unread_count=await self._notifications.unread_count(actor))
