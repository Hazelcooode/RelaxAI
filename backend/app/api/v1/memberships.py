"""Memberships, invitations, and people pickers (contract §4.3, §6.3-6.5; T06)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field

from app.api.v1.conventions import Idempotent, IdempotentAnyActor
from app.api.v1.deps import ActiveActor, AppContainer, CurrentActor, Identity
from app.api.v1.me import Me, me_for
from app.api.v1.projects import project_scope
from app.api.v1.schemas.base import ApiModel, ApiRequest
from app.modules.projects import (
    Capability,
    InvitationContext,
    InvitationState,
    InvitationView,
    MembershipView,
    PeoplePurpose,
    PersonRef,
    ProjectRole,
    ProjectScope,
    RoleChangePreview,
    people_capability,
)
from app.platform.http.concurrency import IfMatch, set_etag
from app.platform.http.pagination import Page, PageParams, page_params

router = APIRouter(tags=["memberships"])

ManageScope = Annotated[ProjectScope, Depends(project_scope(Capability.MEMBERSHIP_MANAGE))]
TeamRole = Literal["Administrator", "Contributor", "Viewer"]


class RoleChangeRequest(ApiRequest):
    """Team Member roles only: a Client Collaborator's role never changes."""

    role: TeamRole


class InvitationRequest(ApiRequest):
    email: str = Field(min_length=3, max_length=320)
    role: ProjectRole


class CredentialRequest(ApiRequest):
    credential: str = Field(min_length=1, max_length=128)


class AcceptanceView(ApiModel):
    project_id: str
    role: ProjectRole
    me: Me


# --------------------------------------------------------------------------- memberships


@router.get(
    "/projects/{project_id}/memberships",
    response_model=Page[MembershipView],
    operation_id="listMemberships",
)
async def list_memberships(
    container: AppContainer,
    scope: ManageScope,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[MembershipView]:
    return await container.membership.list(scope, params.cursor, limit=params.limit)


@router.get(
    "/projects/{project_id}/memberships/{membership_id}/role-change-preview",
    response_model=RoleChangePreview,
    operation_id="previewRoleChange",
)
async def role_change_preview(
    membership_id: str,
    container: AppContainer,
    scope: ManageScope,
    role: Annotated[TeamRole, Query()],
) -> RoleChangePreview:
    return await container.membership.role_change_preview(scope, membership_id, ProjectRole(role))


@router.patch(
    "/projects/{project_id}/memberships/{membership_id}",
    response_model=MembershipView,
    operation_id="changeMembershipRole",
)
async def change_role(
    membership_id: str,
    body: RoleChangeRequest,
    container: AppContainer,
    scope: ManageScope,
    version: IfMatch,
    response: Response,
) -> MembershipView:
    """**[ver]** ``409 FINAL_ADMINISTRATOR`` when it would leave no Administrator."""
    view = await container.membership.change_role(
        scope, membership_id, ProjectRole(body.role), version
    )
    set_etag(response, view.version)
    return view


@router.delete(
    "/projects/{project_id}/memberships/{membership_id}",
    status_code=204,
    operation_id="removeMembership",
)
async def remove_membership(
    membership_id: str, container: AppContainer, scope: ManageScope, version: IfMatch
) -> Response:
    """**[ver]** ``409 FINAL_ADMINISTRATOR`` for the last Administrator."""
    await container.membership.remove(scope, membership_id, version)
    return Response(status_code=204)


@router.get(
    "/projects/{project_id}/people", response_model=list[PersonRef], operation_id="listPeople"
)
async def people(
    project_id: str,
    container: AppContainer,
    actor: ActiveActor,
    purpose: Annotated[PeoplePurpose, Query()],
) -> list[PersonRef]:
    scope = await container.project_access.require(actor, project_id, people_capability(purpose))
    return await container.membership.people(scope, purpose)


# --------------------------------------------------------------------------- invitations


@router.get(
    "/projects/{project_id}/invitations",
    response_model=Page[InvitationView],
    operation_id="listInvitations",
)
async def list_invitations(
    container: AppContainer,
    scope: ManageScope,
    params: Annotated[PageParams, Depends(page_params)],
    state: Annotated[InvitationState | None, Query()] = None,
) -> Page[InvitationView]:
    return await container.invitations.list(scope, state, params.cursor, limit=params.limit)


@router.post(
    "/projects/{project_id}/invitations",
    response_model=InvitationView,
    status_code=201,
    operation_id="createInvitation",
)
async def create_invitation(
    body: InvitationRequest, container: AppContainer, scope: ManageScope, idem: Idempotent
) -> Response:
    """**[idem]** ``409 ALREADY_EXISTS`` for a pending invitation or an existing member."""
    return await idem.run(
        lambda: container.invitations.issue(scope, body.email, body.role), status_code=201
    )


@router.get(
    "/projects/{project_id}/invitations/{invitation_id}",
    response_model=InvitationView,
    operation_id="getInvitation",
)
async def get_invitation(
    invitation_id: str, container: AppContainer, scope: ManageScope, response: Response
) -> InvitationView:
    view = await container.invitations.get(scope, invitation_id)
    set_etag(response, view.version)
    return view


@router.post(
    "/projects/{project_id}/invitations/{invitation_id}/resend",
    response_model=InvitationView,
    status_code=201,
    operation_id="resendInvitation",
)
async def resend_invitation(
    invitation_id: str,
    container: AppContainer,
    scope: ManageScope,
    version: IfMatch,
    idem: Idempotent,
) -> Response:
    """**[idem][ver]** A new invitation; the previous credential stops working."""
    return await idem.run(
        lambda: container.invitations.resend(scope, invitation_id, version), status_code=201
    )


@router.post(
    "/projects/{project_id}/invitations/{invitation_id}/revoke",
    response_model=InvitationView,
    operation_id="revokeInvitation",
)
async def revoke_invitation(
    invitation_id: str,
    container: AppContainer,
    scope: ManageScope,
    version: IfMatch,
    response: Response,
) -> InvitationView:
    """**[ver]**"""
    view = await container.invitations.revoke(scope, invitation_id, version)
    set_etag(response, view.version)
    return view


@router.post(
    "/invitations/inspect", response_model=InvitationContext, operation_id="inspectInvitation"
)
async def inspect_invitation(
    body: CredentialRequest, request: Request, container: AppContainer, actor: CurrentActor
) -> InvitationContext:
    """Open to not-yet-activated actors. Rate limited per client address and per actor."""
    client = request.client.host if request.client is not None else "unknown"
    return await container.invitations.inspect(body.credential, actor, client_key=client)


@router.post("/invitations/accept", response_model=AcceptanceView, operation_id="acceptInvitation")
async def accept_invitation(
    body: CredentialRequest,
    container: AppContainer,
    actor: CurrentActor,
    identity: Identity,
    idem: IdempotentAnyActor,
) -> Response:
    """**[idem]** Creates the Client Collaborator account when needed; re-accepting with the
    same account returns the same result."""

    async def command() -> AcceptanceView:
        result = await container.invitations.accept(body.credential, actor, identity)
        return AcceptanceView(
            project_id=result.project_id,
            role=result.role,
            me=await me_for(container, result.actor),
        )

    return await idem.run(command)
