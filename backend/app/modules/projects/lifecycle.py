"""``ProjectLifecycle``: archive and restore, scheduled archival, and deletion from request
through the 30-day recovery window to verified cross-store erasure (T07).

Deletion reaches every store through the ``ProjectPurger`` registry (conventions §4.9): the
coordinator iterates whatever adapters the composition root registered and never knows what
the stores are. Adding a store means adding an adapter.

The Project row survives deletion as a tombstone that keeps its name — the content is gone
and the name is not content (docs/adr/0001-project-deletion-tombstone.md).
"""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum
from typing import NoReturn, Protocol, runtime_checkable

from pydantic import BaseModel
from sqlalchemy import RowMapping

from app.modules.actions import Notifications, NotificationTrigger, SafeSubjectRef
from app.modules.governance import (
    Audience,
    Audit,
    AuditAction,
    Destination,
    JobQueue,
    OperationRef,
    Operations,
    OperationType,
    Outcome,
    RetryableError,
    SafeFacts,
    SafeSubject,
    SystemActor,
    TargetRef,
    TerminalError,
)
from app.modules.organization_access import Actor, OrganizationAccess
from app.modules.projects import repository as repo
from app.modules.projects.authorization import ProjectAccess, ProjectScope
from app.modules.projects.domain import (
    ARCHIVAL_NOTICE_PERIOD,
    DELETION_RECOVERY_WINDOW,
    AccessPurpose,
    Capability,
    DeletionState,
    Lifecycle,
    LifecycleCommand,
    ProjectRole,
    ScheduleState,
    WorkItemState,
    allowed_commands,
    confirmation_matches,
    is_archival_candidate,
    next_lifecycle,
)
from app.modules.projects.service import ArchivalScheduleView, ProjectView, project_views
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork
from app.platform.errors import (
    AppError,
    ErrorCode,
    GovernanceOnly,
    InvalidTransition,
    NotAvailable,
    VersionConflict,
)
from app.platform.schema import CamelModel, OmitWhenNone

ARCHIVAL_EXECUTE = "projects.archival_execute"
PROJECT_DELETE = "governance.project_delete"
DELETION_MAX_ATTEMPTS = 5


# --------------------------------------------------------------------------- seams


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """Content-free outcome of one store's purge: the store name only."""

    store: str


@runtime_checkable
class ProjectPurger(Protocol):
    """One adapter per store that holds Project data (frozen seam, conventions §4.9)."""

    store: str  # "postgres.knowledge", "blob.retained", "qdrant", ...

    async def purge(self, project_id: str) -> PurgeReport:
        """Remove everything this store holds for the Project. Idempotent."""
        ...

    async def verify(self, project_id: str) -> bool:
        """True only when the store provably holds nothing for the Project."""
        ...


class VerifiedNoopPurger:
    """A store whose owner has not landed yet: it holds no Project data, so purging is a
    no-op and verification is trivially true. The owning task replaces it with a real
    adapter (the registry entry stays, so the deletion report already names the store)."""

    def __init__(self, store: str) -> None:
        self.store = store

    async def purge(self, project_id: str) -> PurgeReport:
        del project_id
        return PurgeReport(store=self.store)

    async def verify(self, project_id: str) -> bool:
        del project_id
        return True


class ProjectMetadataPurger:
    """``postgres.projects``, purged last: invitations (they hold invitee emails), their
    events, the activity ledger, and archival schedules. The Project row and its membership
    history stay as the tombstone (docs/adr/0001-project-deletion-tombstone.md)."""

    store = "postgres.projects"

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def purge(self, project_id: str) -> PurgeReport:
        async with self._uow() as tx:
            await repo.purge_project_metadata(tx, project_id)
        return PurgeReport(store=self.store)

    async def verify(self, project_id: str) -> bool:
        async with self._uow() as tx:
            return not await repo.project_metadata_remains(tx, project_id)


class SuspendReason(StrEnum):
    ARCHIVED = "Archived"
    PENDING_DELETION = "PendingDeletion"


