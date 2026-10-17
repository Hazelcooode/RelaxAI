"""``Projects`` (creation, reads, updates, mark-active) and ``ProjectActivity`` (the Latest
Project Activity ledger) — T05. Authorization is ``ProjectAccess``'s job: every method here
takes a ``ProjectScope`` it minted, except creation and listing, which are actor-wide."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import RowMapping

from app.modules.governance import (
    Audit,
    AuditAction,
    OperationRef,
    Operations,
    Outcome,
    SafeFacts,
    TargetRef,
)
from app.modules.organization_access import Actor
from app.modules.projects import repository as repo
from app.modules.projects.authorization import ProjectScope
from app.modules.projects.domain import (
    ActivityKind,
    Capability,
    Lifecycle,
    ProjectRole,
    capabilities_for,
    project_code,
)
from app.modules.projects.tables import projects as projects_table
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork, optimistic_update
from app.platform.errors import CapabilityDenied, FieldIssue, ValidationFailed, VersionConflict
from app.platform.http.pagination import CursorCodec, Page
from app.platform.schema import CamelModel

MAX_NAME = 120
MAX_DESCRIPTION = 1000


# --------------------------------------------------------------------------- views


class ArchivalScheduleView(CamelModel):
    scheduled_by: Literal["FirmManager"] = "FirmManager"
    executes_at: datetime
    scheduled_at: datetime


class DeletionView(CamelModel):
    requested_at: datetime
    executes_at: datetime
    operation: OperationRef


class ProjectView(CamelModel):
    """Contract §6.1 ``Project`` as seen by one member."""

    project_id: str
    name: str
    description: str | None
    code: str
    lifecycle: Lifecycle
    role: ProjectRole
    capabilities: list[Capability]
    latest_activity_at: datetime | None
    created_at: datetime
    archival_schedule: ArchivalScheduleView | None
    deletion: DeletionView | None
    version: int


class LifecycleFilter:
    ACTIVE = "Active"
    ARCHIVED = "Archived"
    ALL = "All"


_FILTERS: dict[str, frozenset[Lifecycle]] = {
    LifecycleFilter.ACTIVE: frozenset({Lifecycle.ACTIVE}),
    LifecycleFilter.ARCHIVED: frozenset({Lifecycle.ARCHIVED}),
    LifecycleFilter.ALL: frozenset({Lifecycle.ACTIVE, Lifecycle.ARCHIVED}),
}


@dataclass(frozen=True, slots=True)
class ProjectPatch:
    """``PATCH /projects/{id}``: only the fields present in the request change."""

    name: str | None = None
    description: str | None = None
    set_description: bool = False


async def project_views(
    tx: Tx,
    operations: Operations,
    rows: Sequence[RowMapping],
    roles: dict[str, ProjectRole],
) -> list[ProjectView]:
    """Views for Project rows with the viewer's role per Project (module-internal helper
    shared with membership and lifecycle services)."""
    ids = [r["id"] for r in rows]
    schedules = await repo.scheduled_archivals(tx, ids)
    deletions = await repo.pending_deletions(tx, ids)
    refs = await operations.refs(tx, [d["operation_id"] for d in deletions.values()])
    views = []
    for row in rows:
        role = roles[row["id"]]
        lifecycle = Lifecycle(row["lifecycle"])
        schedule = schedules.get(row["id"])
        deletion = deletions.get(row["id"]) if lifecycle is Lifecycle.PENDING_DELETION else None
        operation = refs.get(deletion["operation_id"]) if deletion is not None else None
        views.append(
            ProjectView(
                project_id=row["id"],
                name=row["name"],
                description=row["description"],
                code=str(row["code"]),
                lifecycle=lifecycle,
                role=role,
                capabilities=capabilities_for(role, lifecycle),
                latest_activity_at=row["latest_activity_at"],
                created_at=row["created_at"],
                archival_schedule=ArchivalScheduleView(
                    executes_at=schedule["executes_at"], scheduled_at=schedule["scheduled_at"]
                )
                if schedule is not None
                else None,
                deletion=DeletionView(
                    requested_at=deletion["requested_at"],
                    executes_at=deletion["executes_at"],
                    operation=operation,
                )
                if deletion is not None and operation is not None
                else None,
                version=row["version"],
            )
        )
    return views


# --------------------------------------------------------------------------- ledger


class ProjectActivity:
    """Latest Project Activity (PRJ-05). Only ``ActivityKind`` events move it; read paths
    (views, search, chat), failed imports, and reprocessing must never call ``record``."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock

    async def record(
        self,
        tx: Tx,
        scope: ProjectScope,
        kind: ActivityKind,
        source_ref: str | None = None,
    ) -> None:
        await self.record_for(tx, scope.firm_id, scope.project_id, kind, scope.user_id, source_ref)

    async def record_for(
        self,
        tx: Tx,
        firm_id: str,
        project_id: str,
        kind: ActivityKind,
        actor_user_id: str | None,
        source_ref: str | None = None,
    ) -> None:
        """Module-internal variant for commands that run before a scope exists (Project
        creation, invitation acceptance)."""
        await repo.record_activity(
            tx,
            firm_id=firm_id,
            project_id=project_id,
            kind=kind,
            occurred_at=self._clock.now(),
            actor_user_id=actor_user_id,
            source_ref=source_ref,
        )


# --------------------------------------------------------------------------- service


