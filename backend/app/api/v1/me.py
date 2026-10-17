"""``GET /me`` and ``POST /activation/employee`` (contract §4.1, §4.2)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import AwareDatetime, Field

from app.api.v1.deps import AppContainer, CurrentActor, Identity
from app.api.v1.schemas.base import ApiModel, ApiRequest
from app.container import Container
from app.modules.governance import ProjectLabel
from app.modules.organization_access import Actor
from app.modules.projects import AccessSummary, Capability, LandingKind, Lifecycle, ProjectRole
from app.modules.projects import Landing as AccessLanding

router = APIRouter(tags=["me"])

ConnectorState = Literal["Disconnected", "Connected", "Paused", "Error"]
GlobalCapabilityValue = Literal["project.create", "governance.enter", "operator.enter"]


class FirmRolesView(ApiModel):
    firm_manager: bool
    platform_operator: bool


class MeProject(ApiModel):
    project: ProjectLabel
    role: ProjectRole
    lifecycle: Literal["Active", "Archived"]
    capabilities: list[Capability]
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


def _landing(landing: AccessLanding) -> Landing:
    if landing.kind is LandingKind.PROJECT and landing.project_id is not None:
        return ProjectLanding(project_id=landing.project_id)
    if landing.kind is LandingKind.HOME:
        return HomeLanding()
    if landing.kind is LandingKind.PROJECT_CHOOSER:
        return ProjectChooserLanding()
    if landing.kind is LandingKind.ZERO_STATE:
        return ZeroStateLanding(can_create_project=landing.can_create_project)
    return ActivationLanding()


def me_view(actor: Actor, summary: AccessSummary) -> Me:
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
        global_capabilities=[c.value for c in summary.global_capabilities],
        projects=[
            MeProject(
                project=ProjectLabel(project_id=p.project_id, name=p.name),
                role=p.role,
                lifecycle="Archived" if p.lifecycle is Lifecycle.ARCHIVED else "Active",
                capabilities=p.capabilities,
                latest_activity_at=p.latest_activity_at,
            )
            for p in summary.projects
        ],
        landing=_landing(summary.landing),
        authorization_version=summary.authorization_version,
        connector=ConnectorSummary(state="Disconnected"),
    )


async def me_for(container: Container, actor: Actor) -> Me:
    """``Me`` for ``actor``: ``ProjectAccess.summary`` supplies memberships, landing, and the
    authorization version (T05)."""
    return me_view(actor, await container.project_access.summary(actor))


@router.get("/me", response_model=Me, operation_id="getMe")
async def get_me(container: AppContainer, actor: CurrentActor) -> Me:
    return await me_for(container, actor)


class EmployeeActivationRequest(ApiRequest):
    """Empty: the identity comes from the verified token."""


@router.post("/activation/employee", response_model=Me, operation_id="activateEmployee")
async def activate_employee(
    container: AppContainer, identity: Identity, body: EmployeeActivationRequest | None = None
) -> Me:
    del body
    return await me_for(container, await container.organization.activate_employee(identity))
