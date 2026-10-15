"""User Operations: job-driven state, progress, Activity groups, badge, dismissal, audience,
destination reauthorization, content-free types, retry (T04)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.container import Container
from app.modules.governance import (
    ActivityGroup,
    Audience,
    Destination,
    JobState,
    OperationState,
    OperationType,
    OperationView,
    ProgressUnit,
    ProjectLabel,
    SafeSubject,
    Worker,
)
from app.modules.governance.operations import PRESENTATION_STATE
from app.modules.organization_access import Actor
from app.platform.clock import FrozenClock
from app.platform.errors import AppError, ErrorCode, NotAvailable
from tests.auth import TestTokenIssuer
from tests.fixtures.synthetic.personas import AVERY, MORGAN, Persona
from tests.handlers import (
    KIND_FLAKY,
    KIND_OK,
    KIND_TERMINAL,
    KIND_WAITING,
    FlakyPayload,
    OkPayload,
    TerminalPayload,
    WaitingPayload,
)

PROJECT = "prj_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"
SECRET_PROJECT_NAME = "Atlas Expansion"
Activate = Callable[[Persona], Awaitable[Actor]]


class FakeProjectAccess:
    """Stands in for T05's ProjectAccess adapter."""

    def __init__(self) -> None:
        self.holders: set[tuple[str | None, str, str | None]] = set()
        self.destinations_authorized = True

    async def holds(self, actor: Any, capability: str, project_id: str | None) -> bool:
        return (actor.user_id, capability, project_id) in self.holders

    async def destination_authorized(self, actor: Any, destination: Destination) -> bool:
        return self.destinations_authorized

    async def project_label(self, actor: Any, project_id: str) -> ProjectLabel | None:
        return ProjectLabel(project_id=project_id, name=SECRET_PROJECT_NAME)


@pytest.fixture
def access(container: Container) -> FakeProjectAccess:
    fake = FakeProjectAccess()
    container.operations.access = fake
    return fake


@pytest.fixture
async def avery(activate: Activate) -> Actor:
    return await activate(AVERY)


@pytest.fixture
async def morgan(activate: Activate) -> Actor:
    return await activate(MORGAN)


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as http:
        yield http


StartOp = Callable[..., Awaitable[str]]


@pytest.fixture
def start_op(container: Container) -> StartOp:
    """``await start_op(actor, kind=..., payload=...)``: one command = operation + its job."""

    async def _start(
        actor: Actor | None,
        *,
        kind: str | None = None,
        payload: BaseModel | None = None,
        type: OperationType = OperationType.DOCUMENT_INGESTION,
        label: str = "Q3 board pack.pdf",
        project_id: str | None = PROJECT,
        audience: Audience | None = None,
    ) -> str:
        async with container.uow() as tx:
            ref = await container.operations.start(
                tx,
                type=type,
                subject=SafeSubject(label, "doc_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"),
                initiated_by=actor.actor_ref if actor else None,
                project_id=project_id,
                destination=Destination(
                    kind="Document", project_id=project_id, path=f"/projects/{project_id}/x"
                ),
                audience=audience or Audience.initiator(),
            )
            if kind is not None and payload is not None:
                await container.queue.enqueue(
                    tx, kind, payload, idempotency_key=f"op:{ref.operation_id}",
                    operation_id=ref.operation_id,
                )  # fmt: skip
        return ref.operation_id

    return _start


def test_job_states_map_to_the_presentation_vocabulary() -> None:
    assert {s.value: PRESENTATION_STATE[s].value for s in JobState} == {
        "Queued": "Queued",
        "Running": "Running",
        "Retrying": "Retrying",
        "WaitingForUser": "WaitingForUser",
        "Succeeded": "Succeeded",
        "Failed": "Failed",
        "Cancelled": "Cancelled",
    }


# --------------------------------------------------------------------------- lifecycle


