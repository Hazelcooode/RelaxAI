"""Project lifecycle (T07): transition matrix, archival candidacy and scheduling, archive
effects, deletion request and recovery, verified per-store deletion, reports, RA-14."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.container import Container
from app.modules.governance import (
    AuditAction,
    AuditFilters,
    OperationState,
    ProjectAdministratorViewer,
    RetryableError,
    TerminalError,
    Worker,
)
from app.modules.organization_access import Actor, OperatorActor
from app.modules.projects import (
    AccessPurpose,
    Capability,
    Lifecycle,
    LifecycleHook,
    ProjectLifecycle,
    ProjectPurger,
    ProjectRole,
    ProjectScope,
    ProjectView,
    PurgeReport,
    SuspendReason,
    VerifiedNoopPurger,
)
from app.modules.projects.tables import deletion_requests
from app.platform.clock import FrozenClock
from app.platform.db import Tx
from app.platform.errors import AppError, ErrorCode, GovernanceOnly, NotAvailable
from tests.auth import TestTokenIssuer
from tests.fixtures.synthetic.personas import AVERY, JORDAN, MORGAN, RILEY, Persona
from tests.projects_support import actor_for, invite, join, new_project, scope_of

Activate = Callable[[Persona], Awaitable[Actor]]
CANDIDATE_AFTER = timedelta(days=62)  # FROZEN_NOW is 5 Jan: 5 Mar + 1 day is past two months


@pytest.fixture
async def avery(activate: Activate) -> Actor:
    return await activate(AVERY)


@pytest.fixture
async def project(container: Container, avery: Actor) -> ProjectView:
    return await new_project(container, avery)


@pytest.fixture
async def manager(container: Container, activate: Activate) -> Actor:
    morgan = await activate(MORGAN)
    await container.organization.appoint_first_firm_manager(
        OperatorActor.cli(), morgan.user_id or ""
    )
    return await actor_for(container, MORGAN)


async def _current(container: Container, actor: Actor, project_id: str) -> ProjectView:
    scope = await scope_of(
        container, actor, project_id, Capability.PROJECT_READ, AccessPurpose.RECOVERY
    )
    return await container.projects.get(scope)


async def _lifecycle_scope(container: Container, actor: Actor, project_id: str) -> ProjectScope:
    return await scope_of(
        container, actor, project_id, Capability.LIFECYCLE_MANAGE, AccessPurpose.RECOVERY
    )


async def _request_deletion(container: Container, actor: Actor, project_id: str) -> None:
    current = await _current(container, actor, project_id)
    scope = await _lifecycle_scope(container, actor, project_id)
    await container.lifecycle.request_deletion(scope, current.name, current.version)


async def _request_id(container: Container, project_id: str) -> str:
    async with container.uow() as tx:
        stmt = (
            select(deletion_requests.c.id)
            .where(deletion_requests.c.project_id == project_id)
            .order_by(deletion_requests.c.requested_at.desc())
        )
        return str((await tx.session.execute(stmt)).scalars().first())


async def _notifications(container: Container, actor: Actor) -> list[str]:
    page, _ = await container.notifications.list(actor, unread_only=False, cursor=None)
    return [n.trigger.value for n in page.items]


def _lifecycle_with(
    container: Container,
    *,
    purgers: Sequence[ProjectPurger] | None = None,
    hooks: Sequence[LifecycleHook] = (),
) -> ProjectLifecycle:
    """The container's lifecycle service with test purgers or hooks."""
    return ProjectLifecycle(
        uow=container.uow,
        clock=container.clock,
        firm_id=container.settings.firm_id,
        access=container.project_access,
        audit=container.audit,
        operations=container.operations,
        notifications=container.notifications,
        queue=container.queue,
        directory=container.organization,
        purgers=purgers if purgers is not None else [VerifiedNoopPurger("only")],
        hooks=hooks,
        zone=ZoneInfo("UTC"),
    )


class RecordingHook:
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def suspend(self, tx: Tx, project_id: str, reason: SuspendReason) -> None:
        del tx, project_id
        self.calls.append(("suspend", reason.value))

    async def resume(self, tx: Tx, project_id: str, reason: SuspendReason) -> None:
        del tx, project_id
        self.calls.append(("resume", reason.value))


class CountingPurger:
    def __init__(self, store: str, *, failures: int = 0) -> None:
        self.store = store
        self.failures = failures
        self.purged = 0
        self.purged_ids: set[str] = set()

    async def purge(self, project_id: str) -> PurgeReport:
        self.purged += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("store unavailable: Atlas Expansion")  # never recorded
        self.purged_ids.add(project_id)
        return PurgeReport(store=self.store)

    async def verify(self, project_id: str) -> bool:
        return project_id in self.purged_ids


