"""Temporary Platform Operator authentication.

**Replacement point for T03.** Until the Entra token verifier and the Platform Operator role
exist, ``/operator/*`` accepts ``Authorization: Bearer <RELAXAI__OPERATOR__BOOTSTRAP_TOKEN>``.
When the token is not configured every operator route is denied, so an environment cannot
accidentally expose operator capability. T03 replaces ``operator_principal`` with token
verification plus the operator claim; routes and services keep taking ``OperatorContext``
and do not change.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.modules.governance import ActorKind, ActorRef
from app.platform.errors import GovernanceOnly, Unauthenticated

BOOTSTRAP_OPERATOR_LABEL = "Platform Operator"
# Bootstrap has no directory account; the id marks provenance in the audit log.
BOOTSTRAP_OPERATOR_ID = "usr_bootstrap_operator"

_bearer = HTTPBearer(auto_error=False, description="Bootstrap operator token (until T03)")


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    label: str = BOOTSTRAP_OPERATOR_LABEL
    user_id: str = BOOTSTRAP_OPERATOR_ID

    @property
    def actor(self) -> ActorRef:
        return ActorRef(user_id=self.user_id, kind=ActorKind.PLATFORM_OPERATOR, label=self.label)


def operator_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> OperatorPrincipal:
    configured = request.app.state.container.settings.operator.bootstrap_token
    if configured is None:
        raise GovernanceOnly()
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise Unauthenticated()
    supplied = credentials.credentials.encode()
    if not hmac.compare_digest(supplied, configured.get_secret_value().encode()):
        raise Unauthenticated()
    return OperatorPrincipal()
