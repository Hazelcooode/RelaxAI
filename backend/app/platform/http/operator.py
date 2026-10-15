"""``OperatorContext``: what an operator route may call, with the operator's identity attached."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.modules.governance import ActorRef, Audit, JobQueue, UsageGuard
from app.platform.clock import Clock
from app.platform.db import UnitOfWork
from app.platform.http.operator_auth import OperatorPrincipal, operator_principal
from app.settings import Settings


@dataclass(slots=True)
class OperatorContext:
    settings: Settings
    clock: Clock
    uow: UnitOfWork
    queue: JobQueue
    usage: UsageGuard
    audit: Audit
    claims: OperatorPrincipal

    @property
    def actor(self) -> ActorRef:
        return self.claims.actor


def operator_context(
    request: Request,
    claims: Annotated[OperatorPrincipal, Depends(operator_principal)],
) -> OperatorContext:
    container = request.app.state.container
    return OperatorContext(
        settings=container.settings,
        clock=container.clock,
        uow=container.uow,
        queue=container.queue,
        usage=container.usage,
        audit=container.audit,
        claims=claims,
    )