# --------------------------------------------------------------------------- transitions

COMMANDS = {
    # command -> (path suffix, HTTP method)
    "archive": ("/archive", "POST"),
    "restore": ("/restore", "POST"),
    "request_deletion": ("/deletion", "POST"),
    "cancel_deletion": ("/deletion", "DELETE"),
}
MATRIX = {
    ("archive", Lifecycle.ACTIVE): 200,
    ("archive", Lifecycle.ARCHIVED): "INVALID_TRANSITION",
    ("archive", Lifecycle.PENDING_DELETION): "NOT_AVAILABLE",
    ("restore", Lifecycle.ACTIVE): "INVALID_TRANSITION",
    ("restore", Lifecycle.ARCHIVED): 200,
    ("restore", Lifecycle.PENDING_DELETION): "NOT_AVAILABLE",
    ("request_deletion", Lifecycle.ACTIVE): 202,
    ("request_deletion", Lifecycle.ARCHIVED): "INVALID_TRANSITION",
    ("request_deletion", Lifecycle.PENDING_DELETION): "INVALID_TRANSITION",
    ("cancel_deletion", Lifecycle.ACTIVE): "INVALID_TRANSITION",
    ("cancel_deletion", Lifecycle.ARCHIVED): "INVALID_TRANSITION",
    ("cancel_deletion", Lifecycle.PENDING_DELETION): 200,
}


@pytest.mark.parametrize(("command", "state"), list(MATRIX))
async def test_transition_matrix(
    container: Container, api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
    avery: Actor, project: ProjectView, command: str, state: Lifecycle,
) -> None:  # fmt: skip
    if state is Lifecycle.ARCHIVED:
        await container.lifecycle.archive(
            await _lifecycle_scope(container, avery, project.project_id), project.version
        )
    elif state is Lifecycle.PENDING_DELETION:
        await _request_deletion(container, avery, project.project_id)
    version = (await _current(container, avery, project.project_id)).version
    suffix, method = COMMANDS[command]
    response = await api_client.request(
        method,
        f"/api/v1/projects/{project.project_id}{suffix}",
        headers={
            **token_issuer.headers_for(AVERY),
            "If-Match": f'"{version}"',
            "Idempotency-Key": "2c1b9a8e-6f4d-4e3c-8b2a-1d0e9f8a7b6c",
        },
        json={"confirmationName": project.name} if command == "request_deletion" else {},
    )
    want = MATRIX[(command, state)]
    if isinstance(want, int):
        assert response.status_code == want, response.text
    else:
        assert response.json()["error"]["code"] == want, response.text