async def test_the_attached_job_drives_state_and_trustworthy_progress(
    container: Container, start_op: StartOp, make_worker: Callable[..., Worker],
    avery: Actor, access: FakeProjectAccess, effects: Any,
) -> None:  # fmt: skip
    op_id = await start_op(avery, kind=KIND_OK, payload=OkPayload(steps=2))
    queued = await container.operations.get_for(avery, op_id)
    assert queued.state is OperationState.QUEUED
    assert queued.progress is None
    assert await make_worker().run_until_idle() == 1
    done = await container.operations.get_for(avery, op_id)
    assert done.state is OperationState.SUCCEEDED
    assert done.stage == "Working"
    assert done.progress is not None
    assert (done.progress.done, done.progress.total, done.progress.unit) == (2, 2, "Records")
    assert done.completed_at is not None
    assert "Open" in done.available_actions


async def test_progress_is_omitted_unless_both_counts_are_reported(
    container: Container, start_op: StartOp, avery: Actor, access: FakeProjectAccess
) -> None:
    op_id = await start_op(avery)
    async with container.uow() as tx:
        await container.operations.report(tx, op_id, stage="ExtractingStructure")
    view = await container.operations.get_for(avery, op_id)
    assert view.state is OperationState.RUNNING
    assert view.stage == "ExtractingStructure"
    assert view.progress is None
    assert view.model_dump(by_alias=True)["progress"] is None  # present, as null
    with pytest.raises(ValueError, match="together"):
        async with container.uow() as tx:
            await container.operations.report(tx, op_id, stage="X", done=3)
    async with container.uow() as tx:
        await container.operations.report(
            tx, op_id, stage="ExtractingStructure", done=3, total=9, unit=ProgressUnit.PAGES
        )
    view = await container.operations.get_for(avery, op_id)
    assert view.progress is not None
    assert (view.progress.done, view.progress.total, view.progress.unit) == (3, 9, "Pages")


async def test_retries_failure_and_operator_retry(
    container: Container, start_op: StartOp, make_worker: Callable[..., Worker],
    avery: Actor, access: FakeProjectAccess, frozen_clock: FrozenClock,
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
) -> None:  # fmt: skip
    op_id = await start_op(avery, kind=KIND_FLAKY, payload=FlakyPayload(fail_times=3))
    worker = make_worker()
    await worker.run_once()
    retrying = await container.operations.get_for(avery, op_id)
    assert retrying.state is OperationState.RETRYING
    assert retrying.next_retry_at is not None
    for _ in range(2):
        frozen_clock.advance(timedelta(minutes=10))
        await worker.run_once()
    failed = await container.operations.get_for(avery, op_id)
    assert failed.state is OperationState.FAILED
    assert failed.failure is not None
    assert failed.failure.code == "FlakyUpstream"
    assert failed.available_actions == ["Retry"]  # unresolved: not dismissible

    headers = {**token_issuer.headers_for(AVERY), "Idempotency-Key": str(uuid.uuid4())}
    r = await client.post(f"/api/v1/operations/{op_id}/retry", headers=headers)
    assert r.status_code == 202
    assert r.json() == {
        "operation": {
            "operationId": op_id,
            "state": "Queued",
            "statusUrl": f"/api/v1/operations/{op_id}",
        }
    }
    replay = await client.post(f"/api/v1/operations/{op_id}/retry", headers=headers)
    assert replay.status_code == 202
    assert replay.headers["Idempotency-Replayed"] == "true"
    # A new job runs under the same operation, and the operation follows it again.
    assert await worker.run_once() == 1
    again = await container.operations.get_for(avery, op_id)
    assert again.state is OperationState.RETRYING
    assert again.failure is None


