"""Notifications, undo, and email delivery (T04, TSK-12, NOTE-01..07, NFR-05)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import select

from app.container import Container
from app.modules.actions import (
    Channel,
    DeliveryState,
    NotificationTrigger,
    SafeSubjectRef,
)
from app.modules.actions.tables import notification_deliveries, notifications
from app.modules.governance import JobState, Priority, Worker
from app.modules.governance.tables import jobs, undo_tokens
from app.modules.organization_access import Actor
from app.platform.clock import FrozenClock
from app.platform.email import OutboxEmailSender
from app.platform.errors import CapabilityDenied, NotAvailable
from tests.auth import TestTokenIssuer
from tests.conftest import identity_of
from tests.fixtures.synthetic.personas import AVERY, JORDAN, MORGAN, Persona
from tests.handlers import KIND_NOOP, NoopPayload

PROJECT = "prj_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"
TASK = SafeSubjectRef("Task", "tsk_01J9Z3Q8M8V3W7G2E1N4R5T6Y7")
Activate = Callable[[Persona], Awaitable[Actor]]
Notify = Callable[..., Awaitable[str]]


@pytest.fixture
async def avery(activate: Activate) -> Actor:
    return await activate(AVERY)


@pytest.fixture
async def morgan(activate: Activate) -> Actor:
    return await activate(MORGAN)


@pytest.fixture
async def jordan(container: Container, firm: str) -> Actor:
    async with container.uow() as tx:
        return await container.organization.activate_client_collaborator(tx, identity_of(JORDAN))


@pytest.fixture
def notify(container: Container) -> Notify:
    async def _notify(
        recipient: Actor,
        *,
        template_key: str = "task.assigned",
        trigger: NotificationTrigger = NotificationTrigger.TASK_ASSIGNED,
        context: Mapping[str, str | int] | None = None,
        subject: SafeSubjectRef = TASK,
        project_id: str | None = PROJECT,
        channels: frozenset[Channel] = frozenset({Channel.IN_PRODUCT}),
    ) -> str:
        async with container.uow() as tx:
            return await container.notifications.enqueue(
                tx,
                recipient_user_id=recipient.user_id or "",
                trigger=trigger,
                template_key=template_key,
                subject_ref=subject,
                safe_context=context or {},
                project_id=project_id,
                channels=channels,
            )

    return _notify


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as http:
        yield http


@pytest.fixture
def outbox(container: Container) -> OutboxEmailSender:
    assert isinstance(container.email, OutboxEmailSender)
    return container.email


# --------------------------------------------------------------------------- in-product


async def test_list_read_state_and_undo(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, avery: Actor, morgan: Actor,
    notify: Notify, frozen_clock: FrozenClock,
) -> None:  # fmt: skip
    first = await notify(avery)
    frozen_clock.advance(timedelta(minutes=1))
    second = await notify(
        avery, template_key="task.due_soon", trigger=NotificationTrigger.TASK_DUE_SOON,
        context={"dueDate": "2026-01-09"},
    )  # fmt: skip
    await notify(morgan)
    headers = token_issuer.headers_for(AVERY)

    listed = (await client.get("/api/v1/notifications", headers=headers)).json()
    assert listed["unreadCount"] == 2
    assert [n["notificationId"] for n in listed["items"]] == [second, first]
    due = listed["items"][0]
    assert set(due) == {"notificationId", "trigger", "title", "body", "createdAt", "readAt"}
    assert (due["trigger"], due["title"], due["body"]) == (
        "TaskDueSoon", "A task is due soon", "It is due on 2026-01-09."
    )  # fmt: skip

    r = await client.post(f"/api/v1/notifications/{first}/read", headers=headers)
    assert r.status_code == 200
    undo = r.json()["undo"]
    assert set(undo) == {"token", "expiresAt"}
    unread = (await client.get("/api/v1/notifications?unreadOnly=true", headers=headers)).json()
    assert [n["notificationId"] for n in unread["items"]] == [second]
    assert unread["unreadCount"] == 1
    # Read state is per recipient: Morgan's notification is untouched.
    assert (await client.get("/api/v1/notifications", headers=token_issuer.headers_for(MORGAN))
            ).json()["unreadCount"] == 1  # fmt: skip

    restored = await client.post("/api/v1/undo", json={"token": undo["token"]}, headers=headers)
    assert restored.status_code == 200
    assert restored.json()["result"]["notificationId"] == first
    assert restored.json()["result"]["readAt"] is None
    # Undo is idempotent: a second call changes nothing and returns the current resource.
    again = await client.post("/api/v1/undo", json={"token": undo["token"]}, headers=headers)
    assert again.status_code == 200
    assert again.json()["result"]["readAt"] is None
    assert (await client.get("/api/v1/notifications", headers=headers)).json()["unreadCount"] == 2


async def test_read_all_and_its_undo(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, avery: Actor, notify: Notify
) -> None:
    for _ in range(3):
        await notify(avery)
    headers = token_issuer.headers_for(AVERY)
    r = await client.post("/api/v1/notifications/read-all", headers=headers)
    assert r.status_code == 200
    assert (await client.get("/api/v1/notifications", headers=headers)).json()["unreadCount"] == 0
    restored = await client.post(
        "/api/v1/undo", json={"token": r.json()["undo"]["token"]}, headers=headers
    )
    assert restored.json() == {"result": {"unreadCount": 3}}


async def test_undo_window_and_ownership(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, avery: Actor, morgan: Actor,
    notify: Notify, frozen_clock: FrozenClock,
) -> None:  # fmt: skip
    notification = await notify(avery)
    headers = token_issuer.headers_for(AVERY)
    token = (
        await client.post(f"/api/v1/notifications/{notification}/read", headers=headers)
    ).json()["undo"]["token"]

    other = await client.post(
        "/api/v1/undo", json={"token": token}, headers=token_issuer.headers_for(MORGAN)
    )
    assert other.status_code == 404  # another actor's token is not theirs to know about

    frozen_clock.advance(timedelta(seconds=8))
    headers = token_issuer.headers_for(AVERY)
    expired = await client.post("/api/v1/undo", json={"token": token}, headers=headers)
    assert expired.status_code == 409
    error = expired.json()["error"]
    assert error["code"] == "UNDO_EXPIRED"
    assert error["current"]["notificationId"] == notification
    assert error["current"]["readAt"] is not None  # still read: nothing was undone


class _Renamed(BaseModel):
    title: str


async def test_compensation_rechecks_authorization(container: Container, avery: Actor) -> None:
    """Undo never bypasses authorization: a compensator re-checks it against now."""
    allowed: set[str | None] = {avery.user_id}

    class RenameBack:
        async def compensate(self, actor: Any, payload: Mapping[str, Any]) -> BaseModel:
            if actor.user_id not in allowed:
                raise CapabilityDenied()
            return _Renamed(title=payload["previous"])

        async def current(self, actor: Any, payload: Mapping[str, Any]) -> BaseModel:
            return _Renamed(title="current")

    container.undo.register("probe.rename", RenameBack())

    async def issue() -> str:
        async with container.uow() as tx:
            ref = await container.undo.issue(
                tx, actor_id=avery.user_id or "", command_kind="probe.rename",
                compensating_payload={"previous": "Old title"},
            )  # fmt: skip
        return ref.token

    allowed_token = await issue()
    assert await container.undo.undo(avery, allowed_token) == _Renamed(title="Old title")

    revoked_token = await issue()
    allowed.clear()  # the actor's role changed after the command
    with pytest.raises(CapabilityDenied):
        await container.undo.undo(avery, revoked_token)
    # The refused compensation did not consume the token either.
    async with container.uow() as tx:
        consumed = (
            await tx.session.execute(
                select(undo_tokens.c.consumed_at).where(undo_tokens.c.token == revoked_token)
            )
        ).scalar_one()
    assert consumed is None
    with pytest.raises(NotAvailable):
        await container.undo.undo(avery, "und_01J9Z3Q8M8V3W7G2E1N4R5T6Y7")


# --------------------------------------------------------------------------- target


async def test_target_is_non_enumerating(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, avery: Actor, morgan: Actor,
    notify: Notify,
) -> None:  # fmt: skip
    in_project = await notify(avery)
    decision = await notify(
        avery, template_key="erasure.decision", trigger=NotificationTrigger.ERASURE_DECISION,
        context={"decision": "Approved"}, subject=SafeSubjectRef("ErasureRequest", "era_1"),
        project_id=None,
    )  # fmt: skip
    headers = token_issuer.headers_for(AVERY)

    r = await client.get(f"/api/v1/notifications/{decision}/target", headers=headers)
    assert r.json() == {
        "available": True,
        "destination": {"kind": "ErasureRequest", "path": "/account/erasure"},
    }
    # Until T05 grants Project access, a Project destination fails closed — and the answer
    # is the same as for someone else's notification or one that never existed.
    unavailable = [
        await client.get(f"/api/v1/notifications/{in_project}/target", headers=headers),
        await client.get(
            f"/api/v1/notifications/{in_project}/target", headers=token_issuer.headers_for(MORGAN)
        ),
        await client.get(
            "/api/v1/notifications/ntf_01J9Z3Q8M8V3W7G2E1N4R5T6Y7/target", headers=headers
        ),
    ]
    for response in unavailable:
        assert response.status_code == 200
        assert response.json() == {"available": False}


# --------------------------------------------------------------------------- enqueue rules


async def test_client_collaborators_receive_only_client_visible_templates(
    notify: Notify, jordan: Actor
) -> None:
    assert await notify(jordan)  # task.assigned is Client Visible
    with pytest.raises(ValueError, match="internal"):
        await notify(
            jordan, template_key="review.reminder", trigger=NotificationTrigger.REVIEW_REMINDER,
            context={"pendingCount": 2}, subject=SafeSubjectRef("Review", "rvw_1"),
        )  # fmt: skip


async def test_safe_context_rejects_personal_data_and_unknown_keys(
    notify: Notify, avery: Actor
) -> None:
    due = {"template_key": "task.due_soon", "trigger": NotificationTrigger.TASK_DUE_SOON}
    with pytest.raises(ValueError, match="dueDate"):
        await notify(avery, **due, context={"dueDate": "secret-name@example.com"})
    with pytest.raises(ValueError, match="not allowed"):
        await notify(avery, **due, context={"dueDate": "2026-01-09", "taskTitle": "Riley"})
    with pytest.raises(ValueError, match="required"):
        await notify(avery, **due, context={})
    with pytest.raises(ValueError, match="unknown"):
        await notify(avery, trigger=NotificationTrigger.MENTION)  # trigger/template mismatch
    with pytest.raises(ValueError, match="subject"):
        SafeSubjectRef("Task", "Riley Park")


async def test_repeat_triggers_are_deduplicated_per_day(
    container: Container, notify: Notify, avery: Actor, frozen_clock: FrozenClock
) -> None:
    overdue = {"template_key": "task.overdue", "trigger": NotificationTrigger.TASK_OVERDUE}
    first = await notify(avery, **overdue, context={"dueDate": "2026-01-04"})
    again = await notify(avery, **overdue, context={"dueDate": "2026-01-03"})
    assert again == first
    frozen_clock.advance(timedelta(days=1))
    tomorrow = await notify(avery, **overdue, context={"dueDate": "2026-01-03"})
    assert tomorrow != first
    page, unread = await container.notifications.list(avery, unread_only=False, cursor=None)
    assert unread == 2
    assert {n.body for n in page.items} == {"It was due on 2026-01-03."}


async def test_unknown_or_inactive_recipient_is_refused(notify: Notify, avery: Actor) -> None:
    ghost = replace(avery, user_id="usr_01J9Z3Q8M8V3W7G2E1N4R5T6Y7")
    with pytest.raises(ValueError, match="recipient"):
        await notify(ghost)


# --------------------------------------------------------------------------- email


async def test_email_is_delivered_by_a_high_priority_job(
    container: Container, notify: Notify, avery: Actor, outbox: OutboxEmailSender,
    make_worker: Callable[..., Worker],
) -> None:  # fmt: skip
    notification = await notify(avery, channels=frozenset({Channel.IN_PRODUCT, Channel.EMAIL}))
    async with container.uow() as tx:
        job = (
            (
                await tx.session.execute(
                    select(jobs).where(jobs.c.kind == "notifications.email_deliver")
                )
            )
            .mappings()
            .one()
        )
    assert job["priority"] == Priority.HIGH
    assert job["payload"] == {"notification_id": notification}  # opaque id only

    await make_worker().run_until_idle()
    assert len(outbox.sent) == 1
    message = outbox.sent[0]
    assert message.to == AVERY.email
    assert message.subject == "A task was assigned to you"
    async with container.uow() as tx:
        states = dict(
            (
                await tx.session.execute(
                    select(
                        notification_deliveries.c.channel, notification_deliveries.c.state
                    ).where(notification_deliveries.c.notification_id == notification)
                )
            ).all()
        )
    assert states == {"InProduct": "Delivered", "Email": "Sent"}
    # The completion check makes the job safe to run twice.
    assert await container.notifications.deliver_email(notification) is DeliveryState.SENT
    assert len(outbox.sent) == 1


async def test_email_outage_is_retried(
    container: Container, notify: Notify, avery: Actor, outbox: OutboxEmailSender,
    make_worker: Callable[..., Worker], frozen_clock: FrozenClock,
) -> None:  # fmt: skip
    outbox.fail_next = 1
    await notify(avery, channels=frozenset({Channel.EMAIL}))
    worker = make_worker()
    await worker.run_once()
    async with container.uow() as tx:
        state = (
            await tx.session.execute(
                select(jobs.c.state).where(jobs.c.kind == "notifications.email_deliver")
            )
        ).scalar_one()
    assert state == JobState.RETRYING.value
    assert outbox.sent == []
    frozen_clock.advance(timedelta(minutes=1))
    await worker.run_until_idle()
    assert len(outbox.sent) == 1


async def test_email_meets_the_two_minute_target_under_a_background_backlog(
    container: Container, notify: Notify, avery: Actor, outbox: OutboxEmailSender,
    make_worker: Callable[..., Worker], enqueue: Callable[..., Awaitable[str]],
) -> None:  # fmt: skip
    """NFR-05 with a fake load: HIGH priority email delivery overtakes a LOW backlog.

    One worker slot (the test's rolled-back connection cannot be shared by concurrent jobs);
    T33 repeats this against the accepted production load."""
    for i in range(150):
        await enqueue(KIND_NOOP, NoopPayload(effect_id=f"bg-{i}"), key=f"bg-{i}")
    started = time.monotonic()
    for _ in range(20):
        await notify(avery, channels=frozenset({Channel.IN_PRODUCT, Channel.EMAIL}))
    worker = make_worker()
    rounds = 0
    while len(outbox.sent) < 20 and rounds < 200:
        await worker.run_once()
        rounds += 1
    elapsed = time.monotonic() - started
    assert len(outbox.sent) == 20
    assert elapsed < 120
    assert rounds == 20  # every claim went to an email before any backlog job
    async with container.uow() as tx:
        pending_backlog = (
            await tx.session.execute(
                select(jobs.c.id).where(jobs.c.kind == KIND_NOOP, jobs.c.state == "Queued")
            )
        ).all()
    assert len(pending_backlog) == 150


async def test_rows_carry_no_content(container: Container, notify: Notify, avery: Actor) -> None:
    await notify(
        avery, template_key="task.changed", trigger=NotificationTrigger.TASK_CHANGED,
        context={"changedField": "Status"},
    )  # fmt: skip
    async with container.uow() as tx:
        row = (await tx.session.execute(select(notifications))).mappings().one()
    assert row["safe_context"] == {"changedField": "Status"}
    assert row["subject_kind"] == "Task"
    stored = str(dict(row))
    assert AVERY.email not in stored
    assert AVERY.display_name not in stored


async def test_notification_routes_need_an_activated_actor(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, firm: str
) -> None:
    for method, path in (
        ("GET", "/api/v1/notifications"),
        ("POST", "/api/v1/notifications/read-all"),
        ("POST", "/api/v1/undo"),
    ):
        r = await client.request(method, path, headers=token_issuer.headers_for(AVERY), json={})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ACCOUNT_NOT_ACTIVATED"


async def test_mark_read_of_someone_elses_notification_is_unavailable(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, avery: Actor, morgan: Actor,
    notify: Notify,
) -> None:  # fmt: skip
    notification = await notify(avery)
    r = await client.post(
        f"/api/v1/notifications/{notification}/read", headers=token_issuer.headers_for(MORGAN)
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_AVAILABLE"


async def test_a_tampered_cursor_is_malformed(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, avery: Actor
) -> None:
    r = await client.get(
        "/api/v1/notifications",
        params={"cursor": "e30.AAAA"},
        headers=token_issuer.headers_for(AVERY),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "MALFORMED_REQUEST"


def test_convention_rows_expire_through_a_production_sweep() -> None:
    from app.modules.governance.jobs import CONVENTIONS_EXPIRY
    from app.modules.governance.registry import DEFAULT_REGISTRY
    from tests.handlers import registry as test_registry

    spec = DEFAULT_REGISTRY[CONVENTIONS_EXPIRY]
    assert spec.schedule is not None
    assert test_registry.get(CONVENTIONS_EXPIRY) is None  # frozen-clock tests stay sweep-free