async def test_lifecycle_commands_require_administrators(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    riley = await join(container, avery, project.project_id, RILEY, ProjectRole.CONTRIBUTOR)
    with pytest.raises(AppError) as caught:
        await scope_of(container, riley, project.project_id, Capability.LIFECYCLE_MANAGE)
    assert caught.value.code is ErrorCode.CAPABILITY_DENIED


# --------------------------------------------------------------------------- archive effects


async def test_archive_and_restore_suspend_and_resume_content(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    hook = RecordingHook()
    lifecycle = _lifecycle_with(container, hooks=[hook])
    scope = await _lifecycle_scope(container, avery, project.project_id)
    archived = await lifecycle.archive(scope, project.version)
    assert archived.lifecycle is Lifecycle.ARCHIVED
    assert Capability.DOCUMENT_UPLOAD not in archived.capabilities
    # Excluded from active lists and multi-Project scope (search, chat).
    active = await container.projects.list(avery, "Active", None)
    assert project.project_id not in [p.project_id for p in active.items]
    with pytest.raises(NotAvailable):
        await container.project_access.require_many(avery, [project.project_id], Capability.SEARCH)

    restored = await lifecycle.restore(scope, archived.version)
    assert restored.lifecycle is Lifecycle.ACTIVE
    assert hook.calls == [("suspend", "Archived"), ("resume", "Archived")]

    current = await _current(container, avery, project.project_id)
    await lifecycle.request_deletion(scope, project.name, current.version)
    recovery = await _lifecycle_scope(container, avery, project.project_id)
    pending = await _current(container, avery, project.project_id)
    await lifecycle.cancel_deletion(recovery, pending.version)
    assert hook.calls[2:] == [("suspend", "PendingDeletion"), ("resume", "PendingDeletion")]


# --------------------------------------------------------------------------- scheduled archival


async def test_scheduling_requires_a_candidate_and_a_firm_manager(
    container: Container, frozen_clock: FrozenClock, avery: Actor, manager: Actor,
    project: ProjectView,
) -> None:  # fmt: skip
    with pytest.raises(AppError) as caught:
        await container.lifecycle.schedule_archival(manager, project.project_id)
    assert caught.value.code is ErrorCode.ARCHIVAL_NOT_CANDIDATE
    frozen_clock.advance(CANDIDATE_AFTER)
    with pytest.raises(GovernanceOnly):
        await container.lifecycle.schedule_archival(avery, project.project_id)
    assert await container.lifecycle.is_archival_candidate(project.project_id)


async def test_schedule_notifies_every_administrator_and_can_be_cancelled(
    container: Container, frozen_clock: FrozenClock, avery: Actor, manager: Actor,
    project: ProjectView,
) -> None:  # fmt: skip
    riley = await join(container, avery, project.project_id, RILEY, ProjectRole.ADMINISTRATOR)
    frozen_clock.advance(CANDIDATE_AFTER)
    schedule = await container.lifecycle.schedule_archival(manager, project.project_id)
    assert schedule.executes_at == frozen_clock.now() + timedelta(days=7)
    for admin in (avery, riley):
        assert "ArchivalScheduled" in await _notifications(container, admin)
    view = await _current(container, avery, project.project_id)
    assert view.archival_schedule is not None
    assert view.lifecycle is Lifecycle.ACTIVE  # a candidate is not a lifecycle state

    scope = await _lifecycle_scope(container, avery, project.project_id)
    cancelled = await container.lifecycle.cancel_archival_schedule(scope)
    assert cancelled.archival_schedule is None
    assert "ArchivalCancelled" in await _notifications(container, riley)


async def test_scheduled_archival_executes_or_skips(
    container: Container, frozen_clock: FrozenClock, avery: Actor, manager: Actor,
    make_worker: Callable[..., Worker],
) -> None:  # fmt: skip
    idle = await new_project(container, avery, "Atlas Expansion")
    resumed = await new_project(container, avery, "Northstar Transformation")
    frozen_clock.advance(CANDIDATE_AFTER)
    await container.lifecycle.schedule_archival(manager, idle.project_id)
    await container.lifecycle.schedule_archival(manager, resumed.project_id)
    await container.projects.mark_active(
        await scope_of(container, avery, resumed.project_id, Capability.LIFECYCLE_MANAGE)
    )

    frozen_clock.advance(timedelta(days=7))
    await make_worker().run_until_idle()

    assert (await _current(container, avery, idle.project_id)).lifecycle is Lifecycle.ARCHIVED
    kept = await _current(container, avery, resumed.project_id)
    assert kept.lifecycle is Lifecycle.ACTIVE
    assert kept.archival_schedule is None
    assert "ArchivalCancelled" in await _notifications(container, avery)


# --------------------------------------------------------------------------- deletion request


async def test_deletion_request_confirmation_and_effects(
    container: Container, api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
    avery: Actor, project: ProjectView, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    caplog.set_level(logging.DEBUG)
    riley = await join(container, avery, project.project_id, RILEY, ProjectRole.ADMINISTRATOR)
    await invite(container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR)
    version = (await _current(container, avery, project.project_id)).version
    url = f"/api/v1/projects/{project.project_id}/deletion"
    headers = {
        **token_issuer.headers_for(AVERY),
        "If-Match": f'"{version}"',
        "Idempotency-Key": "5f0e3d2c-1b0a-4f9e-8d7c-6b5a4f3e2d1c",
    }
    wrong = await api_client.post(
        url, headers=headers, json={"confirmationName": "atlas expansion"}
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "CONFIRMATION_MISMATCH"

    headers["Idempotency-Key"] = "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d"
    accepted = await api_client.post(url, headers=headers, json={"confirmationName": project.name})
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["project"]["lifecycle"] == "PendingDeletion"
    assert body["project"]["deletion"]["operation"] == body["operation"]
    assert body["operation"]["state"] == "Queued"
    replay = await api_client.post(url, headers=headers, json={"confirmationName": project.name})
    assert replay.status_code == 202
    assert replay.json() == body

    for admin in (avery, riley):
        assert "DeletionRequested" in await _notifications(container, admin)
    auth = token_issuer.headers_for(AVERY)
    listed = (await api_client.get("/api/v1/projects?lifecycle=All", headers=auth)).json()
    assert listed["items"] == []
    me = (await api_client.get("/api/v1/me", headers=auth)).json()
    assert me["projects"] == []
    recovery = (await api_client.get("/api/v1/projects/recovery", headers=auth)).json()
    assert [p["projectId"] for p in recovery] == [project.project_id]
    assert recovery[0]["deletion"]["executesAt"] is not None
    assert (
        await api_client.get(f"/api/v1/projects/{project.project_id}", headers=auth)
    ).status_code == 404
    # New uploads (and every other ordinary capability) are refused.
    with pytest.raises(NotAvailable):
        await scope_of(container, avery, project.project_id, Capability.DOCUMENT_UPLOAD)

    operation = await container.operations.get_for(avery, body["operation"]["operationId"])
    assert operation.subject.label == "Project deletion"
    assert operation.project is None  # content-free type

    audit = await container.audit.query(
        ProjectAdministratorViewer(project.project_id), AuditFilters(limit=100), None
    )
    assert AuditAction.PROJECT_DELETION_REQUESTED.value in {e.action for e in audit.items}
    assert "atlas expansion" not in caplog.text
    assert project.name not in audit.model_dump_json()


async def test_cancel_within_window_and_rejected_after_deadline(
    container: Container,
    frozen_clock: FrozenClock,
    avery: Actor,
) -> None:
    first = await new_project(container, avery, "Atlas Expansion")
    await _request_deletion(container, avery, first.project_id)
    frozen_clock.advance(timedelta(days=29, hours=23))
    pending = await _current(container, avery, first.project_id)
    restored = await container.lifecycle.cancel_deletion(
        await _lifecycle_scope(container, avery, first.project_id), pending.version
    )
    assert restored.lifecycle is Lifecycle.ACTIVE
    assert restored.deletion is None
    assert "DeletionCancelled" in await _notifications(container, avery)

    second = await new_project(container, avery, "Northstar Transformation")
    await _request_deletion(container, avery, second.project_id)
    frozen_clock.advance(timedelta(days=30))
    late = await _current(container, avery, second.project_id)
    with pytest.raises(AppError) as caught:
        await container.lifecycle.cancel_deletion(
            await _lifecycle_scope(container, avery, second.project_id), late.version
        )
    assert caught.value.code is ErrorCode.INVALID_TRANSITION


# --------------------------------------------------------------------------- execution


async def test_deletion_executes_after_deadline_and_reports(
    container: Container, frozen_clock: FrozenClock, avery: Actor, manager: Actor,
    project: ProjectView, make_worker: Callable[..., Worker],
) -> None:  # fmt: skip
    _, credential = await invite(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    await _request_deletion(container, avery, project.project_id)

    report = await container.lifecycle.deletion_report(avery, project.project_id)
    assert [s.store for s in report.stores] == list(container.lifecycle.stores)
    assert {s.state.value for s in report.stores} == {"Pending"}
    with pytest.raises(NotAvailable):
        await container.lifecycle.deletion_report(manager, project.project_id)

    await make_worker().run_until_idle()  # not due yet: nothing happens
    assert (await _current(container, avery, project.project_id)).lifecycle is (
        Lifecycle.PENDING_DELETION
    )

    frozen_clock.advance(timedelta(days=30))
    await make_worker().run_until_idle()

    with pytest.raises(NotAvailable):
        await _current(container, avery, project.project_id)
    with pytest.raises(NotAvailable):
        await container.lifecycle.deletion_report(avery, project.project_id)
    final = await container.lifecycle.deletion_report(manager, project.project_id)
    assert {s.state.value for s in final.stores} == {"Completed"}
    assert final.completed_at is not None
    assert "postgres.projects" in [s.store for s in final.stores]
    # The metadata purger removed the Project's invitations (they hold invitee emails).
    with pytest.raises(AppError) as gone:
        await container.invitations.inspect(credential, None, client_key="test")
    assert gone.value.details[0].code == "Unknown"

    audit = await container.audit.query(
        ProjectAdministratorViewer(project.project_id), AuditFilters(limit=100), None
    )
    deleted = next(e for e in audit.items if e.action == AuditAction.PROJECT_DELETED.value)
    assert deleted.facts["storesVerified"] == len(container.lifecycle.stores)
    assert JORDAN.email not in audit.model_dump_json()


async def test_failing_store_is_retried_alone_and_fails_with_work_retained(
    container: Container, frozen_clock: FrozenClock, avery: Actor, project: ProjectView,
) -> None:  # fmt: skip
    first = CountingPurger("first")
    flaky = CountingPurger("flaky", failures=1)
    last = CountingPurger("last")
    lifecycle = _lifecycle_with(container, purgers=[first, flaky, last])

    async def no_progress(done: int, total: int) -> None:
        del done, total

    await _request_deletion(container, avery, project.project_id)
    request_id = await _request_id(container, project.project_id)
    frozen_clock.advance(timedelta(days=30))

    with pytest.raises(RetryableError):
        await lifecycle.execute_deletion(request_id, attempt=1, report=no_progress)
    report = await lifecycle.deletion_report(avery, project.project_id)
    assert [(s.store, s.state.value) for s in report.stores] == [
        ("first", "Completed"),
        ("flaky", "Retrying"),
        ("last", "Pending"),
    ]
    assert (await _current(container, avery, project.project_id)).lifecycle is (
        Lifecycle.PENDING_DELETION
    )

    await lifecycle.execute_deletion(request_id, attempt=2, report=no_progress)
    assert (first.purged, flaky.purged, last.purged) == (1, 2, 1)  # only the failed store re-ran
    with pytest.raises(NotAvailable):
        await _current(container, avery, project.project_id)

    await lifecycle.execute_deletion(request_id, attempt=3, report=no_progress)
    assert (first.purged, flaky.purged, last.purged) == (1, 2, 1)  # a second run is a no-op


async def test_last_attempt_failure_retains_work(
    container: Container, frozen_clock: FrozenClock, avery: Actor, project: ProjectView,
) -> None:  # fmt: skip
    broken = CountingPurger("broken", failures=99)
    lifecycle = _lifecycle_with(container, purgers=[broken])

    async def no_progress(done: int, total: int) -> None:
        del done, total

    await _request_deletion(container, avery, project.project_id)
    request_id = await _request_id(container, project.project_id)
    frozen_clock.advance(timedelta(days=30))
    with pytest.raises(TerminalError):
        await lifecycle.execute_deletion(request_id, attempt=5, report=no_progress)

    current = await _current(container, avery, project.project_id)
    assert current.lifecycle is Lifecycle.PENDING_DELETION
    assert current.deletion is not None
    operation = await container.operations.get_for(avery, current.deletion.operation.operation_id)
    assert operation.state is OperationState.FAILED
    assert operation.failure is not None
    assert operation.failure.work_retained is True
    assert operation.failure.code == "StorePurgeFailed"
    report = await lifecycle.deletion_report(avery, project.project_id)
    assert report.model_dump(mode="json", by_alias=True) == {
        "stores": [{"store": "broken", "state": "Failed"}]
    }


# --------------------------------------------------------------------------- RA-14


async def test_ra14_full_lifecycle(
    container: Container, frozen_clock: FrozenClock, avery: Actor, manager: Actor,
    project: ProjectView, make_worker: Callable[..., Worker],
) -> None:  # fmt: skip
    frozen_clock.advance(CANDIDATE_AFTER)
    await container.lifecycle.schedule_archival(manager, project.project_id)
    await container.lifecycle.governance_cancel_archival(manager, project.project_id)

    scope = await _lifecycle_scope(container, avery, project.project_id)
    archived = await container.lifecycle.archive(scope, project.version)
    restored = await container.lifecycle.restore(scope, archived.version)

    await container.lifecycle.request_deletion(scope, project.name, restored.version)
    pending = await _current(container, avery, project.project_id)
    back = await container.lifecycle.cancel_deletion(
        await _lifecycle_scope(container, avery, project.project_id), pending.version
    )
    await container.lifecycle.request_deletion(scope, project.name, back.version)

    frozen_clock.advance(timedelta(days=30))
    await make_worker().run_until_idle()
    report = await container.lifecycle.deletion_report(manager, project.project_id)
    assert {s.state.value for s in report.stores} == {"Completed"}

    actions = [
        e.action
        for e in (
            await container.audit.query(
                ProjectAdministratorViewer(project.project_id), AuditFilters(limit=100), None
            )
        ).items
    ]
    for action in (
        AuditAction.ARCHIVAL_SCHEDULED,
        AuditAction.ARCHIVAL_UNSCHEDULED,
        AuditAction.PROJECT_ARCHIVED,
        AuditAction.PROJECT_RESTORED,
        AuditAction.PROJECT_DELETION_REQUESTED,
        AuditAction.PROJECT_DELETION_CANCELLED,
        AuditAction.PROJECT_DELETED,
    ):
        assert action.value in actions