async def test_terminal_failure_is_not_retryable_but_dismissible(
    container: Container, start_op: StartOp, make_worker: Callable[..., Worker],
    avery: Actor, access: FakeProjectAccess,
) -> None:  # fmt: skip
    op_id = await start_op(avery, kind=KIND_TERMINAL, payload=TerminalPayload(code="Corrupt"))
    await make_worker().run_until_idle()
    view = await container.operations.get_for(avery, op_id)
    assert view.state is OperationState.FAILED
    assert view.available_actions == ["Dismiss"]
    with pytest.raises(AppError) as refused:
        await container.operations.retry(avery, op_id, str(uuid.uuid4()))
    assert refused.value.code is ErrorCode.INVALID_TRANSITION


async def test_waiting_for_user_and_resume(
    container: Container, start_op: StartOp, make_worker: Callable[..., Worker],
    avery: Actor, access: FakeProjectAccess,
) -> None:  # fmt: skip
    op_id = await start_op(avery, kind=KIND_WAITING, payload=WaitingPayload())
    await make_worker().run_once()
    view = await container.operations.get_for(avery, op_id)
    assert view.state is OperationState.WAITING_FOR_USER
    assert view.waiting_for is not None
    assert (view.waiting_for.action, view.waiting_for.label) == ("Review", "Review required")
    assert view.available_actions == ["Review"]
    assert view.destination is not None
    assert view.destination.path == "/projects/prj_x/privacy/reviews"  # set by the job
    job_id = await container.queue.stats()  # noqa: F841 - stats must not fail with waits
    from sqlalchemy import select

    from app.modules.governance.tables import jobs

    async with container.uow() as tx:
        job = (
            await tx.session.execute(select(jobs.c.id).where(jobs.c.operation_id == op_id))
        ).scalar_one()
        await container.queue.resume(tx, job)
    assert (await container.operations.get_for(avery, op_id)).state is OperationState.QUEUED


async def test_owning_module_finish_wins_over_later_job_events(
    container: Container, start_op: StartOp, avery: Actor, access: FakeProjectAccess
) -> None:
    op_id = await start_op(avery)
    async with container.uow() as tx:
        await container.operations.finish(tx, op_id, outcome="Cancelled")
        await container.operations.report(tx, op_id, stage="Late", done=1, total=2,
                                          unit=ProgressUnit.RECORDS)  # fmt: skip
    view = await container.operations.get_for(avery, op_id)
    assert view.state is OperationState.CANCELLED
    assert view.stage == "Queued"


# --------------------------------------------------------------------------- groups and badge


async def _ids(container: Container, actor: Actor, group: ActivityGroup) -> list[str]:
    page = await container.operations.list_for(actor, group, None)
    return [item.operation_id for item in page.items]