class LifecycleHook(Protocol):
    """Content modules react to a Project leaving or re-entering the Active state in the
    lifecycle command's transaction: pause Source Connections (T21/T22), suspend retrieval
    projections (T16), cancel enrichment jobs that have not started. ``resume`` reverses
    only what ``suspend`` did for the same reason — a source whose consent was revoked
    meanwhile stays paused."""

    name: str

    async def suspend(self, tx: Tx, project_id: str, reason: SuspendReason) -> None: ...

    async def resume(self, tx: Tx, project_id: str, reason: SuspendReason) -> None: ...


# --------------------------------------------------------------------------- views


class DeletionRequestResult(CamelModel):
    """``202`` body of ``POST /projects/{id}/deletion``."""

    project: ProjectView
    operation: OperationRef


class StoreReportView(OmitWhenNone):
    omit_when_none = frozenset({"completed_at"})

    store: str
    state: WorkItemState
    completed_at: datetime | None = None


class DeletionReportView(OmitWhenNone):
    """Store name, state, and completion time only — never counts or identifiers of
    content."""

    omit_when_none = frozenset({"completed_at"})

    stores: list[StoreReportView]
    completed_at: datetime | None = None


type ReportProgress = Callable[[int, int], Awaitable[None]]


# --------------------------------------------------------------------------- service


