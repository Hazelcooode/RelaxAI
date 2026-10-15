"""Request dependencies shared by ``/api/v1`` routes: authentication and actor checks.

``current_actor`` verifies the bearer token and resolves the actor; ``ActiveActor`` adds
the account-state rule (contract §1.1); ``Operator`` adds ``operator.enter``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.container import Container
from app.modules.governance import Audit, JobQueue, UsageGuard
from app.modules.organization_access import Actor, OperatorActor, OrganizationAccess
from app.platform.auth import RejectionReason, TokenRejected, VerifiedIdentity
from app.platform.clock import Clock
from app.platform.db import UnitOfWork
from app.platform.errors import AccountNotActivated
from app.settings import Settings

_bearer = HTTPBearer(auto_error=False, description="Microsoft Entra External ID access token")


def container_of(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


AppContainer = Annotated[Container, Depends(container_of)]


async def verified_identity(
    container: AppContainer,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> VerifiedIdentity:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise TokenRejected(RejectionReason.MISSING)
    try:
        return await container.token_verifier.verify(credentials.credentials)
    except TokenRejected as rejected:
        await container.organization.record_sign_in_failure(
            rejected.reason, rejected.unverified_subject
        )
        raise


Identity = Annotated[VerifiedIdentity, Depends(verified_identity)]


async def current_actor(container: AppContainer, identity: Identity) -> Actor:
    return await container.organization.resolve_actor(identity)


CurrentActor = Annotated[Actor, Depends(current_actor)]


def require_activated_actor(actor: CurrentActor) -> Actor:
    if not actor.is_active:
        raise AccountNotActivated()
    return actor


ActiveActor = Annotated[Actor, Depends(require_activated_actor)]


def require_operator(container: AppContainer, actor: CurrentActor) -> OperatorActor:
    return container.organization.operator_for(actor)


@dataclass(slots=True)
class OperatorContext:
    """What an operator route may call, with the operator's identity attached."""

    settings: Settings
    clock: Clock
    uow: UnitOfWork
    queue: JobQueue
    usage: UsageGuard
    audit: Audit
    organization: OrganizationAccess
    operator: OperatorActor


def operator_context(
    container: AppContainer, operator: Annotated[OperatorActor, Depends(require_operator)]
) -> OperatorContext:
    return OperatorContext(
        settings=container.settings,
        clock=container.clock,
        uow=container.uow,
        queue=container.queue,
        usage=container.usage,
        audit=container.audit,
        organization=container.organization,
        operator=operator,
    )


Operator = Annotated[OperatorContext, Depends(operator_context)]
