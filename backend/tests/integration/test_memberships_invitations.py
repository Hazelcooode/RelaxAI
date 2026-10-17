"""Membership and invitations (T06): RA-02 invitation lifecycle, expiry, acceptance rules,
final-administrator invariant, role-change preview, removal, people pickers, audit."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
import pytest

from app.container import Container
from app.modules.governance import AuditAction, AuditFilters, ProjectAdministratorViewer
from app.modules.organization_access import Actor
from app.modules.projects import (
    Capability,
    InvitationState,
    PeoplePurpose,
    ProjectRole,
    ProjectScope,
    ProjectView,
)
from app.platform.clock import FrozenClock
from app.platform.errors import (
    AlreadyExists,
    AppError,
    ErrorCode,
    InvalidTransition,
    NotAvailable,
    ProjectReadOnly,
    ValidationFailed,
)
from tests.auth import TestTokenIssuer
from tests.conftest import identity_of
from tests.fixtures.synthetic.personas import AVERY, JORDAN, MORGAN, RILEY, Persona
from tests.projects_support import (
    actor_for,
    invite,
    join,
    last_credential,
    membership_id,
    new_project,
    outbox,
    scope_of,
)

Activate = Callable[[Persona], Awaitable[Actor]]


@pytest.fixture
async def avery(activate: Activate) -> Actor:
    return await activate(AVERY)


@pytest.fixture
async def project(container: Container, avery: Actor) -> ProjectView:
    return await new_project(container, avery)


def _failure(caught: pytest.ExceptionInfo[AppError]) -> str:
    assert caught.value.code is ErrorCode.INVITATION_INVALID
    return caught.value.details[0].code


async def _inspect_failure(container: Container, credential: str) -> str:
    with pytest.raises(AppError) as caught:
        await container.invitations.inspect(credential, None, client_key="test")
    return _failure(caught)


async def _manage(container: Container, avery: Actor, project: ProjectView) -> ProjectScope:
    return await scope_of(container, avery, project.project_id, Capability.MEMBERSHIP_MANAGE)


# --------------------------------------------------------------------------- RA-02


async def test_invitation_lifecycle_without_credential_exposure(
    container: Container,
    frozen_clock: FrozenClock,
    avery: Actor,
    project: ProjectView,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    scope = await _manage(container, avery, project)

    first, first_credential = await invite(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    assert first.state is InvitationState.PENDING
    assert first.inviter.user_id == avery.user_id
    message = outbox(container).sent[-1]
    assert message.to == JORDAN.email
    assert project.name in message.text
    assert "ClientCollaborator" not in message.text  # never the role

    # Resend: a new credential; the old one is revoked.
    second = await container.invitations.resend(scope, first.invitation_id, first.version)
    second_credential = last_credential(container)
    assert second.invitation_id != first.invitation_id
    assert await _inspect_failure(container, first_credential) == "Revoked"
    context = await container.invitations.inspect(second_credential, None, client_key="test")
    assert context.invitee_email_masked == "j•••@atlas-client.test"
    assert context.intended_role is ProjectRole.CLIENT_COLLABORATOR

    # Revoke is terminal.
    await container.invitations.revoke(scope, second.invitation_id, second.version)
    assert await _inspect_failure(container, second_credential) == "Revoked"
    with pytest.raises(InvalidTransition):
        await container.invitations.revoke(scope, second.invitation_id, second.version + 1)

    # Expiry: lazily on read, persisted by the sweep.
    third, third_credential = await invite(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    frozen_clock.advance(timedelta(days=7))
    assert await _inspect_failure(container, third_credential) == "Expired"
    assert (await container.invitations.get(scope, third.invitation_id)).state is (
        InvitationState.EXPIRED
    )
    assert await container.invitations.expire_due() == 1
    expired = await container.invitations.list(scope, InvitationState.EXPIRED, None)
    assert [i.invitation_id for i in expired.items] == [third.invitation_id]

    # Accept with a new account.
    _, credential = await invite(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    jordan = await actor_for(container, JORDAN)
    assert not jordan.is_active
    result = await container.invitations.accept(credential, jordan, identity_of(JORDAN))
    assert result.role is ProjectRole.CLIENT_COLLABORATOR
    assert result.actor.is_active
    again = await container.invitations.accept(credential, result.actor, identity_of(JORDAN))
    assert again.project_id == result.project_id
    members = await container.membership.list(scope, None)
    assert sum(1 for m in members.items if m.user_id == result.actor.user_id) == 1

    # Accept with an existing account (Morgan activated before being invited).
    morgan = await container.organization.activate_employee(identity_of(MORGAN))
    _, morgan_credential = await invite(
        container, avery, project.project_id, MORGAN, ProjectRole.CONTRIBUTOR
    )
    accepted = await container.invitations.accept(morgan_credential, morgan, identity_of(MORGAN))
    assert accepted.actor.user_id == morgan.user_id

    # No credential in any response or log line.
    listed = await container.invitations.list(scope, None, None)
    serialized = listed.model_dump_json(by_alias=True)
    for value in (first_credential, second_credential, third_credential, credential):
        assert value not in serialized
        assert value not in caplog.text


async def test_expiry_boundary(
    container: Container, frozen_clock: FrozenClock, avery: Actor, project: ProjectView
) -> None:
    _, credential = await invite(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    frozen_clock.advance(timedelta(days=6, hours=23, minutes=59))
    context = await container.invitations.inspect(credential, None, client_key="test")
    assert context.state is InvitationState.PENDING
    frozen_clock.advance(timedelta(minutes=1))
    assert await _inspect_failure(container, credential) == "Expired"


async def test_unknown_and_malformed_credentials(container: Container, firm: str) -> None:
    del firm
    assert await _inspect_failure(container, "x" * 43) == "Unknown"
    assert await _inspect_failure(container, "not a credential") == "Unknown"


# --------------------------------------------------------------------------- acceptance rules


async def test_acceptance_requires_the_invited_email(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    _, credential = await invite(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    riley = await actor_for(container, RILEY)
    with pytest.raises(AppError) as caught:
        await container.invitations.accept(credential, riley, identity_of(RILEY))
    assert caught.value.code is ErrorCode.INVITATION_EMAIL_MISMATCH

    with pytest.raises(AppError) as unverified:
        await container.invitations.accept(
            credential, riley, identity_of(JORDAN, email_verified=False)
        )
    assert unverified.value.code is ErrorCode.EMAIL_NOT_VERIFIED

    jordan = await actor_for(container, JORDAN)
    accepted = await container.invitations.accept(credential, jordan, identity_of(JORDAN))
    morgan = await container.organization.activate_employee(identity_of(MORGAN))
    with pytest.raises(AppError) as reused:
        await container.invitations.accept(credential, morgan, identity_of(MORGAN))
    assert _failure(reused) == "Accepted"
    assert accepted.actor.user_id != morgan.user_id


async def test_team_invitation_cannot_make_a_client_a_team_member(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    other = await new_project(container, avery, "Northstar Transformation")
    jordan = await join(container, avery, other.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR)
    scope = await _manage(container, avery, project)
    with pytest.raises(ValidationFailed):
        await container.invitations.issue(scope, JORDAN.email, ProjectRole.CONTRIBUTOR)
    with pytest.raises(ValidationFailed):
        await container.invitations.issue(scope, MORGAN.email, ProjectRole.CLIENT_COLLABORATOR)
    assert jordan.is_active


async def test_one_pending_invitation_per_email_and_no_member_reinvite(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    scope = await _manage(container, avery, project)
    await container.invitations.issue(scope, JORDAN.email, ProjectRole.CLIENT_COLLABORATOR)
    with pytest.raises(AlreadyExists):
        await container.invitations.issue(
            scope, JORDAN.email.upper(), ProjectRole.CLIENT_COLLABORATOR
        )
    with pytest.raises(AlreadyExists) as member:
        await container.invitations.issue(scope, AVERY.email, ProjectRole.VIEWER)
    assert member.value.details[0].code == "AlreadyMember"


async def test_email_failure_rolls_the_invitation_back(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    scope = await _manage(container, avery, project)
    outbox(container).fail_next = 1
    with pytest.raises(AppError):
        await container.invitations.issue(scope, JORDAN.email, ProjectRole.CLIENT_COLLABORATOR)
    assert (await container.invitations.list(scope, None, None)).items == []
    await container.invitations.issue(scope, JORDAN.email, ProjectRole.CLIENT_COLLABORATOR)


async def test_archived_project_lists_members_but_refuses_changes(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    lifecycle = await scope_of(container, avery, project.project_id, Capability.LIFECYCLE_MANAGE)
    await container.lifecycle.archive(lifecycle, project.version)
    scope = await _manage(container, avery, project)
    assert len((await container.membership.list(scope, None)).items) == 1
    with pytest.raises(ProjectReadOnly):
        await container.invitations.issue(scope, JORDAN.email, ProjectRole.CLIENT_COLLABORATOR)


# --------------------------------------------------------------------------- membership


async def test_final_administrator_cannot_be_demoted_or_removed(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    scope = await _manage(container, avery, project)
    avery_membership = await membership_id(container, avery, project.project_id, avery)
    preview = await container.membership.role_change_preview(
        scope, avery_membership, ProjectRole.VIEWER
    )
    assert preview.blocked == "FINAL_ADMINISTRATOR"
    with pytest.raises(AppError) as demote:
        await container.membership.change_role(scope, avery_membership, ProjectRole.VIEWER, 1)
    assert demote.value.code is ErrorCode.FINAL_ADMINISTRATOR
    with pytest.raises(AppError) as remove:
        await container.membership.remove(scope, avery_membership, 1)
    assert remove.value.code is ErrorCode.FINAL_ADMINISTRATOR

    await join(container, avery, project.project_id, MORGAN, ProjectRole.ADMINISTRATOR)
    preview = await container.membership.role_change_preview(
        scope, avery_membership, ProjectRole.VIEWER
    )
    assert preview.blocked is None
    demoted = await container.membership.change_role(scope, avery_membership, ProjectRole.VIEWER, 1)
    assert demoted.role is ProjectRole.VIEWER
    assert demoted.version == 2


async def test_role_change_preview_and_client_roles_fixed(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    riley = await join(container, avery, project.project_id, RILEY, ProjectRole.VIEWER)
    jordan = await join(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    scope = await _manage(container, avery, project)
    riley_membership = await membership_id(container, avery, project.project_id, riley)
    preview = await container.membership.role_change_preview(
        scope, riley_membership, ProjectRole.CONTRIBUTOR
    )
    assert Capability.DOCUMENT_UPLOAD in preview.gained
    assert preview.lost == []
    assert preview.blocked is None

    jordan_membership = await membership_id(container, avery, project.project_id, jordan)
    with pytest.raises(ValidationFailed):
        await container.membership.change_role(scope, jordan_membership, ProjectRole.CONTRIBUTOR, 1)
    with pytest.raises(ValidationFailed):
        await container.membership.change_role(
            scope, riley_membership, ProjectRole.CLIENT_COLLABORATOR, 1
        )


async def test_removal_deactivates_and_revokes_access(
    container: Container, avery: Actor, project: ProjectView
) -> None:
    riley = await join(container, avery, project.project_id, RILEY, ProjectRole.CONTRIBUTOR)
    scope = await _manage(container, avery, project)
    riley_membership = await membership_id(container, avery, project.project_id, riley)
    await container.membership.remove(scope, riley_membership, 1)
    with pytest.raises(NotAvailable):
        await scope_of(container, riley, project.project_id)
    with pytest.raises(NotAvailable):
        await container.membership.remove(scope, riley_membership, 2)
    members = await container.membership.list(scope, None)
    assert riley.user_id not in [m.user_id for m in members.items]
    actions = [
        e.action
        for e in (
            await container.audit.query(
                ProjectAdministratorViewer(project.project_id), AuditFilters(limit=100), None
            )
        ).items
    ]
    assert AuditAction.MEMBERSHIP_REMOVED.value in actions
    # The person can be invited again; a fresh membership is created.
    await join(container, avery, project.project_id, RILEY, ProjectRole.VIEWER)
    assert (await scope_of(container, riley, project.project_id)).role is ProjectRole.VIEWER


# --------------------------------------------------------------------------- people pickers


async def test_people_pickers(container: Container, avery: Actor, project: ProjectView) -> None:
    jordan = await join(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    scope = await scope_of(container, avery, project.project_id, Capability.TASK_ADMINISTER)
    assignees = await container.membership.people(scope, PeoplePurpose.TASK_ASSIGNEE)
    by_label = {p.display_label: p for p in assignees}
    assert by_label[AVERY.display_name].eligible_for_internal is True
    assert by_label[AVERY.display_name].user_id == avery.user_id
    assert by_label[JORDAN.display_name].eligible_for_internal is False
    assert by_label[JORDAN.display_name].user_id is None
    assert all(p.space == "Account" for p in assignees)

    review = await scope_of(container, avery, project.project_id, Capability.KNOWLEDGE_REVIEW)
    responsible = await container.membership.people(review, PeoplePurpose.ACTION_ITEM_RESPONSIBLE)
    assert [p.display_label for p in responsible] == [AVERY.display_name]

    search = await scope_of(container, avery, project.project_id, Capability.SEARCH)
    evidence = await container.membership.people(search, PeoplePurpose.PARTICIPANT_FILTER)
    assert all(p.space == "Evidence" for p in evidence)
    assert JORDAN.display_name not in [p.display_label for p in evidence]
    account_ids = {p.person_ref_id for p in assignees}
    assert account_ids.isdisjoint({p.person_ref_id for p in evidence})

    client_search = await scope_of(container, jordan, project.project_id, Capability.SEARCH)
    with pytest.raises(NotAvailable):
        await container.membership.people(client_search, PeoplePurpose.PARTICIPANT_FILTER)
    with pytest.raises(NotAvailable):
        await scope_of(container, jordan, project.project_id, Capability.TASK_ADMINISTER)


# --------------------------------------------------------------------------- audit


async def test_invitation_audit_is_content_free(
    container: Container, frozen_clock: FrozenClock, avery: Actor, project: ProjectView
) -> None:
    scope = await _manage(container, avery, project)
    first, _ = await invite(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    second = await container.invitations.resend(scope, first.invitation_id, first.version)
    await container.invitations.revoke(scope, second.invitation_id, second.version)
    await invite(container, avery, project.project_id, RILEY, ProjectRole.VIEWER)
    frozen_clock.advance(timedelta(days=8))
    await container.invitations.expire_due()
    await join(container, avery, project.project_id, MORGAN, ProjectRole.VIEWER)

    page = await container.audit.query(
        ProjectAdministratorViewer(project.project_id), AuditFilters(limit=100), None
    )
    actions = {e.action for e in page.items}
    assert {
        AuditAction.INVITATION_CREATED.value,
        AuditAction.INVITATION_RESENT.value,
        AuditAction.INVITATION_REVOKED.value,
        AuditAction.INVITATION_EXPIRED.value,
        AuditAction.INVITATION_ACCEPTED.value,
    } <= actions
    dumped = page.model_dump_json()
    for persona in (JORDAN, RILEY, MORGAN):
        assert persona.email not in dumped
        assert persona.email.split("@")[0] not in dumped


# --------------------------------------------------------------------------- HTTP


async def test_invitation_view_exposes_only_contract_fields(
    api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer, avery: Actor,
    project: ProjectView,
) -> None:  # fmt: skip
    headers = {
        **token_issuer.headers_for(AVERY),
        "Idempotency-Key": "7d1c43a6-4f1e-4c5a-9c0b-3e9b2a1d4f11",
    }
    created = await api_client.post(
        f"/api/v1/projects/{project.project_id}/invitations",
        headers=headers,
        json={"email": JORDAN.email, "role": "ClientCollaborator"},
    )
    assert created.status_code == 201
    assert set(created.json()) == {
        "invitationId", "email", "role", "inviter", "issuedAt", "expiresAt", "state", "version",
    }  # fmt: skip
    assert set(created.json()["inviter"]) == {"userId", "displayName"}


async def test_accept_over_http_for_a_new_client_collaborator(
    container: Container, api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
    avery: Actor, project: ProjectView,
) -> None:  # fmt: skip
    _, credential = await invite(
        container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR
    )
    auth = token_issuer.headers_for(JORDAN)
    inspected = await api_client.post(
        "/api/v1/invitations/inspect", headers=auth, json={"credential": credential}
    )
    assert inspected.status_code == 200
    assert inspected.json()["signedInEmailMatches"] is True
    assert JORDAN.email not in inspected.text

    headers = {**auth, "Idempotency-Key": "4b0f8a52-7d34-4a1d-9a4e-0f6d1c2b3a44"}
    accepted = await api_client.post(
        "/api/v1/invitations/accept", headers=headers, json={"credential": credential}
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["projectId"] == project.project_id
    assert body["role"] == "ClientCollaborator"
    assert body["me"]["activation"] == "Active"
    assert body["me"]["landing"] == {"kind": "Project", "projectId": project.project_id}
    replay = await api_client.post(
        "/api/v1/invitations/accept", headers=headers, json={"credential": credential}
    )
    assert replay.json() == body


async def test_inspect_is_rate_limited(
    api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer, firm: str
) -> None:
    del firm
    auth = token_issuer.headers_for(RILEY)
    statuses = [
        (
            await api_client.post(
                "/api/v1/invitations/inspect", headers=auth, json={"credential": "x" * 43}
            )
        ).status_code
        for _ in range(11)
    ]
    assert statuses[:10] == [422] * 10
    assert statuses[10] == 429


async def test_people_picker_route_hides_participants_from_clients(
    container: Container, api_client: httpx.AsyncClient, token_issuer: TestTokenIssuer,
    avery: Actor, project: ProjectView,
) -> None:  # fmt: skip
    await join(container, avery, project.project_id, JORDAN, ProjectRole.CLIENT_COLLABORATOR)
    url = f"/api/v1/projects/{project.project_id}/people?purpose="
    client = await api_client.get(
        url + "ParticipantFilter", headers=token_issuer.headers_for(JORDAN)
    )
    assert client.status_code == 404
    team = await api_client.get(url + "TaskAssignee", headers=token_issuer.headers_for(AVERY))
    assert team.status_code == 200
    jordan_entry = next(p for p in team.json() if p["kind"] == "ClientCollaborator")
    assert "userId" not in jordan_entry
    assert jordan_entry["eligibleForInternal"] is False
