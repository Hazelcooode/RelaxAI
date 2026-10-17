"""Authorization matrix for the Project-scoped routes added by T05-T07 (conventions §8).

Every route x caller x lifecycle → expected gate outcome. "pass" means the request got past
``ProjectAccess`` and the lifecycle guard (anything below 500 other than 401/403/404 and
``PROJECT_READ_ONLY``). A member without the capability gets ``403 CAPABILITY_DENIED``; a
Client Collaborator gets ``404`` for capabilities a client can never hold; non-members, a Firm
Manager without membership, and a nonexistent Project all get the same ``404 NOT_AVAILABLE``.
Archived Projects answer writes with ``409 PROJECT_READ_ONLY``; Pending Deletion Projects
answer ``404`` everywhere except the recovery routes, for Administrators.

Passing requests must not change the fixture: versioned routes send a stale ``If-Match`` (so
they stop at ``409 VERSION_CONFLICT``), and sub-resource routes use a real membership or
invitation id, so a ``404`` there means the gate refused, not that the id was unknown.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import pytest

from app.container import Container
from app.modules.organization_access import Actor, OperatorActor
from app.modules.projects import Capability, Lifecycle, PeoplePurpose, ProjectRole
from app.modules.projects.domain import ARCHIVED_CAPABILITIES, RECOVERY_CAPABILITIES, role_holds
from tests.auth import TestTokenIssuer
from tests.fixtures.synthetic.personas import (
    AVERY,
    FIRM_DOMAIN,
    JORDAN,
    MORGAN,
    RILEY,
    SAM,
    TAYLOR,
    Persona,
)
from tests.projects_support import invite, join, membership_id, new_project, scope_of

MISSING = "prj_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"
STALE = '"999"'
PASS = "pass"
INVITEE = Persona("casey", "Casey Quinn", f"casey.quinn@{FIRM_DOMAIN}", "sub-casey-0099")


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    # After /projects/{id}; ``{membership}`` and ``{invitation}`` are real ids.
    suffix: str
    capability: Capability
    body: dict[str, object] | None = None
    # Calls ``ProjectScope.require_writable`` on a capability Archived Projects keep.
    writes: bool = False
    # Scope minted with ``AccessPurpose.RECOVERY``: Administrators reach Pending Deletion.
    recovery: bool = False
    # Answers only while Pending Deletion; ``404`` otherwise even when the gate passes.
    pending_only: bool = False
    # The service refuses Client Collaborators with ``404`` although the role holds it.
    client_hidden: bool = False

    @property
    def label(self) -> str:
        return f"{self.method} {self.suffix or '/'}"


def _people(
    purpose: PeoplePurpose, capability: Capability, *, client_hidden: bool = False
) -> Route:
    path = f"/people?purpose={purpose.value}"
    return Route("GET", path, capability, client_hidden=client_hidden)


_LM = Capability.LIFECYCLE_MANAGE
_MM = Capability.MEMBERSHIP_MANAGE

ROUTES: list[Route] = [
    # T05
    Route("GET", "", Capability.PROJECT_READ),
    Route("PATCH", "", Capability.PROJECT_UPDATE, {"description": "Updated"}),
    Route("POST", "/mark-active", _LM, writes=True),
    # T07
    Route("POST", "/archive", _LM, {}),
    Route("POST", "/restore", _LM, {}),
    Route("DELETE", "/archival-schedule", _LM, writes=True),
    Route("POST", "/deletion", _LM, {"confirmationName": "Not the name"}, recovery=True),
    Route("DELETE", "/deletion", _LM, recovery=True),
    Route("GET", "/deletion-report", _LM, recovery=True, pending_only=True),
    # T06 memberships
    Route("GET", "/memberships", _MM),
    Route("GET", "/memberships/{membership}/role-change-preview?role=Viewer", _MM),
    Route("PATCH", "/memberships/{membership}", _MM, {"role": "Viewer"}, writes=True),
    Route("DELETE", "/memberships/{membership}", _MM, writes=True),
    # T06 invitations (an existing member's email stops at 409 ALREADY_EXISTS)
    Route("GET", "/invitations", _MM),
    Route("POST", "/invitations", _MM, {"email": MORGAN.email, "role": "Contributor"}, writes=True),
    Route("GET", "/invitations/{invitation}", _MM),
    Route("POST", "/invitations/{invitation}/resend", _MM, writes=True),
    Route("POST", "/invitations/{invitation}/revoke", _MM, writes=True),
    # T06 people pickers
    _people(PeoplePurpose.TASK_ASSIGNEE, Capability.TASK_ADMINISTER),
    _people(PeoplePurpose.TASK_WATCHER, Capability.TASK_ADMINISTER),
    _people(PeoplePurpose.ACTION_ITEM_RESPONSIBLE, Capability.KNOWLEDGE_REVIEW),
    _people(PeoplePurpose.PARTICIPANT_FILTER, Capability.SEARCH, client_hidden=True),
]

CALLERS = {
    "administrator": (AVERY, ProjectRole.ADMINISTRATOR),
    "contributor": (MORGAN, ProjectRole.CONTRIBUTOR),
    "viewer": (RILEY, ProjectRole.VIEWER),
    "client": (JORDAN, ProjectRole.CLIENT_COLLABORATOR),
    "firm_manager": (SAM, None),
    "non_member": (TAYLOR, None),
    "nonexistent": (AVERY, None),
    "anonymous": (None, None),
}

LIFECYCLES = [Lifecycle.ACTIVE, Lifecycle.ARCHIVED, Lifecycle.PENDING_DELETION]

NOT_AVAILABLE = (404, "NOT_AVAILABLE")


def expected(
    caller: str, role: ProjectRole | None, route: Route, lifecycle: Lifecycle
) -> str | tuple[int, str]:
    if caller == "anonymous":
        return (401, "UNAUTHENTICATED")
    if role is None:
        return NOT_AVAILABLE
    if lifecycle is Lifecycle.PENDING_DELETION:
        recoverable = (
            route.recovery
            and role is ProjectRole.ADMINISTRATOR
            and route.capability in RECOVERY_CAPABILITIES
        )
        return PASS if recoverable else NOT_AVAILABLE
    if not role_holds(role, route.capability):
        if role is ProjectRole.CLIENT_COLLABORATOR:
            return NOT_AVAILABLE
        return (403, "CAPABILITY_DENIED")
    if route.client_hidden and role is ProjectRole.CLIENT_COLLABORATOR:
        return NOT_AVAILABLE
    if lifecycle is Lifecycle.ARCHIVED and (
        route.writes or route.capability not in ARCHIVED_CAPABILITIES
    ):
        return (409, "PROJECT_READ_ONLY")
    if route.pending_only:
        return NOT_AVAILABLE
    return PASS


@dataclass(frozen=True, slots=True)
class Fixture:
    project_id: str
    membership_id: str
    invitation_id: str


async def _fixture(
    container: Container, activate: Callable[[Persona], Awaitable[Actor]], lifecycle: Lifecycle
) -> Fixture:
    """Atlas with one member per role (joined through the real invitation flow), a pending
    invitation, a Firm Manager outside it, and a non-member — then moved to ``lifecycle``."""
    avery = await activate(AVERY)
    project = await new_project(container, avery)
    pid = project.project_id
    morgan = await join(container, avery, pid, MORGAN, ProjectRole.CONTRIBUTOR)
    await join(container, avery, pid, RILEY, ProjectRole.VIEWER)
    await join(container, avery, pid, JORDAN, ProjectRole.CLIENT_COLLABORATOR)
    invitation, _ = await invite(container, avery, pid, INVITEE, ProjectRole.VIEWER)
    member = await membership_id(container, avery, pid, morgan)
    sam = await activate(SAM)
    await container.organization.appoint_first_firm_manager(OperatorActor.cli(), sam.user_id or "")
    await activate(TAYLOR)

    scope = await scope_of(container, avery, pid, Capability.LIFECYCLE_MANAGE)
    current = await container.projects.get(scope)
    if lifecycle is Lifecycle.ARCHIVED:
        await container.lifecycle.archive(scope, current.version)
    elif lifecycle is Lifecycle.PENDING_DELETION:
        await container.lifecycle.request_deletion(scope, current.name, current.version)
    return Fixture(pid, member, invitation.invitation_id)


def _outcome_ok(response: httpx.Response, want: str | tuple[int, str]) -> bool:
    body = response.json() if response.content else None
    # Successful list routes (people pickers) return a JSON array, not an envelope.
    code = body.get("error", {}).get("code") if isinstance(body, dict) else None
    if isinstance(want, str):
        return (
            response.status_code < 500
            and response.status_code not in (401, 403, 404)
            and code != "PROJECT_READ_ONLY"
        )
    status, wanted_code = want
    return response.status_code == status and code == wanted_code


@pytest.mark.parametrize("caller", list(CALLERS))
@pytest.mark.parametrize("lifecycle", LIFECYCLES, ids=str)
async def test_project_route_matrix(
    lifecycle: Lifecycle,
    caller: str,
    container: Container,
    api_client: httpx.AsyncClient,
    token_issuer: TestTokenIssuer,
    activate: Callable[[Persona], Awaitable[Actor]],
) -> None:
    fixture = await _fixture(container, activate, lifecycle)
    persona, role = CALLERS[caller]
    project_id = MISSING if caller == "nonexistent" else fixture.project_id
    auth = token_issuer.headers_for(persona) if persona else {}
    failures = []
    for route in ROUTES:
        suffix = route.suffix.format(
            membership=fixture.membership_id, invitation=fixture.invitation_id
        )
        headers = {"If-Match": STALE, "Idempotency-Key": str(uuid.uuid4()), **auth}
        response = await api_client.request(
            route.method, f"/api/v1/projects/{project_id}{suffix}", headers=headers, json=route.body
        )
        want = expected(caller, role, route, lifecycle)
        if not _outcome_ok(response, want):
            failures.append(
                f"{route.label}: want {want}, got {response.status_code} {response.text[:120]}"
            )
    if failures:
        pytest.fail(f"{lifecycle} / {caller}:\n" + "\n".join(failures))


@pytest.mark.parametrize("caller", [c for c in CALLERS if c != "nonexistent"])
@pytest.mark.parametrize("lifecycle", LIFECYCLES, ids=str)
async def test_recovery_list_matrix(
    lifecycle: Lifecycle,
    caller: str,
    container: Container,
    api_client: httpx.AsyncClient,
    token_issuer: TestTokenIssuer,
    activate: Callable[[Persona], Awaitable[Actor]],
) -> None:
    """``GET /projects/recovery`` lists a Project only while Pending Deletion, and only to its
    Administrators; everyone else (members, Firm Manager, non-members) gets an empty list."""
    fixture = await _fixture(container, activate, lifecycle)
    persona, _ = CALLERS[caller]
    headers = token_issuer.headers_for(persona) if persona else {}
    response = await api_client.get("/api/v1/projects/recovery", headers=headers)
    if persona is None:
        assert response.status_code == 401
        return
    assert response.status_code == 200
    listed = [p["projectId"] for p in response.json()]
    should_list = caller == "administrator" and lifecycle is Lifecycle.PENDING_DELETION
    assert listed == ([fixture.project_id] if should_list else [])


@pytest.mark.parametrize(("persona", "status"), [(AVERY, 200), (JORDAN, 200), (None, 401)])
async def test_list_and_me_gate(
    persona: Persona | None,
    status: int,
    container: Container,
    api_client: httpx.AsyncClient,
    token_issuer: TestTokenIssuer,
    activate: Callable[[Persona], Awaitable[Actor]],
) -> None:
    avery = await activate(AVERY)
    project = await new_project(container, avery)
    await join(container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR)
    headers = token_issuer.headers_for(persona) if persona else {}
    for path in ("/api/v1/projects", "/api/v1/projects/recovery"):
        assert (await api_client.get(path, headers=headers)).status_code == status