async def test_groups_ordering_badge_and_recent_window(
    container: Container, start_op: StartOp, avery: Actor, access: FakeProjectAccess,
    frozen_clock: FrozenClock, client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
) -> None:  # fmt: skip
    ops = container.operations
    ids: dict[str, str] = {}
    for name in ("old_done", "running_a", "running_b", "waiting", "failed_retry",
                 "failed_terminal", "done_a", "done_b", "queued"):  # fmt: skip
        frozen_clock.advance(timedelta(minutes=1))
        ids[name] = await start_op(avery)
    async with container.uow() as tx:
        await ops.finish(tx, ids["old_done"], outcome="Succeeded")
    frozen_clock.advance(timedelta(hours=25))  # old_done leaves the 24 h window
    async with container.uow() as tx:
        await ops.report(tx, ids["running_a"], stage="Working")
        await ops.report(tx, ids["running_b"], stage="Working")
        await ops.wait_for_user(
            tx, ids["waiting"], action_code="Review",
            destination=Destination(kind="Review", project_id=PROJECT, path="/projects/p/r"),
        )  # fmt: skip
        await ops.finish(tx, ids["failed_terminal"], outcome="Failed", failure_code="Corrupt")
    frozen_clock.advance(timedelta(minutes=1))
    async with container.uow() as tx:
        await ops.finish(tx, ids["failed_retry"], outcome="Failed", failure_code="Timeout")
        await ops.finish(tx, ids["done_a"], outcome="Succeeded")
    frozen_clock.advance(timedelta(minutes=1))
    async with container.uow() as tx:
        await ops.finish(tx, ids["done_b"], outcome="Cancelled")

    assert await _ids(container, avery, ActivityGroup.RUNNING) == [
        ids["queued"],
        ids["running_b"],
        ids["running_a"],
    ]  # start time descending
    assert await _ids(container, avery, ActivityGroup.NEEDS_ATTENTION) == [
        ids["failed_retry"],
        ids["waiting"],
        ids["failed_terminal"],
    ]  # urgency, then most recent
    assert await _ids(container, avery, ActivityGroup.RECENTLY_COMPLETED) == [
        ids["done_b"],
        ids["done_a"],
    ]  # completion descending; old_done is outside the window

    summary = await ops.summary(avery)
    # Running 3 + waiting 1 + unresolved failure 1; the terminal failure needs attention
    # (it can be dismissed) but is not counted in the badge.
    assert (summary.badge_count, summary.running, summary.needs_attention) == (5, 3, 3)

    headers = token_issuer.headers_for(AVERY)
    r = await client.get("/api/v1/operations/summary", headers=headers)
    assert r.json() == {"badgeCount": 5, "running": 3, "needsAttention": 3}

    # Pagination keeps the order across pages.
    first = await client.get(
        "/api/v1/operations", params={"group": "NeedsAttention", "limit": 2}, headers=headers
    )
    body = first.json()
    assert [i["operationId"] for i in body["items"]] == [ids["failed_retry"], ids["waiting"]]
    second = await client.get(
        "/api/v1/operations",
        params={"group": "NeedsAttention", "limit": 2, "cursor": body["nextCursor"]},
        headers=headers,
    )
    assert [i["operationId"] for i in second.json()["items"]] == [ids["failed_terminal"]]
    assert second.json()["nextCursor"] is None

    # Dismissal: refused for active and unresolved rows, per user, survives a reload.
    for active in ("running_a", "waiting", "failed_retry"):
        r = await client.post(f"/api/v1/operations/{ids[active]}/dismiss", headers=headers)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "OPERATION_NOT_DISMISSIBLE"
    for done in ("done_a", "failed_terminal"):
        r = await client.post(f"/api/v1/operations/{ids[done]}/dismiss", headers=headers)
        assert r.status_code == 204
    reloaded = await client.get(
        "/api/v1/operations", params={"group": "RecentlyCompleted"}, headers=headers
    )
    assert [i["operationId"] for i in reloaded.json()["items"]] == [ids["done_b"]]
    summary = await ops.summary(avery)
    assert (summary.badge_count, summary.needs_attention) == (5, 2)
    # Dismissal touched only the dismissal table: the operation itself is unchanged.
    still = await ops.get_for(avery, ids["done_a"])
    assert still.state is OperationState.SUCCEEDED


# --------------------------------------------------------------------------- authorization


