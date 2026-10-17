"""Helpers for the projects tests (T05-T07): every member joins through the real invitation
flow, with the credential read from the outbox email, so the tests exercise the same path
as production."""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.container import Container
from app.modules.organization_access import Actor
from app.modules.projects import (
    AccessPurpose,
    Capability,
    InvitationView,
    ProjectRole,
    ProjectScope,
    ProjectView,
)
from app.platform.email import OutboxEmailSender
from tests.conftest import identity_of
from tests.fixtures.synthetic.personas import Persona

_CREDENTIAL = re.compile(r"/invite#c=([A-Za-z0-9_\-]{43})")
PROJECT_NAME = "Atlas Expansion"


async def actor_for(container: Container, persona: Persona) -> Actor:
    return await container.organization.resolve_actor(identity_of(persona))


async def new_project(
    container: Container, owner: Actor, name: str = PROJECT_NAME, description: str | None = None
) -> ProjectView:
    return await container.projects.create(owner, name, description)


async def scope_of(
    container: Container,
    actor: Actor,
    project_id: str,
    capability: Capability = Capability.PROJECT_READ,
    purpose: AccessPurpose = AccessPurpose.ORDINARY,
) -> ProjectScope:
    return await container.project_access.require(actor, project_id, capability, purpose=purpose)


def outbox(container: Container) -> OutboxEmailSender:
    assert isinstance(container.email, OutboxEmailSender)
    return container.email


def last_credential(container: Container) -> str:
    match = _CREDENTIAL.search(outbox(container).sent[-1].text)
    assert match is not None, "the invitation email carries the credential in the fragment"
    return match.group(1)


async def invite(
    container: Container, admin: Actor, project_id: str, persona: Persona, role: ProjectRole
) -> tuple[InvitationView, str]:
    scope = await scope_of(container, admin, project_id, Capability.MEMBERSHIP_MANAGE)
    view = await container.invitations.issue(scope, persona.email, role)
    return view, last_credential(container)


async def join(
    container: Container, admin: Actor, project_id: str, persona: Persona, role: ProjectRole
) -> Actor:
    """Invite and accept: the persona ends up an active member with ``role``."""
    _, credential = await invite(container, admin, project_id, persona, role)
    actor = await actor_for(container, persona)
    result = await container.invitations.accept(credential, actor, identity_of(persona))
    return result.actor


async def membership_id(container: Container, admin: Actor, project_id: str, user: Actor) -> str:
    scope = await scope_of(container, admin, project_id, Capability.MEMBERSHIP_MANAGE)
    page = await container.membership.list(scope, None, limit=100)
    return next(m.membership_id for m in page.items if m.user_id == user.user_id)


def project_ids(views: Sequence[ProjectView]) -> list[str]:
    return [v.project_id for v in views]
