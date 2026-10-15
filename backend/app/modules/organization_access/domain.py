"""Pure organization_access rules: actors, activation policy, domain normalization.

Nothing here performs I/O.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.modules.governance import ActorKind, ActorRef
from app.platform.auth import VerifiedIdentity


class PersonCategory(StrEnum):
    TEAM_MEMBER = "TeamMember"
    CLIENT_COLLABORATOR = "ClientCollaborator"


class Activation(StrEnum):
    ACTIVE = "Active"
    NOT_ACTIVATED = "NotActivated"
    DISABLED = "Disabled"


class GlobalCapability(StrEnum):
    PROJECT_CREATE = "project.create"
    GOVERNANCE_ENTER = "governance.enter"
    OPERATOR_ENTER = "operator.enter"


class FirmManagerState(StrEnum):
    ACTIVE = "Active"
    # T30 adds Nominated, Rejected, Removed.


@dataclass(frozen=True, slots=True)
class FirmRoles:
    firm_manager: bool
    platform_operator: bool


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated principal behind one request (``current_actor``).

    ``user_id`` is ``None`` until the person activates an account; such an actor may reach
    only ``GET /me`` and the activation/invitation routes (contract §1.1).
    """

    user_id: str | None
    firm_id: str
    subject: str
    email: str | None
    display_name: str
    person_category: PersonCategory | None
    activation: Activation
    firm_manager: bool
    platform_operator: bool

    @property
    def is_active(self) -> bool:
        return self.activation is Activation.ACTIVE and self.user_id is not None

    @property
    def is_team_member(self) -> bool:
        return self.is_active and self.person_category is PersonCategory.TEAM_MEMBER

    @property
    def firm_roles(self) -> FirmRoles:
        return FirmRoles(firm_manager=self.firm_manager, platform_operator=self.platform_operator)

    @property
    def actor_ref(self) -> ActorRef:
        """The audit/job reference of an *activated* actor."""
        if self.user_id is None or self.person_category is None:
            raise ValueError("only an activated actor has an actor reference")
        kind = (
            ActorKind.CLIENT_COLLABORATOR
            if self.person_category is PersonCategory.CLIENT_COLLABORATOR
            else ActorKind.TEAM_MEMBER
        )
        return ActorRef(user_id=self.user_id, kind=kind, label=self.display_name)


def global_capabilities(actor: Actor) -> list[GlobalCapability]:
    capabilities: list[GlobalCapability] = []
    if actor.is_team_member:
        capabilities.append(GlobalCapability.PROJECT_CREATE)
    if actor.is_active and actor.firm_manager:
        capabilities.append(GlobalCapability.GOVERNANCE_ENTER)
    if actor.platform_operator:
        capabilities.append(GlobalCapability.OPERATOR_ENTER)
    return capabilities


def authorization_version(actor: Actor) -> str:
    """Changes whenever anything that drives navigation changes. T05 folds memberships,
    roles, and lifecycles into the same digest."""
    parts = (
        actor.user_id or "-",
        actor.activation.value,
        actor.person_category.value if actor.person_category else "-",
        str(actor.firm_manager),
        str(actor.platform_operator),
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- operators

CLI_OPERATOR_REFERENCE: Final = "cli:relaxai-admin"
OPERATOR_LABEL: Final = "Platform Operator"


@dataclass(frozen=True, slots=True)
class OperatorActor:
    """A Platform Operator acting through ``/operator/*`` or ``relaxai-admin``.

    Operators are configuration (``entra.operator_subjects``), not accounts, so their audit
    reference is a keyed hash of the subject rather than a ``usr_`` id.
    """

    reference: str
    label: str = OPERATOR_LABEL

    @property
    def actor_ref(self) -> ActorRef:
        return ActorRef(user_id=self.reference, kind=ActorKind.PLATFORM_OPERATOR, label=self.label)

    @classmethod
    def cli(cls) -> OperatorActor:
        return cls(reference=CLI_OPERATOR_REFERENCE)


def subject_reference(subject: str, key: bytes) -> str:
    """Stable, non-reversible reference to an external subject (audit target ids)."""
    digest = hmac.new(key, f"subject:{subject}".encode(), hashlib.sha256).hexdigest()
    return f"sub_{digest[:32]}"


# --------------------------------------------------------------------------- activation


class ActivationFailure(StrEnum):
    EMAIL_NOT_VERIFIED = "EmailNotVerified"
    DOMAIN_NOT_APPROVED = "DomainNotApproved"


def employee_activation_failure(
    identity: VerifiedIdentity, approved_domains: frozenset[str]
) -> ActivationFailure | None:
    """``EmployeeActivationPolicy``: a verified email under an approved domain (IAM-01)."""
    if not identity.email_verified or identity.email is None:
        return ActivationFailure.EMAIL_NOT_VERIFIED
    if identity.email_domain not in approved_domains:
        return ActivationFailure.DOMAIN_NOT_APPROVED
    return None


_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def normalize_domain(value: str) -> str:
    """Lower-case ASCII (IDNA) form of an email domain; ``ValueError`` when not a domain."""
    candidate = value.strip().rstrip(".").lower()
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("not a valid domain") from None
    labels = candidate.split(".")
    if len(candidate) > 253 or len(labels) < 2 or not all(_LABEL.match(x) for x in labels):
        raise ValueError("not a valid domain")
    if labels[-1].isdigit():
        raise ValueError("not a valid domain")
    return candidate
