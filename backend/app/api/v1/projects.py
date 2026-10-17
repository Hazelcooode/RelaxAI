"""Projects and Project lifecycle (contract §6.1; T05, T07).

Every Project-scoped route obtains its ``ProjectScope`` from ``ProjectAccess`` through
``project_scope``; nothing here interprets a role.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field

from app.api.v1.conventions import Idempotent
from app.api.v1.deps import ActiveActor, AppContainer
from app.api.v1.schemas.base import ApiRequest
from app.modules.projects import (
    AccessPurpose,
    Capability,
    DeletionReportView,
    DeletionRequestResult,
    ProjectPatch,
    ProjectScope,
    ProjectView,
)
from app.platform.http.concurrency import IfMatch, set_etag
from app.platform.http.pagination import Page, PageParams, page_params

router = APIRouter(tags=["projects"])


def project_scope(
    capability: Capability, purpose: AccessPurpose = AccessPurpose.ORDINARY
) -> Callable[..., Awaitable[ProjectScope]]:
    """Dependency: the trusted scope for ``{project_id}``, or the access error."""

    async def dependency(
        project_id: str, container: AppContainer, actor: ActiveActor
    ) -> ProjectScope:
        return await container.project_access.require(
            actor, project_id, capability, purpose=purpose
        )

    return dependency


ReadScope = Annotated[ProjectScope, Depends(project_scope(Capability.PROJECT_READ))]
UpdateScope = Annotated[ProjectScope, Depends(project_scope(Capability.PROJECT_UPDATE))]
LifecycleScope = Annotated[ProjectScope, Depends(project_scope(Capability.LIFECYCLE_MANAGE))]
RecoveryScope = Annotated[
    ProjectScope,
    Depends(project_scope(Capability.LIFECYCLE_MANAGE, AccessPurpose.RECOVERY)),
]


class CreateProjectRequest(ApiRequest):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class UpdateProjectRequest(ApiRequest):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class EmptyRequest(ApiRequest):
    """``{}``."""


class DeletionRequest(ApiRequest):
    confirmation_name: str = Field(min_length=1, max_length=200)


def _with_etag(response: Response, view: ProjectView) -> ProjectView:
    set_etag(response, view.version)
    return view


# --------------------------------------------------------------------------- T05


@router.get("/projects", response_model=Page[ProjectView], operation_id="listProjects")
async def list_projects(
    container: AppContainer,
    actor: ActiveActor,
    params: Annotated[PageParams, Depends(page_params)],
    lifecycle: Annotated[Literal["Active", "Archived", "All"], Query()] = "Active",
) -> Page[ProjectView]:
    """The caller's Projects. Pending Deletion Projects appear only on ``/projects/recovery``."""
    return await container.projects.list(actor, lifecycle, params.cursor, limit=params.limit)


@router.post("/projects", response_model=ProjectView, status_code=201, operation_id="createProject")
async def create_project(
    body: CreateProjectRequest, container: AppContainer, actor: ActiveActor, idem: Idempotent
) -> Response:
    """**[idem]** ``project.create``: the creator becomes the Administrator."""
    return await idem.run(
        lambda: container.projects.create(actor, body.name, body.description), status_code=201
    )


@router.get(
    "/projects/recovery", response_model=list[ProjectView], operation_id="listRecoveryProjects"
)
async def recovery_projects(container: AppContainer, actor: ActiveActor) -> list[ProjectView]:
    """Pending Deletion Projects the caller administers."""
    return await container.lifecycle.recovery_list(actor)


@router.get("/projects/{project_id}", response_model=ProjectView, operation_id="getProject")
async def get_project(container: AppContainer, scope: ReadScope, response: Response) -> ProjectView:
    return _with_etag(response, await container.projects.get(scope))


@router.patch("/projects/{project_id}", response_model=ProjectView, operation_id="updateProject")
async def update_project(
    body: UpdateProjectRequest,
    container: AppContainer,
    scope: UpdateScope,
    version: IfMatch,
    response: Response,
) -> ProjectView:
    """**[ver]**"""
    patch = ProjectPatch(
        name=body.name,
        description=body.description,
        set_description="description" in body.model_fields_set,
    )
    return _with_etag(response, await container.projects.update(scope, patch, version))


@router.post(
    "/projects/{project_id}/mark-active",
    response_model=ProjectView,
    operation_id="markProjectActive",
)
async def mark_active(
    container: AppContainer, scope: LifecycleScope, response: Response
) -> ProjectView:
    return _with_etag(response, await container.projects.mark_active(scope))


# --------------------------------------------------------------------------- T07


@router.post(
    "/projects/{project_id}/archive", response_model=ProjectView, operation_id="archiveProject"
)
async def archive_project(
    container: AppContainer,
    scope: LifecycleScope,
    version: IfMatch,
    response: Response,
    body: EmptyRequest | None = None,
) -> ProjectView:
    """**[ver]**"""
    del body
    return _with_etag(response, await container.lifecycle.archive(scope, version))


@router.post(
    "/projects/{project_id}/restore", response_model=ProjectView, operation_id="restoreProject"
)
async def restore_project(
    container: AppContainer,
    scope: LifecycleScope,
    version: IfMatch,
    response: Response,
    body: EmptyRequest | None = None,
) -> ProjectView:
    """**[ver]**"""
    del body
    return _with_etag(response, await container.lifecycle.restore(scope, version))


@router.delete(
    "/projects/{project_id}/archival-schedule",
    response_model=ProjectView,
    operation_id="cancelArchivalSchedule",
)
async def cancel_archival_schedule(
    container: AppContainer, scope: LifecycleScope, response: Response
) -> ProjectView:
    """An Administrator cancels the Firm Manager's scheduled archival."""
    return _with_etag(response, await container.lifecycle.cancel_archival_schedule(scope))


@router.post(
    "/projects/{project_id}/deletion",
    response_model=DeletionRequestResult,
    status_code=202,
    operation_id="requestProjectDeletion",
)
async def request_deletion(
    body: DeletionRequest,
    container: AppContainer,
    scope: RecoveryScope,
    version: IfMatch,
    idem: Idempotent,
) -> Response:
    """**[idem][ver]** ``confirmationName`` must equal the current name exactly.

    The scope uses the recovery purpose so a retry after success (the Project is now
    Pending Deletion) still reaches the stored response instead of ``NOT_AVAILABLE``.
    """
    return await idem.run(
        lambda: container.lifecycle.request_deletion(scope, body.confirmation_name, version),
        status_code=202,
    )


@router.delete(
    "/projects/{project_id}/deletion",
    response_model=ProjectView,
    operation_id="cancelProjectDeletion",
)
async def cancel_deletion(
    container: AppContainer, scope: RecoveryScope, version: IfMatch, response: Response
) -> ProjectView:
    """**[ver]** Recovery before the 30-day deadline."""
    return _with_etag(response, await container.lifecycle.cancel_deletion(scope, version))


@router.get(
    "/projects/{project_id}/deletion-report",
    response_model=DeletionReportView,
    operation_id="getDeletionReport",
)
async def deletion_report(
    project_id: str, container: AppContainer, actor: ActiveActor
) -> DeletionReportView:
    """Administrators during recovery; a Firm Manager after completion."""
    return await container.lifecycle.deletion_report(actor, project_id)