class ProjectLifecycle:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        firm_id: str,
        access: ProjectAccess,
        audit: Audit,
        operations: Operations,
        notifications: Notifications,
        queue: JobQueue,
        directory: OrganizationAccess,
        purgers: Sequence[ProjectPurger],
        hooks: Sequence[LifecycleHook] = (),
        zone: tzinfo,
    ) -> None:
        stores = [p.store for p in purgers]
        if len(set(stores)) != len(stores):
            raise ValueError("each store has exactly one ProjectPurger")
        self._uow = uow
        self._clock = clock
        self._firm_id = firm_id
        self._access = access
        self._audit = audit
        self._operations = operations
        self._notifications = notifications
        self._queue = queue
        self._directory = directory
        self._purgers = tuple(purgers)
        self._hooks = tuple(hooks)
        self._zone = zone

    @property
    def stores(self) -> tuple[str, ...]:
        """Registered stores in purge order."""
        return tuple(p.store for p in self._purgers)

    # ------------------------------------------------------------------ archive / restore

    async def archive(self, scope: ProjectScope, version: int) -> ProjectView:
        """Active -> Archived: read-only, sources paused, excluded from active lists, All
        Projects search, and chat scope expansion (PRJ-08)."""
        async with self._uow() as tx:
            row = await self._transition(tx, scope, version, LifecycleCommand.ARCHIVE)
            await self._archive(tx, row)
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.PROJECT_ARCHIVED,
                target=TargetRef("project", scope.project_id),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
                facts=SafeFacts(initiatedBy="Administrator"),
            )
            return await self._view(tx, scope.project_id, scope.role)

    async def restore(self, scope: ProjectScope, version: int) -> ProjectView:
        """Archived -> Active without recreating content."""
        async with self._uow() as tx:
            await self._transition(tx, scope, version, LifecycleCommand.RESTORE)
            for hook in self._hooks:
                await hook.resume(tx, scope.project_id, SuspendReason.ARCHIVED)
            await repo.bump_authorization_version(tx, self._firm_id)
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.PROJECT_RESTORED,
                target=TargetRef("project", scope.project_id),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
            )
            return await self._view(tx, scope.project_id, scope.role)

    # ------------------------------------------------------------------ scheduled archival

    async def schedule_archival(self, actor: Actor, project_id: str) -> ArchivalScheduleView:
        """Firm Manager scheduling (route in T30): only a real Archival Candidate, executing
        seven days later, with notice to every Project Administrator (PRJ-06, PRJ-07).
        Scheduling an already scheduled Project returns the existing schedule."""
        if not actor.is_active or not actor.firm_manager or actor.user_id is None:
            raise GovernanceOnly()
        now = self._clock.now()
        async with self._uow() as tx:
            project = await repo.project_row(tx, self._firm_id, project_id, for_update=True)
            if project is None or Lifecycle(project["lifecycle"]) not in (
                Lifecycle.ACTIVE,
                Lifecycle.ARCHIVED,
            ):
                raise NotAvailable()
            existing = (await repo.scheduled_archivals(tx, [project_id])).get(project_id)
            if existing is not None:
                return _schedule_view(existing)
            if Lifecycle(project["lifecycle"]) is not Lifecycle.ACTIVE or not self._candidate(
                project, now
            ):
                raise AppError(ErrorCode.ARCHIVAL_NOT_CANDIDATE)
            executes_at = now + ARCHIVAL_NOTICE_PERIOD
            recipients = await self._notify_administrators(
                tx, project_id, NotificationTrigger.ARCHIVAL_SCHEDULED, "archival.scheduled",
                {"executesOn": self._local_date(executes_at)},
            )  # fmt: skip
            schedule = await repo.insert_schedule(
                tx,
                firm_id=self._firm_id,
                project_id=project_id,
                scheduled_by=actor.user_id,
                now=now,
                executes_at=executes_at,
                recipients=recipients,
            )
            await self._queue.enqueue(
                tx,
                ARCHIVAL_EXECUTE,
                ArchivalExecutePayload(schedule_id=schedule["id"]),
                idempotency_key=f"archival:{schedule['id']}",
                run_at=executes_at,
            )
            await self._audit.record(
                tx,
                actor=actor.actor_ref,
                action=AuditAction.ARCHIVAL_SCHEDULED,
                target=TargetRef("project", project_id),
                outcome=Outcome.SUCCEEDED,
                project_id=project_id,
                facts=SafeFacts(executesAt=executes_at),
            )
        return _schedule_view(schedule)

    async def cancel_archival_schedule(self, scope: ProjectScope) -> ProjectView:
        """``DELETE /projects/{id}/archival-schedule``: an Administrator cancels the Firm
        Manager's schedule before it executes; every Administrator is told."""
        scope.require_writable()
        async with self._uow() as tx:
            await self._cancel_schedule(tx, scope.project_id, scope.actor)
            return await self._view(tx, scope.project_id, scope.role)

    async def governance_cancel_archival(self, actor: Actor, project_id: str) -> None:
        """The Firm Manager's own cancellation (T30's route): no membership, no view."""
        if not actor.is_active or not actor.firm_manager:
            raise GovernanceOnly()
        async with self._uow() as tx:
            if await repo.project_row(tx, self._firm_id, project_id) is None:
                raise NotAvailable()
            await self._cancel_schedule(tx, project_id, actor)

    async def is_archival_candidate(self, project_id: str) -> bool:
        """The policy result T30's minimal Project list shows (never a lifecycle state)."""
        async with self._uow() as tx:
            project = await repo.project_row(tx, self._firm_id, project_id)
        if project is None or Lifecycle(project["lifecycle"]) is not Lifecycle.ACTIVE:
            return False
        return self._candidate(project, self._clock.now())

    async def execute_archival(self, schedule_id: str) -> None:
        """``projects.archival_execute`` at ``executes_at``: archive if the Project is still
        an Active candidate; otherwise skip and tell the Administrators why."""
        now = self._clock.now()
        async with self._uow() as tx:
            schedule = await repo.schedule(tx, schedule_id, for_update=True)
            if schedule is None or ScheduleState(schedule["state"]) is not ScheduleState.SCHEDULED:
                return
            project_id = schedule["project_id"]
            project = await repo.project_row(tx, self._firm_id, project_id, for_update=True)
            assert project is not None  # noqa: S101 - schedules reference existing Projects
            lifecycle = Lifecycle(project["lifecycle"])
            system = SystemActor("Archival schedule")
            if lifecycle is Lifecycle.ACTIVE and self._candidate(project, now):
                await repo.set_lifecycle(tx, project_id, Lifecycle.ARCHIVED, now=now)
                await self._archive(tx, project)
                await self._audit.record(
                    tx,
                    actor=system,
                    action=AuditAction.PROJECT_ARCHIVED,
                    target=TargetRef("project", project_id),
                    outcome=Outcome.SUCCEEDED,
                    project_id=project_id,
                    facts=SafeFacts(initiatedBy="FirmManagerSchedule"),
                )
                return
            await repo.set_schedule_state(
                tx, schedule_id, ScheduleState.CANCELLED, cancelled_at=now
            )
            if lifecycle is Lifecycle.ACTIVE:
                await self._notify_administrators(
                    tx, project_id, NotificationTrigger.ARCHIVAL_CANCELLED, "archival.skipped", {}
                )
            await self._audit.record(
                tx,
                actor=system,
                action=AuditAction.ARCHIVAL_UNSCHEDULED,
                target=TargetRef("project", project_id),
                outcome=Outcome.SUCCEEDED,
                project_id=project_id,
                facts=SafeFacts(outcome="SkippedNoLongerCandidate"),
            )

    # ------------------------------------------------------------------ deletion

    async def request_deletion(
        self, scope: ProjectScope, confirmation_name: str, version: int
    ) -> DeletionRequestResult:
        """Active -> Pending Deletion for 30 days. The typed name must equal the current
        name exactly; it is compared and discarded, never stored or logged (PRJ-09)."""
        now = self._clock.now()
        async with self._uow() as tx:
            project = await self._locked(tx, scope, version)
            target = next_lifecycle(
                Lifecycle(project["lifecycle"]), LifecycleCommand.REQUEST_DELETION
            )
            if target is None:
                raise InvalidTransition(allowed_commands(Lifecycle(project["lifecycle"])))
            if not confirmation_matches(confirmation_name, project["name"]):
                raise AppError(ErrorCode.CONFIRMATION_MISMATCH)
            deadline = now + DELETION_RECOVERY_WINDOW
            await repo.set_lifecycle(
                tx, scope.project_id, target, now=now, deletion_deadline=deadline
            )
            schedule = (await repo.scheduled_archivals(tx, [scope.project_id])).get(
                scope.project_id
            )
            if schedule is not None:
                await repo.set_schedule_state(
                    tx, schedule["id"], ScheduleState.CANCELLED,
                    cancelled_at=now, cancelled_by=scope.user_id,
                )  # fmt: skip
            for hook in self._hooks:
                await hook.suspend(tx, scope.project_id, SuspendReason.PENDING_DELETION)
            operation = await self._operations.start(
                tx,
                type=OperationType.PROJECT_DELETION,
                subject=SafeSubject(label="Project deletion"),
                initiated_by=scope.actor.actor_ref,
                project_id=scope.project_id,
                destination=Destination(kind="ProjectRecovery", path="/projects/recovery"),
                audience=Audience.holders_of(Capability.LIFECYCLE_MANAGE.value, scope.project_id),
            )
            request = await repo.insert_deletion_request(
                tx,
                firm_id=self._firm_id,
                project_id=scope.project_id,
                requested_by=scope.user_id,
                now=now,
                executes_at=deadline,
                operation_id=operation.operation_id,
            )
            await self._queue.enqueue(
                tx,
                PROJECT_DELETE,
                ProjectDeletePayload(project_id=scope.project_id, request_id=request["id"]),
                # One job per request, so a deletion re-requested after a cancellation gets
                # its own execution (``project:{id}:delete`` alone would replay the first).
                idempotency_key=f"project:{scope.project_id}:delete:{request['id']}",
                operation_id=operation.operation_id,
                run_at=deadline,
            )
            await self._notify_administrators(
                tx, scope.project_id, NotificationTrigger.DELETION_REQUESTED,
                "deletion.requested", {"executesOn": self._local_date(deadline)},
            )  # fmt: skip
            await repo.bump_authorization_version(tx, self._firm_id)
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.PROJECT_DELETION_REQUESTED,
                target=TargetRef("project", scope.project_id),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
                facts=SafeFacts(executesAt=deadline, operationId=operation.operation_id),
            )
            view = await self._view(tx, scope.project_id, scope.role)
        return DeletionRequestResult(project=view, operation=operation)

    async def cancel_deletion(self, scope: ProjectScope, version: int) -> ProjectView:
        """Pending Deletion -> Active, only before the deadline (scope minted with
        ``AccessPurpose.RECOVERY``)."""
        now = self._clock.now()
        async with self._uow() as tx:
            project = await self._locked(tx, scope, version)
            lifecycle = Lifecycle(project["lifecycle"])
            if next_lifecycle(lifecycle, LifecycleCommand.CANCEL_DELETION) is None:
                raise InvalidTransition(allowed_commands(lifecycle))
            request = (await repo.pending_deletions(tx, [scope.project_id])).get(scope.project_id)
            if request is None or now >= request["executes_at"]:
                raise InvalidTransition(allowed_transitions=[])
            await repo.set_lifecycle(tx, scope.project_id, Lifecycle.ACTIVE, now=now)
            await repo.set_request_state(
                tx, request["id"], DeletionState.CANCELLED,
                cancelled_at=now, cancelled_by=scope.user_id,
            )  # fmt: skip
            await self._operations.finish(tx, request["operation_id"], outcome="Cancelled")
            for hook in self._hooks:
                await hook.resume(tx, scope.project_id, SuspendReason.PENDING_DELETION)
            await self._notify_administrators(
                tx, scope.project_id, NotificationTrigger.DELETION_CANCELLED,
                "deletion.cancelled", {},
            )  # fmt: skip
            await repo.bump_authorization_version(tx, self._firm_id)
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.PROJECT_DELETION_CANCELLED,
                target=TargetRef("project", scope.project_id),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
            )
            return await self._view(tx, scope.project_id, scope.role)

    async def recovery_list(self, actor: Actor) -> builtins.list[ProjectView]:
        """Pending Deletion Projects where the actor is an active Administrator."""
        if not actor.is_active or actor.user_id is None:
            return []
        async with self._uow() as tx:
            members = await repo.member_rows(
                tx, self._firm_id, actor.user_id, [Lifecycle.PENDING_DELETION]
            )
            ids = [m["id"] for m in members if m["role"] == ProjectRole.ADMINISTRATOR.value]
            rows = sorted(await repo.project_rows(tx, self._firm_id, ids), key=_by_name)
            return await project_views(
                tx, self._operations, rows, dict.fromkeys(ids, ProjectRole.ADMINISTRATOR)
            )

    async def deletion_report(self, actor: Actor, project_id: str) -> DeletionReportView:
        """Administrators during recovery; a Firm Manager after completion (PRJ-10)."""
        executed = False
        try:
            await self._access.require(
                actor, project_id, Capability.LIFECYCLE_MANAGE, purpose=AccessPurpose.RECOVERY
            )
        except NotAvailable:
            if not (actor.is_active and actor.firm_manager):
                raise
            executed = True
        async with self._uow() as tx:
            project = await repo.project_row(tx, self._firm_id, project_id)
            if project is None:
                raise NotAvailable()
            lifecycle = Lifecycle(project["lifecycle"])
            if executed and lifecycle is not Lifecycle.DELETED:
                raise NotAvailable()
            if not executed and lifecycle is not Lifecycle.PENDING_DELETION:
                raise NotAvailable()
            state = DeletionState.EXECUTED if executed else DeletionState.PENDING
            request = await repo.latest_deletion_request(tx, project_id, state)
            if request is None:
                raise NotAvailable()
            items = {i["store"]: i for i in await repo.work_items(tx, request["id"])}
        stores = [
            StoreReportView(
                store=item["store"],
                state=WorkItemState(item["state"]),
                completed_at=item["completed_at"],
            )
            for item in sorted(items.values(), key=lambda i: int(i["position"]))
        ]
        # Before execution starts, every registered store is simply Pending.
        stores += [
            StoreReportView(store=store, state=WorkItemState.PENDING)
            for store in self.stores
            if store not in items
        ]
        return DeletionReportView(stores=stores, completed_at=request["executed_at"])

    async def execute_deletion(
        self, request_id: str, *, attempt: int, report: ReportProgress
    ) -> None:
        """``governance.project_delete``: purge and verify every registered store, then mark
        the Project Deleted.

        Purge order is the registry order the composition root chose: derived and remote
        stores first, authoritative PostgreSQL rows next, the Project's own metadata last. A
        retry must still be able to enumerate what remains, which needs the authoritative
        rows to outlive the derived stores (docs/adr/0002-deletion-purge-order.md). The run
        stops at the first failing store; completed stores are never re-run.
        """
        now = self._clock.now()
        async with self._uow() as tx:
            request = await repo.deletion_request(tx, request_id)
            if request is None or DeletionState(request["state"]) is not DeletionState.PENDING:
                return
            if now < request["executes_at"]:
                raise RetryableError("NotDue", retry_after=request["executes_at"] - now)
            project = await repo.project_row(tx, self._firm_id, request["project_id"])
            if project is None or Lifecycle(project["lifecycle"]) is not Lifecycle.PENDING_DELETION:
                return
            await repo.ensure_work_items(tx, request_id, self.stores)
            items = {i["store"]: i for i in await repo.work_items(tx, request_id)}
        project_id = request["project_id"]
        total = len(self._purgers)
        done = sum(
            1
            for p in self._purgers
            if WorkItemState(items[p.store]["state"]) is WorkItemState.COMPLETED
        )
        await report(done, total)
        for purger in self._purgers:
            if WorkItemState(items[purger.store]["state"]) is WorkItemState.COMPLETED:
                continue
            async with self._uow() as tx:
                await repo.start_work_item(tx, request_id, purger.store, now=self._clock.now())
            failure = await _purge_and_verify(purger, project_id)
            if failure is not None:
                await self._store_failed(request, purger.store, failure, attempt)
            async with self._uow() as tx:
                await repo.update_work_item(
                    tx, request_id, purger.store,
                    state=WorkItemState.COMPLETED.value, completed_at=self._clock.now(),
                    last_error_code=None,
                )  # fmt: skip
            done += 1
            await report(done, total)

        async with self._uow() as tx:
            completed = self._clock.now()
            await repo.set_lifecycle(tx, project_id, Lifecycle.DELETED, now=completed)
            await repo.set_request_state(
                tx, request_id, DeletionState.EXECUTED, executed_at=completed
            )
            await self._operations.finish(tx, request["operation_id"], outcome="Succeeded")
            await repo.bump_authorization_version(tx, self._firm_id)
            await self._audit.record(
                tx,
                actor=SystemActor("Project deletion"),
                action=AuditAction.PROJECT_DELETED,
                target=TargetRef("project", project_id),
                outcome=Outcome.SUCCEEDED,
                project_id=project_id,
                facts=SafeFacts(storesVerified=total, operationId=request["operation_id"]),
            )

    # ------------------------------------------------------------------ internals

    async def _store_failed(
        self, request: RowMapping, store: str, code: str, attempt: int
    ) -> NoReturn:
        """Record the failure; on the last attempt fail the Operation with the work retained
        (the Project stays Pending Deletion and the item is surfaced to operators)."""
        final = attempt >= DELETION_MAX_ATTEMPTS
        async with self._uow() as tx:
            await repo.update_work_item(
                tx, request["id"], store,
                state=(WorkItemState.FAILED if final else WorkItemState.RETRYING).value,
                last_error_code=code,
            )  # fmt: skip
            if final:
                await self._operations.finish(
                    tx,
                    request["operation_id"],
                    outcome="Failed",
                    failure_code="StorePurgeFailed",
                    work_retained=True,
                    retryable=True,
                )
        if final:
            raise TerminalError("StorePurgeFailed")
        raise RetryableError("StorePurgeFailed")

    async def _transition(
        self, tx: Tx, scope: ProjectScope, version: int, command: LifecycleCommand
    ) -> RowMapping:
        project = await self._locked(tx, scope, version)
        lifecycle = Lifecycle(project["lifecycle"])
        target = next_lifecycle(lifecycle, command)
        if target is None:
            raise InvalidTransition(allowed_commands(lifecycle))
        return await repo.set_lifecycle(tx, scope.project_id, target, now=self._clock.now())

    async def _locked(self, tx: Tx, scope: ProjectScope, version: int) -> RowMapping:
        project = await repo.project_row(tx, self._firm_id, scope.project_id, for_update=True)
        if project is None:
            raise NotAvailable()
        if project["version"] != version:
            raise VersionConflict(await self._view(tx, scope.project_id, scope.role))
        return project

    async def _archive(self, tx: Tx, project: RowMapping) -> None:
        """Effects shared by manual and scheduled archival (the lifecycle is already set)."""
        project_id = project["id"]
        schedule = (await repo.scheduled_archivals(tx, [project_id])).get(project_id)
        if schedule is not None:
            await repo.set_schedule_state(tx, schedule["id"], ScheduleState.EXECUTED)
        for hook in self._hooks:
            await hook.suspend(tx, project_id, SuspendReason.ARCHIVED)
        await repo.bump_authorization_version(tx, self._firm_id)

    async def _cancel_schedule(self, tx: Tx, project_id: str, actor: Actor) -> None:
        schedule = (await repo.scheduled_archivals(tx, [project_id])).get(project_id)
        if schedule is None:
            raise InvalidTransition(allowed_transitions=[])
        await repo.set_schedule_state(
            tx, schedule["id"], ScheduleState.CANCELLED,
            cancelled_at=self._clock.now(), cancelled_by=actor.user_id,
        )  # fmt: skip
        await self._notify_administrators(
            tx, project_id, NotificationTrigger.ARCHIVAL_CANCELLED, "archival.cancelled", {}
        )
        await self._audit.record(
            tx,
            actor=actor.actor_ref,
            action=AuditAction.ARCHIVAL_UNSCHEDULED,
            target=TargetRef("project", project_id),
            outcome=Outcome.SUCCEEDED,
            project_id=project_id,
            facts=SafeFacts(outcome="Cancelled"),
        )

    async def _notify_administrators(
        self,
        tx: Tx,
        project_id: str,
        trigger: NotificationTrigger,
        template_key: str,
        context: dict[str, str | int],
    ) -> builtins.list[str]:
        """Notify every active Administrator; returns who was notified."""
        admin_ids = await repo.administrator_user_ids(tx, project_id)
        profiles = await self._directory.profiles(admin_ids)
        notified = []
        for user_id in admin_ids:
            profile = profiles.get(user_id)
            if profile is None or not profile.active:
                continue
            await self._notifications.enqueue(
                tx,
                recipient_user_id=user_id,
                trigger=trigger,
                template_key=template_key,
                subject_ref=SafeSubjectRef("Project", project_id),
                safe_context=context,
                project_id=project_id,
            )
            notified.append(user_id)
        return notified

    def _candidate(self, project: RowMapping, now: datetime) -> bool:
        return is_archival_candidate(
            project["latest_activity_at"], project["created_at"], now, self._zone
        )

    def _local_date(self, moment: datetime) -> str:
        return moment.astimezone(self._zone).date().isoformat()

    async def _view(self, tx: Tx, project_id: str, role: ProjectRole) -> ProjectView:
        row = await repo.project_row(tx, self._firm_id, project_id)
        assert row is not None  # noqa: S101 - callers hold the row lock
        return (await project_views(tx, self._operations, [row], {project_id: role}))[0]


async def _purge_and_verify(purger: ProjectPurger, project_id: str) -> str | None:
    """A content-free failure code, or ``None`` when the store is verified empty. Exception
    messages are never recorded: they may carry content or identifiers."""
    try:
        await purger.purge(project_id)
    except Exception:  # an adapter failure of any kind is a retryable store failure
        return "PurgeFailed"
    try:
        verified = await purger.verify(project_id)
    except Exception:
        return "VerifyFailed"
    return None if verified else "NotVerified"


def _by_name(row: RowMapping) -> tuple[str, str]:
    return (str(row["name"]), str(row["id"]))


def _schedule_view(row: RowMapping) -> ArchivalScheduleView:
    return ArchivalScheduleView(executes_at=row["executes_at"], scheduled_at=row["scheduled_at"])


# --------------------------------------------------------------------------- job payloads


class ArchivalExecutePayload(BaseModel):
    schedule_id: str


class ProjectDeletePayload(BaseModel):
    project_id: str
    request_id: str