class Projects:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        firm_id: str,
        audit: Audit,
        operations: Operations,
        activity: ProjectActivity,
        cursors: CursorCodec,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._firm_id = firm_id
        self._audit = audit
        self._operations = operations
        self._activity = activity
        self._cursors = cursors

    async def create(self, actor: Actor, name: str, description: str | None) -> ProjectView:
        """``project.create``: active Team Members only. The creator becomes the Project's
        Administrator in the same transaction (PRJ-01)."""
        if not actor.is_team_member or actor.user_id is None:
            raise CapabilityDenied()
        clean_name = _clean_name(name)
        clean_description = _clean_description(description)
        now = self._clock.now()
        async with self._uow() as tx:
            sequence = await repo.next_code_sequence(tx, self._firm_id)
            row = await repo.insert_project(
                tx,
                firm_id=self._firm_id,
                code=project_code(clean_name, sequence),
                name=clean_name,
                description=clean_description,
                created_by=actor.user_id,
                now=now,
            )
            membership = await repo.insert_membership(
                tx,
                firm_id=self._firm_id,
                project_id=row["id"],
                user_id=actor.user_id,
                role=ProjectRole.ADMINISTRATOR,
                created_by=actor.user_id,
                now=now,
            )
            await self._activity.record_for(
                tx, self._firm_id, row["id"], ActivityKind.MEMBERSHIP_CHANGED, actor.user_id,
                membership["id"],
            )  # fmt: skip
            await repo.bump_authorization_version(tx, self._firm_id)
            await self._audit.record(
                tx,
                actor=actor.actor_ref,
                action=AuditAction.PROJECT_CREATED,
                target=TargetRef("project", row["id"]),
                outcome=Outcome.SUCCEEDED,
                project_id=row["id"],
            )
            await self._audit.record(
                tx,
                actor=actor.actor_ref,
                action=AuditAction.MEMBERSHIP_ADDED,
                target=TargetRef("membership", membership["id"]),
                outcome=Outcome.SUCCEEDED,
                project_id=row["id"],
                facts=SafeFacts(role=ProjectRole.ADMINISTRATOR.value),
            )
            created = await repo.project_row(tx, self._firm_id, row["id"])
            assert created is not None  # noqa: S101 - inserted above
            views = await project_views(
                tx, self._operations, [created], {row["id"]: ProjectRole.ADMINISTRATOR}
            )
        return views[0]

    async def list(
        self, actor: Actor, lifecycle: str, cursor: str | None, *, limit: int = 25
    ) -> Page[ProjectView]:
        """The actor's Projects by name. Pending Deletion Projects never appear here; they
        are on the recovery list only."""
        if not actor.is_active or actor.user_id is None:
            return Page[ProjectView](items=[], next_cursor=None)
        after = None
        if cursor is not None:
            decoded = self._cursors.decode(cursor)
            if not isinstance(decoded.k, str):
                raise ValidationFailed(
                    [FieldIssue(field="cursor", code="invalid", message="Invalid cursor.")]
                )
            after = (decoded.k, decoded.id)
        async with self._uow() as tx:
            rows = await repo.page_member_projects(
                tx, self._firm_id, actor.user_id, _FILTERS[lifecycle], after=after,
                limit=limit + 1,
            )  # fmt: skip
            page = rows[:limit]
            views = await project_views(
                tx, self._operations, page, {r["id"]: ProjectRole(r["role"]) for r in page}
            )
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = self._cursors.encode(last["name"], last["id"])
        return Page[ProjectView](items=views, next_cursor=next_cursor)

    async def get(self, scope: ProjectScope) -> ProjectView:
        async with self._uow() as tx:
            return await self.view(tx, scope)

    async def update(self, scope: ProjectScope, patch: ProjectPatch, version: int) -> ProjectView:
        """``project.update`` (Administrators; Archived is already read-only via the scope)."""
        values: dict[str, object] = {}
        if patch.name is not None:
            values["name"] = _clean_name(patch.name)
        if patch.set_description:
            values["description"] = _clean_description(patch.description)
        async with self._uow() as tx:
            if not values:
                row = await repo.project_row(tx, scope.firm_id, scope.project_id)
                assert row is not None  # noqa: S101 - the scope proves it exists
                if row["version"] != version:
                    raise VersionConflict(await self.view(tx, scope))
                return await self.view(tx, scope)
            values["updated_at"] = self._clock.now()
            try:
                await optimistic_update(tx, projects_table, scope.project_id, version, values)
            except VersionConflict:
                raise VersionConflict(await self.view(tx, scope)) from None
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.PROJECT_UPDATED,
                target=TargetRef("project", scope.project_id),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
                facts=SafeFacts(fields="+".join(sorted(k for k in values if k != "updated_at"))),
            )
            return await self.view(tx, scope)

    async def mark_active(self, scope: ProjectScope) -> ProjectView:
        """An Administrator's explicit "still active" signal (``AdministratorMarkedActive``)."""
        scope.require_writable()
        async with self._uow() as tx:
            await self._activity.record(tx, scope, ActivityKind.ADMINISTRATOR_MARKED_ACTIVE)
            return await self.view(tx, scope)

    async def view(self, tx: Tx, scope: ProjectScope) -> ProjectView:
        row = await repo.project_row(tx, scope.firm_id, scope.project_id)
        assert row is not None  # noqa: S101 - the scope proves it exists
        views = await project_views(tx, self._operations, [row], {row["id"]: scope.role})
        return views[0]


def _clean_name(name: str) -> str:
    clean = name.strip()
    if not 1 <= len(clean) <= MAX_NAME:
        raise ValidationFailed(
            [FieldIssue(field="name", code="string_length", message="Name is 1-120 characters.")]
        )
    return clean


def _clean_description(description: str | None) -> str | None:
    if description is None:
        return None
    clean = description.strip()
    if len(clean) > MAX_DESCRIPTION:
        raise ValidationFailed(
            [
                FieldIssue(
                    field="description",
                    code="string_too_long",
                    message="Description is at most 1000 characters.",
                )
            ]
        )
    return clean or None
