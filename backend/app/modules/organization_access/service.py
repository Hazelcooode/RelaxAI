"""``OrganizationAccess``: actor resolution, employee activation, firm bootstrap (T03).

Accounts are created only at activation — employee activation here, Client Collaborator
activation through invitation acceptance (T06) — never in advance.

Known limitation (IAM-06): an existing Client Collaborator account whose email later falls
under an approved domain stays a Client Collaborator; changing category is out of scope.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import RowMapping

from app.modules.governance import (
    Audit,
    AuditAction,
    Outcome,
    SafeFacts,
    SystemActor,
    TargetRef,
)
from app.modules.organization_access import repository as repo
from app.modules.organization_access.domain import (
    Activation,
    ActivationFailure,
    Actor,
    FirmRoles,
    OperatorActor,
    PersonCategory,
    employee_activation_failure,
    normalize_domain,
    subject_reference,
)
from app.platform.auth import RejectionReason, VerifiedIdentity
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork
from app.platform.errors import (
    AccountNotActivated,
    AlreadyExists,
    AppError,
    ErrorCode,
    FieldIssue,
    GovernanceOnly,
    NotAvailable,
    ValidationFailed,
)

log = logging.getLogger("relaxai.auth")

_FAILURE_ERRORS = {
    ActivationFailure.EMAIL_NOT_VERIFIED: ErrorCode.EMAIL_NOT_VERIFIED,
    ActivationFailure.DOMAIN_NOT_APPROVED: ErrorCode.ACTIVATION_DOMAIN_NOT_APPROVED,
}


@dataclass(frozen=True, slots=True)
class AccountContact:
    """Delivery details for notifications. ``email`` is personal data: never log it."""

    user_id: str
    email: str
    person_category: PersonCategory
    active: bool


@dataclass(frozen=True, slots=True)
class ApprovedDomain:
    domain: str
    created_at: datetime


class AuditRateLimiter:
    """In-process limiter for failure audit events, so a token-spraying client cannot flood
    the audit log (GOV-01): one event per key per ``per_key``, at most ``per_minute`` total.
    Every failure is still counted in the operational log."""

    def __init__(
        self, clock: Clock, *, per_key: timedelta = timedelta(minutes=1), per_minute: int = 30
    ) -> None:
        self._clock = clock
        self._per_key = per_key
        self._per_minute = per_minute
        self._last: dict[str, datetime] = {}
        self._window: list[datetime] = []

    def allow(self, key: str) -> bool:
        now = self._clock.now()
        self._window = [t for t in self._window if now - t < timedelta(minutes=1)]
        if len(self._last) > 10_000:
            self._last = {k: t for k, t in self._last.items() if now - t < self._per_key}
        last = self._last.get(key)
        if (last is not None and now - last < self._per_key) or len(
            self._window
        ) >= self._per_minute:
            return False
        self._last[key] = now
        self._window.append(now)
        return True


class OrganizationAccess:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        firm_id: str,
        stamp: str,
        audit: Audit,
        operator_subjects: Iterable[str],
        reference_key: bytes,
        failure_limiter: AuditRateLimiter | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._firm_id = firm_id
        self._stamp = stamp
        self._audit = audit
        self._operators = frozenset(operator_subjects)
        self._reference_key = reference_key
        self._limiter = failure_limiter or AuditRateLimiter(clock)

    # ------------------------------------------------------------------ actors

    async def resolve_actor(self, identity: VerifiedIdentity) -> Actor:
        """The request's actor. A disabled account is ``403 ACCOUNT_NOT_ACTIVATED``."""
        async with self._uow() as tx:
            row = await repo.account_for(tx, self._firm_id, identity)
            if row is None:
                return self._not_activated(identity)
            return await self._actor(tx, row, identity)

    async def activate_employee(self, identity: VerifiedIdentity) -> Actor:
        """Create (or reuse) a Team Member account for a verified approved-domain email.

        Idempotent: an existing account for the subject or verified email is returned as is
        (IAM-06), so a second activation creates nothing and audits nothing.
        """
        async with self._uow() as tx:
            row = await repo.account_for(tx, self._firm_id, identity, for_update=True)
            if row is not None:
                return await self._actor(tx, row, identity)
            approved = await repo.approved_domain_names(tx, self._firm_id)
            failure = employee_activation_failure(identity, approved)
            if failure is None:
                row = await self._create_account(tx, identity, PersonCategory.TEAM_MEMBER)
                return await self._actor(tx, row, identity)
        # Recorded in its own transaction: the activation itself changed nothing.
        await self._record_activation_failure(identity, failure)
        raise AppError(_FAILURE_ERRORS[failure])

    async def activate_client_collaborator(self, tx: Tx, identity: VerifiedIdentity) -> Actor:
        """Called only by invitation acceptance (T06), inside its transaction."""
        row = await repo.account_for(tx, self._firm_id, identity, for_update=True)
        if row is None:
            if not identity.email_verified or identity.email is None:
                raise AppError(ErrorCode.EMAIL_NOT_VERIFIED)
            row = await self._create_account(tx, identity, PersonCategory.CLIENT_COLLABORATOR)
        return await self._actor(tx, row, identity)

    async def firm_roles(self, user_id: str) -> FirmRoles:
        async with self._uow() as tx:
            row = await repo.account_by_id(tx, self._firm_id, user_id)
            if row is None:
                raise NotAvailable()
            manager = await repo.is_firm_manager(tx, self._firm_id, user_id)
        return FirmRoles(
            firm_manager=manager, platform_operator=row["external_subject"] in self._operators
        )

    def operator_for(self, actor: Actor) -> OperatorActor:
        """``require_operator``: ``operator.enter`` or ``403 GOVERNANCE_ONLY``. An operator
        needs no firm account; anyone else who is not activated gets ``ACCOUNT_NOT_ACTIVATED``
        first (contract §1.1)."""
        if not actor.platform_operator:
            if not actor.is_active:
                raise AccountNotActivated()
            raise GovernanceOnly()
        return OperatorActor(reference=subject_reference(actor.subject, self._reference_key))

    async def record_sign_in_failure(
        self, reason: RejectionReason, unverified_subject: str | None
    ) -> None:
        """Operational metric for every refused token; a rate-limited audit event when the
        token named a subject (stored only as a keyed hash)."""
        log.warning("auth.sign_in_failed", extra={"error_code": reason.value})
        if unverified_subject is None:
            return
        reference = subject_reference(unverified_subject, self._reference_key)
        if not self._limiter.allow(f"sign-in:{reference}"):
            return
        async with self._uow() as tx:
            await self._audit.record(
                tx,
                actor=SystemActor("Sign-in"),
                action=AuditAction.ACCOUNT_SIGN_IN_FAILED,
                target=TargetRef("external_subject", reference),
                outcome=Outcome.FAILED,
                facts=SafeFacts(failureCode=reason.value, subjectHash=reference),
            )

    # ------------------------------------------------------------------ operator commands

    async def init_firm(self, operator: OperatorActor, name: str) -> str:
        name = name.strip()
        if not 1 <= len(name) <= 200:
            raise ValidationFailed(
                [FieldIssue(field="name", code="string_length", message="Name is 1-200 chars.")]
            )
        async with self._uow() as tx:
            created = await repo.insert_firm(
                tx, firm_id=self._firm_id, name=name, stamp=self._stamp, now=self._clock.now()
            )
            if created:
                await self._audit.record(
                    tx,
                    actor=operator.actor_ref,
                    action=AuditAction.FIRM_INITIALIZED,
                    target=TargetRef("firm", self._firm_id),
                    outcome=Outcome.SUCCEEDED,
                    facts=SafeFacts(stamp=self._stamp),
                )
        return self._firm_id

    async def list_approved_domains(self) -> list[ApprovedDomain]:
        async with self._uow() as tx:
            rows = await repo.list_domains(tx, self._firm_id)
        return [ApprovedDomain(domain=str(r["domain"]), created_at=r["created_at"]) for r in rows]

    async def add_approved_domain(self, operator: OperatorActor, domain: str) -> None:
        """Idempotent: adding an approved domain again changes nothing and audits nothing."""
        normalized = _domain_or_422(domain)
        async with self._uow() as tx:
            if await repo.lock_firm(tx, self._firm_id) is None:
                raise NotAvailable()
            added = await repo.insert_domain(
                tx,
                firm_id=self._firm_id,
                domain=normalized,
                created_by=operator.reference,
                now=self._clock.now(),
            )
            if added:
                await self._audit_domain(tx, operator, AuditAction.DOMAIN_ADDED, normalized)

    async def remove_approved_domain(self, operator: OperatorActor, domain: str) -> None:
        """Existing accounts stay active; only new employee activations are affected."""
        normalized = _domain_or_422(domain)
        async with self._uow() as tx:
            if not await repo.delete_domain(tx, firm_id=self._firm_id, domain=normalized):
                raise NotAvailable()
            await self._audit_domain(tx, operator, AuditAction.DOMAIN_REMOVED, normalized)

    async def appoint_first_firm_manager(self, operator: OperatorActor, user_id: str) -> str:
        """Bootstrap the first Firm Manager: an active Team Member, while none is active.
        Returns the assignment id. T30 owns nomination, approval, and removal."""
        async with self._uow() as tx:
            # The firm row lock serialises concurrent bootstrap attempts.
            if await repo.lock_firm(tx, self._firm_id) is None:
                raise NotAvailable()
            account = await repo.account_by_id(tx, self._firm_id, user_id)
            if account is None:
                raise NotAvailable()
            if (
                account["activation_state"] != Activation.ACTIVE.value
                or account["person_category"] != PersonCategory.TEAM_MEMBER.value
            ):
                raise ValidationFailed(
                    [
                        FieldIssue(
                            field="userId",
                            code="not_active_team_member",
                            message="The first Firm Manager must be an active Team Member.",
                        )
                    ]
                )
            if await repo.any_active_firm_manager(tx, self._firm_id):
                raise AlreadyExists()
            assignment_id = await repo.insert_firm_manager(
                tx,
                firm_id=self._firm_id,
                user_id=user_id,
                approved_by=operator.reference,
                now=self._clock.now(),
            )
            await self._audit.record(
                tx,
                actor=operator.actor_ref,
                action=AuditAction.FIRM_MANAGER_APPOINTED,
                target=TargetRef("firm_manager_assignment", assignment_id),
                outcome=Outcome.SUCCEEDED,
                facts=SafeFacts(userId=user_id),
            )
        return assignment_id

    async def account_contact(self, user_id: str) -> AccountContact | None:
        async with self._uow() as tx:
            row = await repo.account_by_id(tx, self._firm_id, user_id)
        if row is None:
            return None
        return AccountContact(
            user_id=row["id"],
            email=str(row["email"]),
            person_category=PersonCategory(row["person_category"]),
            active=row["activation_state"] == Activation.ACTIVE.value
            and row["disabled_at"] is None,
        )

    async def user_id_for_email(self, email: str) -> str:
        """For ``relaxai-admin firm-manager appoint-first --email``."""
        async with self._uow() as tx:
            row = await repo.account_by_email(tx, self._firm_id, email.strip().lower())
        if row is None:
            raise NotAvailable()
        return str(row["id"])

    # ------------------------------------------------------------------ internals

    def _not_activated(self, identity: VerifiedIdentity) -> Actor:
        return Actor(
            user_id=None,
            firm_id=self._firm_id,
            subject=identity.subject,
            email=identity.email,
            display_name=identity.display_name,
            person_category=None,
            activation=Activation.NOT_ACTIVATED,
            firm_manager=False,
            platform_operator=identity.subject in self._operators,
        )

    async def _actor(self, tx: Tx, row: RowMapping, identity: VerifiedIdentity) -> Actor:
        activation = Activation(row["activation_state"])
        if activation is Activation.DISABLED or row["disabled_at"] is not None:
            raise AccountNotActivated()
        return Actor(
            user_id=row["id"],
            firm_id=self._firm_id,
            subject=identity.subject,
            email=str(row["email"]),
            display_name=row["display_name"],
            person_category=PersonCategory(row["person_category"]),
            activation=activation,
            firm_manager=await repo.is_firm_manager(tx, self._firm_id, row["id"]),
            platform_operator=identity.subject in self._operators,
        )

    async def _create_account(
        self, tx: Tx, identity: VerifiedIdentity, category: PersonCategory
    ) -> RowMapping:
        row = await repo.insert_account(
            tx, firm_id=self._firm_id, identity=identity, category=category, now=self._clock.now()
        )
        if row is None:  # lost a race with a concurrent activation: reuse its account
            existing = await repo.account_for(tx, self._firm_id, identity)
            if existing is None:
                raise AlreadyExists()
            return existing
        actor_ref = (await self._actor(tx, row, identity)).actor_ref
        await self._audit.record(
            tx,
            actor=actor_ref,
            action=AuditAction.ACCOUNT_ACTIVATION,
            target=TargetRef("user_account", row["id"]),
            outcome=Outcome.SUCCEEDED,
            facts=SafeFacts(
                personCategory=category.value,
                domainApproved=category is PersonCategory.TEAM_MEMBER,
            ),
        )
        return row

    async def _record_activation_failure(
        self, identity: VerifiedIdentity, failure: ActivationFailure
    ) -> None:
        reference = subject_reference(identity.subject, self._reference_key)
        if not self._limiter.allow(f"activation:{reference}:{failure.value}"):
            return
        async with self._uow() as tx:
            await self._audit.record(
                tx,
                actor=SystemActor("Activation"),
                action=AuditAction.ACCOUNT_ACTIVATION,
                target=TargetRef("external_subject", reference),
                outcome=Outcome.FAILED,
                facts=SafeFacts(failureCode=failure.value, domainApproved=False),
            )

    async def _audit_domain(
        self, tx: Tx, operator: OperatorActor, action: AuditAction, domain: str
    ) -> None:
        await self._audit.record(
            tx,
            actor=operator.actor_ref,
            action=action,
            target=TargetRef("approved_domain", domain),
            outcome=Outcome.SUCCEEDED,
            facts=SafeFacts(domain=domain),
        )


def _domain_or_422(domain: str) -> str:
    try:
        return normalize_domain(domain)
    except ValueError:
        raise ValidationFailed(
            [FieldIssue(field="domain", code="invalid_domain", message="Not a valid domain.")]
        ) from None
