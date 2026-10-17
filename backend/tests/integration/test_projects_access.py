"""Projects and ProjectAccess (T05): creation, non-enumeration, capability checks, scope
normalization, Archived read-only, Latest Project Activity, ``/me``, authorization version."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
import pytest

from app.container import Container
from app.modules.governance import (
    AuditAction,
    AuditFilters,
    Destination,
    ProjectAdministratorViewer,
)
from app.modules.organization_access import Actor, OperatorActor
from app.modules.projects import (
    AccessPurpose,
    ActivityKind,
    Capability,
    Lifecycle,
    ProjectAccessAdapter,
    ProjectRole,
    ProjectView,
    Visibility,
)
from app.platform.clock import FrozenClock
from app.platform.errors import (
    CapabilityDenied,
    NotAvailable,
    ProjectReadOnly,
    ScopeInvalid,
)
from tests.auth import TestTokenIssuer
from tests.fixtures.synthetic.personas import AVERY, JORDAN, MORGAN, RILEY, Persona
from tests.projects_support import actor_for, join, new_project, scope_of

Activate = Callable[[Persona], Awaitable[Actor]]
MISSING = "prj_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"


@pytest.fixture
async def avery(activate: Activate) -> Actor:
    return await activate(AVERY)


@pytest.fixture
async def morgan(activate: Activate) -> Actor:
    return await activate(MORGAN)


@pytest.fixture
async def project(container: Container, avery: Actor) -> ProjectView:
    return await new_project(container, avery)


async def _audit_actions(container: Container, project_id: str) -> list[str]:
    page = await container.audit.query(
        ProjectAdministratorViewer(project_id), AuditFilters(limit=100), None
    )
    return [e.action for e in page.items]


# --------------------------------------------------------------------------- creation


async def test_creator_becomes_the_only_administrator(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    assert project.role is ProjectRole.ADMINISTRATOR
    assert project.lifecycle is Lifecycle.ACTIVE
    assert project.code == "ATL-001"
    assert project.version == 1
    assert Capability.MEMBERSHIP_MANAGE in project.capabilities
    scope = await scope_of(container, avery, project.project_id, Capability.MEMBERSHIP_MANAGE)
    members = await container.membership.list(scope, None)
    assert [(m.user_id, m.role) for m in members.items] == [
        (avery.user_id, ProjectRole.ADMINISTRATOR)
    ]
    actions = await _audit_actions(container, project.project_id)
    assert AuditAction.PROJECT_CREATED.value in actions
    assert AuditAction.MEMBERSHIP_ADDED.value in actions


async def test_codes_are_unique_per_firm(container: Container, avery: Actor) -> None:
    first = await new_project(container, avery, "Atlas Expansion")
    second = await new_project(container, avery, "Atlas Renewal")
    third = await new_project(container, avery, "Harbor Analytics")
    assert [first.code, second.code, third.code] == ["ATL-001", "ATL-002", "HAR-003"]


async def test_new_project_is_invisible_to_non_members(
    container: Container, morgan: Actor, project: ProjectView
) -> None:
    with pytest.raises(NotAvailable):
        await scope_of(container, morgan, project.project_id)


async def test_client_collaborator_cannot_create(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    jordan = await join(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    with pytest.raises(CapabilityDenied):
        await container.projects.create(jordan, "Client Project", None)


# --------------------------------------------------------------------------- require


async def test_member_without_capability_is_denied_and_audited(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    riley = await join(container, avery, project.project_id, RILEY, ProjectRole.VIEWER)
    with pytest.raises(CapabilityDenied):
        await scope_of(container, riley, project.project_id, Capability.DOCUMENT_UPLOAD)
    page = await container.audit.query(
        ProjectAdministratorViewer(project.project_id),
        AuditFilters(actions=frozenset({AuditAction.PERMISSION_DENIED})),
        None,
    )
    [event] = page.items
    assert event.facts == {"capability": "document.upload", "errorCode": "CAPABILITY_DENIED"}
    assert event.target.id == project.project_id


@pytest.mark.parametrize(
    "capability",
    [
        Capability.NETWORK_READ,
        Capability.WORKLOAD_READ,
        Capability.REVIEW_PSEUDONYMIZATION,
        Capability.MEMBERSHIP_MANAGE,
    ],
)
async def test_client_collaborator_gets_not_available_not_forbidden(
    container: Container, avery: Actor, project: ProjectView, capability: Capability
) -> None:
    jordan = await join(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    with pytest.raises(NotAvailable):
        await scope_of(container, jordan, project.project_id, capability)


async def test_client_collaborator_scope_is_client_visible_only(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    jordan = await join(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    scope = await scope_of(container, jordan, project.project_id, Capability.KNOWLEDGE_READ)
    assert scope.visibility == frozenset({Visibility.CLIENT_VISIBLE})
    own = await scope_of(container, avery, project.project_id, Capability.KNOWLEDGE_READ)
    assert own.visibility == frozenset(Visibility)


async def test_archived_project_is_read_only(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    lifecycle_scope = await scope_of(
        container, avery, project.project_id, Capability.LIFECYCLE_MANAGE
    )
    await container.lifecycle.archive(lifecycle_scope, project.version)
    scope = await scope_of(container, avery, project.project_id, Capability.KNOWLEDGE_READ)
    assert scope.lifecycle is Lifecycle.ARCHIVED
    for capability in (
        Capability.DOCUMENT_UPLOAD,
        Capability.PROJECT_UPDATE,
        Capability.TASK_ADMINISTER,
        Capability.KNOWLEDGE_REVIEW,
        Capability.SOURCE_CONNECT,
    ):
        with pytest.raises(ProjectReadOnly):
            await scope_of(container, avery, project.project_id, capability)


async def test_pending_deletion_is_not_available_except_for_recovery(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    scope = await scope_of(container, avery, project.project_id, Capability.LIFECYCLE_MANAGE)
    await container.lifecycle.request_deletion(scope, project.name, project.version)
    with pytest.raises(NotAvailable):
        await scope_of(container, avery, project.project_id)
    recovery = await scope_of(
        container, avery, project.project_id, Capability.LIFECYCLE_MANAGE, AccessPurpose.RECOVERY
    )
    assert recovery.lifecycle is Lifecycle.PENDING_DELETION
    with pytest.raises(NotAvailable):
        await scope_of(
            container, avery, project.project_id, Capability.KNOWLEDGE_READ, AccessPurpose.RECOVERY
        )


async def test_firm_manager_status_never_grants_access(
    container: Container, morgan: Actor, project: ProjectView
) -> None:
    await container.organization.appoint_first_firm_manager(
        OperatorActor.cli(), morgan.user_id or ""
    )
    manager = await actor_for(container, MORGAN)
    assert manager.firm_manager
    with pytest.raises(NotAvailable):
        await scope_of(container, manager, project.project_id)


# --------------------------------------------------------------------------- require_many


async def test_require_many_normalizes_and_fails_whole(
    container: Container, avery: Actor, morgan: Actor
) -> None:
    a = await new_project(container, avery, "Atlas Expansion")
    b = await new_project(container, avery, "Northstar Transformation")
    other = await new_project(container, morgan, "Harbor Analytics")

    with pytest.raises(ScopeInvalid):
        await container.project_access.require_many(avery, [], Capability.SEARCH)
    with pytest.raises(ScopeInvalid):
        await container.project_access.require_many(avery, ["not-an-id"], Capability.SEARCH)

    forward = await container.project_access.require_many(
        avery, [a.project_id, b.project_id, a.project_id], Capability.SEARCH
    )
    backward = await container.project_access.require_many(
        avery, [f" {b.project_id} ", a.project_id], Capability.SEARCH
    )
    assert forward.project_ids == tuple(sorted([a.project_id, b.project_id]))
    assert forward.scope_key == backward.scope_key

    with pytest.raises(NotAvailable):
        await container.project_access.require_many(
            avery, [a.project_id, other.project_id], Capability.SEARCH
        )
    with pytest.raises(NotAvailable):
        await container.project_access.require_many(
            avery, [a.project_id, MISSING], Capability.SEARCH
        )

    lifecycle = await scope_of(container, avery, b.project_id, Capability.LIFECYCLE_MANAGE)
    await container.lifecycle.archive(lifecycle, b.version)
    with pytest.raises(NotAvailable):
        await container.project_access.require_many(
            avery, [a.project_id, b.project_id], Capability.SEARCH
        )


async def test_authorized_projects_filters_by_capability_and_lifecycle(
    container: Container, avery: Actor
) -> None:
    a = await new_project(container, avery, "Atlas Expansion")
    b = await new_project(container, avery, "Northstar Transformation")
    riley = await join(container, avery, a.project_id, RILEY, ProjectRole.VIEWER)
    await join(container, avery, b.project_id, RILEY, ProjectRole.CONTRIBUTOR)
    uploads = await container.project_access.authorized_projects(riley, Capability.DOCUMENT_UPLOAD)
    assert uploads.project_ids == (b.project_id,)
    reads = await container.project_access.authorized_projects(riley, Capability.PROJECT_READ)
    assert set(reads.project_ids) == {a.project_id, b.project_id}


# --------------------------------------------------------------------------- activity


async def test_latest_activity_moves_only_for_listed_events(
    container: Container, frozen_clock: FrozenClock, avery: Actor, project: ProjectView
) -> None:
    created_activity = project.latest_activity_at
    assert created_activity is not None  # creating the Administrator membership counts

    frozen_clock.advance(timedelta(days=3))
    scope = await scope_of(container, avery, project.project_id, Capability.SEARCH)
    await container.projects.get(scope)
    await container.projects.list(avery, "All", None)
    await container.project_access.summary(avery)
    await container.project_access.require_many(avery, [project.project_id], Capability.CHAT)
    assert (await container.projects.get(scope)).latest_activity_at == created_activity

    async with container.uow() as tx:
        await container.activity.record(tx, scope, ActivityKind.DOCUMENT_ACCEPTED, "doc_x")
    assert (await container.projects.get(scope)).latest_activity_at == frozen_clock.now()

    frozen_clock.advance(timedelta(days=1))
    await join(container, avery, project.project_id, RILEY, ProjectRole.VIEWER)
    assert (await container.projects.get(scope)).latest_activity_at == frozen_clock.now()


async def test_mark_active_records_administrator_signal(
    container: Container, frozen_clock: FrozenClock, avery: Actor, project: ProjectView
) -> None:
    frozen_clock.advance(timedelta(days=40))
    scope = await scope_of(container, avery, project.project_id, Capability.LIFECYCLE_MANAGE)
    view = await container.projects.mark_active(scope)
    assert view.latest_activity_at == frozen_clock.now()


# --------------------------------------------------------------------------- /me


async def test_me_lists_projects_and_landing(
    container: Container, api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
    avery: Actor, project: ProjectView,
) -> None:  # fmt: skip
    me = (await api_client.get("/api/v1/me", headers=token_issuer.headers_for(AVERY))).json()
    assert me["landing"] == {"kind": "Home"}
    [entry] = me["projects"]
    assert entry["project"] == {"projectId": project.project_id, "name": project.name}
    assert entry["role"] == "Administrator"
    assert entry["lifecycle"] == "Active"
    assert "membership.manage" in entry["capabilities"]

    await join(container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR)
    client = (await api_client.get("/api/v1/me", headers=token_issuer.headers_for(JORDAN))).json()
    assert client["landing"] == {"kind": "Project", "projectId": project.project_id}
    assert "network.read" not in client["projects"][0]["capabilities"]


async def test_authorization_version_changes_on_role_and_lifecycle(
    container: Container, api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
    avery: Actor, project: ProjectView,
) -> None:  # fmt: skip
    headers = token_issuer.headers_for(AVERY)

    async def version() -> str:
        body = (await api_client.get("/api/v1/me", headers=headers)).json()
        return str(body["authorizationVersion"])

    riley = await join(container, avery, project.project_id, RILEY, ProjectRole.VIEWER)
    before = await version()
    scope = await scope_of(container, avery, project.project_id, Capability.MEMBERSHIP_MANAGE)
    members = await container.membership.list(scope, None)
    riley_membership = next(m for m in members.items if m.user_id == riley.user_id)
    await container.membership.change_role(
        scope, riley_membership.membership_id, ProjectRole.CONTRIBUTOR, riley_membership.version
    )
    after_role = await version()
    assert after_role != before

    current = await container.projects.get(scope)
    lifecycle = await scope_of(container, avery, project.project_id, Capability.LIFECYCLE_MANAGE)
    await container.lifecycle.archive(lifecycle, current.version)
    assert await version() != after_role


# --------------------------------------------------------------------------- HTTP


async def test_non_enumeration_is_byte_identical(
    container: Container, api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
    avery: Actor, morgan: Actor,
) -> None:  # fmt: skip
    mine = await new_project(container, avery, "Atlas Expansion")
    pending = await new_project(container, morgan, "Northstar Transformation")
    scope = await scope_of(container, morgan, pending.project_id, Capability.LIFECYCLE_MANAGE)
    await container.lifecycle.request_deletion(scope, pending.name, pending.version)

    def shape(response: httpx.Response) -> tuple[int, dict[str, object]]:
        error = dict(response.json()["error"])
        error.pop("correlationId")
        return response.status_code, error

    morgan_headers = token_issuer.headers_for(MORGAN)
    non_member = await api_client.get(f"/api/v1/projects/{mine.project_id}", headers=morgan_headers)
    missing = await api_client.get(f"/api/v1/projects/{MISSING}", headers=morgan_headers)
    pending_deletion = await api_client.get(
        f"/api/v1/projects/{pending.project_id}", headers=morgan_headers
    )
    assert shape(non_member) == shape(missing) == shape(pending_deletion)
    assert shape(missing)[0] == 404
    assert shape(missing)[1]["code"] == "NOT_AVAILABLE"


async def test_create_get_patch_over_http(
    api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer, avery: Actor
) -> None:
    headers = {
        **token_issuer.headers_for(AVERY),
        "Idempotency-Key": "0b7d1f8e-3a52-4b8f-8c6b-6f9f2c4c1e01",
    }
    body = {"name": "Atlas Expansion", "description": "Market entry"}
    created = await api_client.post("/api/v1/projects", headers=headers, json=body)
    assert created.status_code == 201
    replay = await api_client.post("/api/v1/projects", headers=headers, json=body)
    assert replay.json() == created.json()
    project_id = created.json()["projectId"]

    auth = token_issuer.headers_for(AVERY)
    got = await api_client.get(f"/api/v1/projects/{project_id}", headers=auth)
    assert got.headers["ETag"] == '"1"'
    assert got.json()["archivalSchedule"] is None
    assert got.json()["deletion"] is None

    missing_version = await api_client.patch(
        f"/api/v1/projects/{project_id}", headers=auth, json={"name": "Atlas"}
    )
    assert missing_version.status_code == 428

    patched = await api_client.patch(
        f"/api/v1/projects/{project_id}",
        headers={**auth, "If-Match": '"1"'},
        json={"description": None},
    )
    assert patched.status_code == 200
    assert patched.json()["description"] is None
    assert patched.json()["name"] == "Atlas Expansion"
    assert patched.headers["ETag"] == '"2"'

    stale = await api_client.patch(
        f"/api/v1/projects/{project_id}",
        headers={**auth, "If-Match": '"1"'},
        json={"name": "Atlas Prime"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    assert stale.json()["error"]["current"]["version"] == 2


async def test_list_filters_by_lifecycle_and_paginates(
    container: Container, api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
    avery: Actor,
) -> None:  # fmt: skip
    names = ["Atlas Expansion", "Harbor Analytics", "Northstar Transformation"]
    views = [await new_project(container, avery, name) for name in names]
    scope = await scope_of(container, avery, views[1].project_id, Capability.LIFECYCLE_MANAGE)
    await container.lifecycle.archive(scope, views[1].version)

    auth = token_issuer.headers_for(AVERY)
    active = (await api_client.get("/api/v1/projects", headers=auth)).json()
    assert [p["name"] for p in active["items"]] == ["Atlas Expansion", "Northstar Transformation"]
    archived = (await api_client.get("/api/v1/projects?lifecycle=Archived", headers=auth)).json()
    assert [p["name"] for p in archived["items"]] == ["Harbor Analytics"]

    first = (await api_client.get("/api/v1/projects?lifecycle=All&limit=2", headers=auth)).json()
    assert [p["name"] for p in first["items"]] == names[:2]
    second = (
        await api_client.get(
            f"/api/v1/projects?lifecycle=All&limit=2&cursor={first['nextCursor']}", headers=auth
        )
    ).json()
    assert [p["name"] for p in second["items"]] == names[2:]
    assert second["nextCursor"] is None


async def test_operations_reauthorize_through_project_access(
    container: Container, avery: Actor, morgan: Actor, project: ProjectView
) -> None:
    adapter = ProjectAccessAdapter(container.project_access)
    assert await adapter.holds(avery, "lifecycle.manage", project.project_id)
    assert not await adapter.holds(morgan, "project.read", project.project_id)
    assert not await adapter.holds(avery, "not.a.capability", project.project_id)
    inside = Destination(kind="Project", project_id=project.project_id, path="/x")
    assert await adapter.destination_authorized(avery, inside)
    assert not await adapter.destination_authorized(morgan, inside)
    label = await adapter.project_label(avery, project.project_id)
    assert label is not None
    assert label.name == project.name
    assert await adapter.project_label(morgan, project.project_id) is None