async def test_audience_is_reevaluated_on_every_read(
    container: Container, start_op: StartOp, avery: Actor, morgan: Actor,
    access: FakeProjectAccess, client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
) -> None:  # fmt: skip
    private = await start_op(avery)
    shared = await start_op(
        avery, audience=Audience.holders_of("ingestion.view", PROJECT), label="Shared.pdf"
    )
    morgan_headers = token_issuer.headers_for(MORGAN)
    # A non-holder sees neither, and the 404 does not say which exists.
    for op_id in (private, shared):
        r = await client.get(f"/api/v1/operations/{op_id}", headers=morgan_headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_AVAILABLE"
    assert await _ids(container, morgan, ActivityGroup.RUNNING) == []

    access.holders.add((morgan.user_id, "ingestion.view", PROJECT))
    assert await _ids(container, morgan, ActivityGroup.RUNNING) == [shared]
    access.holders.clear()  # capability lost: gone on the next read
    assert await _ids(container, morgan, ActivityGroup.RUNNING) == []
    with pytest.raises(NotAvailable):
        await container.operations.dismiss(morgan, shared)


async def test_destination_is_dropped_when_authorization_is_lost(
    container: Container, start_op: StartOp, avery: Actor, access: FakeProjectAccess
) -> None:
    op_id = await start_op(avery, label="Northstar plan.docx")
    async with container.uow() as tx:
        await container.operations.report(tx, op_id, stage="Indexing", done=1, total=4,
                                          unit=ProgressUnit.RECORDS)  # fmt: skip
    authorized = (await container.operations.get_for(avery, op_id)).model_dump(by_alias=True)
    assert authorized["destination"]["path"] == f"/projects/{PROJECT}/x"
    assert authorized["project"] == {"projectId": PROJECT, "name": SECRET_PROJECT_NAME}
    assert authorized["subject"] == {
        "label": "Northstar plan.docx",
        "recordId": "doc_01J9Z3Q8M8V3W7G2E1N4R5T6Y7",
    }

    access.destinations_authorized = False
    lost = (await container.operations.get_for(avery, op_id)).model_dump(by_alias=True)
    assert lost["state"] == "Running"  # state and stage are retained
    assert lost["stage"] == "Indexing"
    assert lost["progress"] == {"done": 1, "total": 4, "unit": "Records"}
    assert lost["destination"] is None
    assert "project" not in lost
    assert lost["subject"] == {"label": "Document ingestion"}  # generic type label only


@pytest.mark.parametrize(
    "op_type",
    [OperationType.PROJECT_DELETION, OperationType.ERASURE_EXECUTION,
     OperationType.VISIBILITY_PROPAGATION],
)  # fmt: skip
async def test_content_free_types_expose_no_content_fields(
    container: Container, start_op: StartOp, avery: Actor, access: FakeProjectAccess,
    op_type: OperationType, client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
) -> None:  # fmt: skip
    op_id = await start_op(avery, type=op_type, label="Erasure case E-12")
    r = await client.get(f"/api/v1/operations/{op_id}", headers=token_issuer.headers_for(AVERY))
    body = r.json()
    assert set(body) == {
        "operationId", "type", "subject", "state", "stage", "progress", "createdAt",
        "updatedAt", "availableActions", "destination",
    }  # fmt: skip
    assert body["subject"] == {"label": "Erasure case E-12"}
    assert SECRET_PROJECT_NAME not in r.text
    assert "doc_01J9Z3Q8M8V3W7G2E1N4R5T6Y7" not in r.text


def test_subject_labels_refuse_personal_data() -> None:
    for label in ("Notes for secret-name@example.com", "Call Riley about it", "x" * 201, ""):
        with pytest.raises(ValueError, match="subject label"):
            SafeSubject(label)
    with pytest.raises(ValueError, match="opaque"):
        SafeSubject("Report.pdf", "not-an-id")


async def test_operations_routes_need_an_activated_actor(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, firm: str
) -> None:
    for path in ("/api/v1/operations?group=Running", "/api/v1/operations/summary"):
        assert (await client.get(path)).status_code == 401
        r = await client.get(path, headers=token_issuer.headers_for(AVERY))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ACCOUNT_NOT_ACTIVATED"


async def test_list_rejects_a_cursor_from_another_group(
    container: Container, start_op: StartOp, avery: Actor, access: FakeProjectAccess
) -> None:
    for _ in range(3):
        await start_op(avery)
    page = await container.operations.list_for(avery, ActivityGroup.RUNNING, None, limit=1)
    assert page.next_cursor is not None
    with pytest.raises(AppError) as bad:
        await container.operations.list_for(
            avery, ActivityGroup.RECENTLY_COMPLETED, page.next_cursor
        )
    assert bad.value.code is ErrorCode.MALFORMED_REQUEST
    assert isinstance(page.items[0], OperationView)
