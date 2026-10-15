"""``GET /me`` and ``POST /activation/employee`` (contract §4.1, §4.2)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import AwareDatetime, Field

from app.api.v1.deps import AppContainer, CurrentActor, Identity
from app.api.v1.schemas.base import ApiModel, ApiRequest
from app.modules.organization_access import (
    Actor,
    authorization_version,
    global_capabilities,
)

router = APIRouter(tags=["me"])

ProjectRole = Literal["Administrator", "Contributor", "Viewer", "ClientCollaborator"]
ConnectorState = Literal["Disconnected", "Connected", "Paused", "Error"]
GlobalCapabilityValue = Literal["project.create", "governance.enter", "operator.enter"]


class FirmRolesView(ApiModel):
    firm_manager: bool
    platform_operator: bool


class ProjectLabel(ApiModel):
    project_id: str
    name: str


class MeProject(ApiModel):
    project: ProjectLabel
    role: ProjectRole
    lifecycle: Literal["Active", "Archived"]
    capabilities: list[str]
    latest_activity_at: AwareDatetime | None


class ActivationLanding(ApiModel):
    kind: Literal["Activation"] = "Activation"


class HomeLanding(ApiModel):
    kind: Literal["Home"] = "Home"


class ProjectLanding(ApiModel):
    kind: Literal["Project"] = "Project"
    project_id: str


class ProjectChooserLanding(ApiModel):
    kind: Literal["ProjectChooser"] = "ProjectChooser"


class ZeroStateLanding(ApiModel):
    kind: Literal["ZeroState"] = "ZeroState"
    can_create_project: bool


Landing = Annotated[
    ActivationLanding | HomeLanding | ProjectLanding | ProjectChooserLanding | ZeroStateLanding,
    Field(discriminator="kind"),
]


class ConnectorSummary(ApiModel):
    state: ConnectorState


class Me(ApiModel):
    """Contract §4.1. T05 completes ``projects`` and ``landing``; T20 ``connector``."""

    user_id: str | None
    activation: Literal["Active", "NotActivated", "Disabled"]
    person_category: Literal["TeamMember", "ClientCollaborator"] | None
    email: str
    display_name: str
    firm_id: str
    firm_roles: FirmRolesView
    global_capabilities: list[GlobalCapabilityValue]
    projects: list[MeProject]
    landing: Landing
    authorization_version: str
    connector: ConnectorSummary


def me_view(actor: Actor) -> Me:
    landing: Landing = (
        ZeroStateLanding(can_create_project=actor.is_team_member)
        if actor.is_active
        else ActivationLanding()
    )
    return Me(
        user_id=actor.user_id,
        activation=actor.activation.value,
        person_category=actor.person_category.value if actor.person_category else None,
        email=actor.email or "",
        display_name=actor.display_name,
        firm_id=actor.firm_id,
        firm_roles=FirmRolesView(
            firm_manager=actor.firm_manager, platform_operator=actor.platform_operator
        ),
        global_capabilities=[c.value for c in global_capabilities(actor)],
        projects=[],
        landing=landing,
        authorization_version=authorization_version(actor),
        connector=ConnectorSummary(state="Disconnected"),
    )


@router.get("/me", response_model=Me, operation_id="getMe")
async def get_me(actor: CurrentActor) -> Me:
    return me_view(actor)


class EmployeeActivationRequest(ApiRequest):
    """Empty: the identity comes from the verified token."""


@router.post("/activation/employee", response_model=Me, operation_id="activateEmployee")
async def activate_employee(
    container: AppContainer, identity: Identity, body: EmployeeActivationRequest | None = None
) -> Me:
    del body
    return me_view(await container.organization.activate_employee(identity))
