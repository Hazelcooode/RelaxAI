"""``Invitations``: single-use, expiring Project invitations (T06, AUTH-03/04/05/10).

The credential is 32 random bytes. Only its SHA-256 digest is stored; the plaintext exists
in the outgoing email link alone, in the URL *fragment* (``/invite#c=…``) so it never
reaches Nginx, the API's access log, or a ``Referer``. It is never logged, audited, or
returned by any endpoint.

``Accepted``, ``Expired``, and ``Revoked`` are terminal. Expiry is applied lazily on read
and by the hourly ``projects.invitation_expire`` sweep.
"""

from __future__ import annotations

import builtins
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import RowMapping
from sqlalchemy.exc import IntegrityError

from app.modules.actions import Notifications, NotificationTrigger, SafeSubjectRef
from app.modules.governance import (
    Audit,
    AuditAction,
    Outcome,
    SafeFacts,
    SystemActor,
    TargetRef,
)
from app.modules.organization_access import Actor, OrganizationAccess
from app.modules.projects import repository as repo
from app.modules.projects.authorization import ProjectScope
from app.modules.projects.domain import (
    INVITATION_TTL,
    TEAM_ROLES,
    ActivityKind,
    InvitationEventKind,
    InvitationFailure,
    InvitationState,
    Lifecycle,
    ProjectRole,
    credential_digest,
    effective_state,
    email_domain,
    mask_email,
    new_credential,
    normalize_invitee_email,
    well_formed_credential,
)
from app.modules.projects.service import ProjectActivity
from app.platform.auth import VerifiedIdentity
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork
from app.platform.errors import (
    AlreadyExists,
    AppError,
    ErrorCode,
    FieldIssue,
    InvalidTransition,
    NotAvailable,
    RateLimited,
    ValidationFailed,
    VersionConflict,
)
from app.platform.http.pagination import CursorCodec, Page
from app.platform.schema import CamelModel

# --------------------------------------------------------------------------- views


class InviterView(CamelModel):
    user_id: str
    display_name: str


class InvitationView(CamelModel):
    """Contract §6.4: invitee, role, inviter, issue time, expiry, state — nothing else."""

    invitation_id: str
    email: str
    role: ProjectRole
    inviter: InviterView
    issued_at: datetime
    expires_at: datetime
    state: InvitationState
    version: int


class InvitationContext(CamelModel):
    """Contract §4.3: what the credential holder may see before accepting."""

    project_name: str
    intended_role: ProjectRole
    inviter_display_name: str
    invitee_email_masked: str
    issued_at: datetime
    expires_at: datetime
    state: InvitationState
    signed_in_email_matches: bool


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    project_id: str
    role: ProjectRole
    actor: Actor


_FAILURE_MESSAGES = {
    InvitationFailure.UNKNOWN: "This invitation link is not valid.",
    InvitationFailure.EXPIRED: "This invitation has expired. Ask for a new one.",
    InvitationFailure.REVOKED: "This invitation was withdrawn.",
    InvitationFailure.ACCEPTED: "This invitation has already been used.",
    InvitationFailure.PROJECT_UNAVAILABLE: "This Project is no longer available.",
}

_STATE_FAILURES = {
    InvitationState.EXPIRED: InvitationFailure.EXPIRED,
    InvitationState.REVOKED: InvitationFailure.REVOKED,
    InvitationState.ACCEPTED: InvitationFailure.ACCEPTED,
}


def invitation_invalid(failure: InvitationFailure) -> AppError:
    """The reason is safe to give: the credential holder is entitled to know it."""
    return AppError(
        ErrorCode.INVITATION_INVALID,
        details=[FieldIssue(code=failure.value, message=_FAILURE_MESSAGES[failure])],
    )


# --------------------------------------------------------------------------- rate limit


class AttemptLimiter:
    """Sliding-window limit on credential lookups per key (10 per 15 minutes) to blunt
    guessing. In-process: each API process enforces its own window."""

    def __init__(
        self, clock: Clock, *, attempts: int = 10, window: timedelta = timedelta(minutes=15)
    ) -> None:
        self._clock = clock
        self._attempts = attempts
        self._window = window
        self._hits: dict[str, deque[datetime]] = {}

    def hit(self, *keys: str) -> None:
        now = self._clock.now()
        if len(self._hits) > 10_000:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self._window}
        for key in keys:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self._window:
                hits.popleft()
            if len(hits) >= self._attempts:
                retry_after = self._window - (now - hits[0])
                raise RateLimited(retry_after_seconds=max(1, int(retry_after.total_seconds())))
        for key in keys:
            self._hits[key].append(now)


# --------------------------------------------------------------------------- service


class Invitations:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        firm_id: str,
        audit: Audit,
        activity: ProjectActivity,
        directory: OrganizationAccess,
        notifications: Notifications,
        cursors: CursorCodec,
        invite_base_url: str,
        limiter: AttemptLimiter | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._firm_id = firm_id
        self._audit = audit
        self._activity = activity
        self._directory = directory
        self._notifications = notifications
        self._cursors = cursors
        self._invite_url = invite_base_url.rstrip("/") + "/invite#c="
        self._limiter = limiter or AttemptLimiter(clock)

    # ------------------------------------------------------------------ administrators

    async def issue(self, scope: ProjectScope, email: str, role: ProjectRole) -> InvitationView:
        """``membership.manage``. Team roles need an approved firm domain; any other domain
        is invited as a Client Collaborator."""
        scope.require_writable()
        address = normalize_invitee_email(email)
        if address is None:
            raise ValidationFailed(
                [FieldIssue(field="email", code="email", message="Enter an email address.")]
            )
        await self._check_role_for_domain(address, role)
        account = await self._directory.profile_for_email(address)
        now = self._clock.now()
        async with self._uow() as tx:
            if account is not None and await repo.active_membership_for_user(
                tx, scope.project_id, account.user_id
            ):
                raise AlreadyExists(
                    details=[
                        FieldIssue(
                            field="email",
                            code="AlreadyMember",
                            message="This person is already a member.",
                        )
                    ]
                )
            await self._expire(tx, now, project_id=scope.project_id, email=address)
            if await repo.pending_invitation_for_email(tx, scope.project_id, address):
                raise AlreadyExists(
                    details=[
                        FieldIssue(
                            field="email",
                            code="PendingInvitation",
                            message="An invitation is already pending for this email.",
                        )
                    ]
                )
            await repo.lock_writable_project(tx, scope.project_id)
            row, credential = await self._issue(tx, scope, address, role, now=now, resend_of=None)
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.INVITATION_CREATED,
                target=TargetRef("invitation", row["id"]),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
                facts=SafeFacts(role=role.value, expiresAt=row["expires_at"]),
            )
            await self._send(tx, scope, address, credential)
        return (await self._views([row], now))[0]

    async def list(
        self,
        scope: ProjectScope,
        state: InvitationState | None,
        cursor: str | None,
        *,
        limit: int = 25,
    ) -> Page[InvitationView]:
        after = None
        if cursor is not None:
            decoded = self._cursors.decode(cursor)
            try:
                if not isinstance(decoded.k, str):
                    raise ValueError("cursor")
                after = (datetime.fromisoformat(decoded.k), decoded.id)
            except ValueError:
                raise ValidationFailed(
                    [FieldIssue(field="cursor", code="invalid", message="Invalid cursor.")]
                ) from None
        now = self._clock.now()
        async with self._uow() as tx:
            rows = await repo.page_invitations(
                tx,
                scope.project_id,
                states=None if state is None else {state},
                expired_before=now,
                after=after,
                limit=limit + 1,
            )
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = self._cursors.encode(last["issued_at"].isoformat(), last["id"])
        return Page[InvitationView](items=await self._views(page, now), next_cursor=next_cursor)

    async def get(self, scope: ProjectScope, invitation_id: str) -> InvitationView:
        async with self._uow() as tx:
            row = await repo.invitation(tx, scope.project_id, invitation_id)
        if row is None:
            raise NotAvailable()
        return (await self._views([row], self._clock.now()))[0]

    async def resend(self, scope: ProjectScope, invitation_id: str, version: int) -> InvitationView:
        """A new credential with the same email and role; a still-Pending original becomes
        Revoked in the same transaction (its credential stops working)."""
        scope.require_writable()
        now = self._clock.now()
        async with self._uow() as tx:
            await repo.lock_writable_project(tx, scope.project_id)
            old = await self._locked(tx, scope, invitation_id, version, now)
            state = effective_state(InvitationState(old["state"]), old["expires_at"], now)
            if state not in (InvitationState.PENDING, InvitationState.EXPIRED):
                raise InvalidTransition(allowed_transitions=[])
            if state is InvitationState.PENDING:
                await repo.set_invitation_state(
                    tx, old["id"], InvitationState.REVOKED, revoked_at=now
                )
                await repo.record_invitation_event(
                    tx, old["id"], InvitationEventKind.REVOKE, at=now, actor_user_id=scope.user_id
                )
            elif InvitationState(old["state"]) is InvitationState.PENDING:
                # Lazily expired: persist it with the same event and audit as the sweep.
                await self._expire(tx, now, project_id=scope.project_id, email=str(old["email"]))
            account = await self._directory.profile_for_email(str(old["email"]))
            if account is not None and await repo.active_membership_for_user(
                tx, scope.project_id, account.user_id
            ):
                raise AlreadyExists(
                    details=[
                        FieldIssue(
                            field="email",
                            code="AlreadyMember",
                            message="This person is already a member.",
                        )
                    ]
                )
            row, credential = await self._issue(
                tx,
                scope,
                str(old["email"]),
                ProjectRole(old["role"]),
                now=now,
                resend_of=old["id"],
                event=InvitationEventKind.RESEND,
            )
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.INVITATION_RESENT,
                target=TargetRef("invitation", row["id"]),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
                facts=SafeFacts(previousInvitationId=old["id"], expiresAt=row["expires_at"]),
            )
            await self._send(tx, scope, str(old["email"]), credential)
        return (await self._views([row], now))[0]

    async def revoke(self, scope: ProjectScope, invitation_id: str, version: int) -> InvitationView:
        """Terminal. Only a Pending (unexpired) invitation can be revoked."""
        scope.require_writable()
        now = self._clock.now()
        async with self._uow() as tx:
            old = await self._locked(tx, scope, invitation_id, version, now)
            state = effective_state(InvitationState(old["state"]), old["expires_at"], now)
            if state is not InvitationState.PENDING:
                raise InvalidTransition(allowed_transitions=[])
            row = await repo.set_invitation_state(
                tx, old["id"], InvitationState.REVOKED, revoked_at=now
            )
            await repo.record_invitation_event(
                tx, old["id"], InvitationEventKind.REVOKE, at=now, actor_user_id=scope.user_id
            )
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.INVITATION_REVOKED,
                target=TargetRef("invitation", old["id"]),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
            )
        return (await self._views([row], now))[0]

    # ------------------------------------------------------------------ credential holders

    async def inspect(
        self, credential: str, actor: Actor | None, *, client_key: str
    ) -> InvitationContext:
        """What the invitation offers. Rate limited per client address and per actor."""
        keys = [f"ip:{client_key}"]
        if actor is not None:
            keys.append(f"actor:{actor.user_id or actor.subject}")
        self._limiter.hit(*keys)
        now = self._clock.now()
        async with self._uow() as tx:
            row, project = await self._resolve(tx, credential)
        state = effective_state(InvitationState(row["state"]), row["expires_at"], now)
        if state in _STATE_FAILURES:
            raise invitation_invalid(_STATE_FAILURES[state])
        inviter = (await self._directory.profiles([row["invited_by"]])).get(row["invited_by"])
        signed_in = actor.email if actor is not None else None
        return InvitationContext(
            project_name=project["name"],
            intended_role=ProjectRole(row["role"]),
            inviter_display_name=inviter.display_name if inviter else "A Project Administrator",
            invitee_email_masked=mask_email(str(row["email"])),
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            state=state,
            signed_in_email_matches=signed_in is not None
            and signed_in.lower() == str(row["email"]).lower(),
        )

    async def accept(
        self, credential: str, actor: Actor, identity: VerifiedIdentity
    ) -> AcceptanceResult:
        """Create (or reuse) the account and one active membership. Re-accepting with the
        same account returns the same result and changes nothing (IAM-06, PROJ-10)."""
        if not identity.email_verified or identity.email is None:
            raise AppError(ErrorCode.EMAIL_NOT_VERIFIED)
        now = self._clock.now()
        async with self._uow() as tx:
            row, _ = await self._resolve(tx, credential)
        replay = self._replay(row, actor)
        if replay is not None:
            return replay
        self._check_acceptable(row, now)
        if identity.email.lower() != str(row["email"]).lower():
            raise AppError(ErrorCode.INVITATION_EMAIL_MISMATCH)
        role = ProjectRole(row["role"])
        actor = await self._account_for(role, actor, identity)

        async with self._uow() as tx:
            row, _ = await self._resolve(tx, credential, for_update=True)
            replay = self._replay(row, actor)
            if replay is not None:
                return replay
            self._check_acceptable(row, now)
            if not actor.is_active:
                actor = await self._directory.activate_client_collaborator(tx, identity)
            assert actor.user_id is not None  # noqa: S101 - activated above
            project_id = row["project_id"]
            membership = await repo.active_membership_for_user(tx, project_id, actor.user_id)
            if membership is None:
                membership = await repo.insert_membership(
                    tx,
                    firm_id=self._firm_id,
                    project_id=project_id,
                    user_id=actor.user_id,
                    role=role,
                    created_by=row["invited_by"],
                    now=now,
                )
                await self._audit.record(
                    tx,
                    actor=actor.actor_ref,
                    action=AuditAction.MEMBERSHIP_ADDED,
                    target=TargetRef("membership", membership["id"]),
                    outcome=Outcome.SUCCEEDED,
                    project_id=project_id,
                    facts=SafeFacts(role=role.value),
                )
                await self._activity.record_for(
                    tx, self._firm_id, project_id, ActivityKind.MEMBERSHIP_CHANGED,
                    actor.user_id, membership["id"],
                )  # fmt: skip
                await repo.bump_authorization_version(tx, self._firm_id)
            await repo.set_invitation_state(
                tx, row["id"], InvitationState.ACCEPTED, accepted_at=now, accepted_by=actor.user_id
            )
            await repo.record_invitation_event(
                tx, row["id"], InvitationEventKind.ACCEPT, at=now, actor_user_id=actor.user_id
            )
            await self._audit.record(
                tx,
                actor=actor.actor_ref,
                action=AuditAction.INVITATION_ACCEPTED,
                target=TargetRef("invitation", row["id"]),
                outcome=Outcome.SUCCEEDED,
                project_id=project_id,
                facts=SafeFacts(role=role.value),
            )
            await self._notify_inviter(tx, row)
        return AcceptanceResult(project_id=project_id, role=role, actor=actor)

    # ------------------------------------------------------------------ sweep

    async def expire_due(self) -> int:
        """``projects.invitation_expire``: persist lazy expiry and record the events."""
        async with self._uow() as tx:
            return len(await self._expire(tx, self._clock.now()))

    # ------------------------------------------------------------------ internals

    async def _check_role_for_domain(self, address: str, role: ProjectRole) -> None:
        firm_domain = await self._directory.is_approved_domain(email_domain(address))
        if role in TEAM_ROLES and not firm_domain:
            raise ValidationFailed(
                [
                    FieldIssue(
                        field="role",
                        code="team_role_requires_firm_domain",
                        message="Team roles need an approved firm email domain.",
                    )
                ]
            )
        if role is ProjectRole.CLIENT_COLLABORATOR and firm_domain:
            raise ValidationFailed(
                [
                    FieldIssue(
                        field="role",
                        code="client_role_requires_external_domain",
                        message="Firm addresses are invited with a team role.",
                    )
                ]
            )

    async def _issue(
        self,
        tx: Tx,
        scope: ProjectScope,
        address: str,
        role: ProjectRole,
        *,
        now: datetime,
        resend_of: str | None,
        event: InvitationEventKind = InvitationEventKind.ISSUE,
    ) -> tuple[RowMapping, str]:
        credential = new_credential()
        try:
            async with tx.session.begin_nested():
                row = await repo.insert_invitation(
                    tx,
                    firm_id=self._firm_id,
                    project_id=scope.project_id,
                    email=address,
                    role=role,
                    invited_by=scope.user_id,
                    digest=credential_digest(credential),
                    now=now,
                    expires_at=now + INVITATION_TTL,
                    resend_of=resend_of,
                )
        except IntegrityError:  # a concurrent request issued one first
            raise AlreadyExists() from None
        await repo.record_invitation_event(
            tx, row["id"], event, at=now, actor_user_id=scope.user_id
        )
        return row, credential

    async def _send(self, tx: Tx, scope: ProjectScope, address: str, credential: str) -> None:
        """Last step of the command: everything else is written, so only the commit itself
        can still fail after the email has left. A provider failure rolls the invitation
        back (the plaintext credential cannot be stored or queued)."""
        project = await repo.project_row(tx, self._firm_id, scope.project_id)
        assert project is not None  # noqa: S101 - the scope proves it exists
        await self._notifications.send_invitation(
            to=address, project_name=project["name"], link=self._invite_url + credential
        )

    async def _locked(
        self, tx: Tx, scope: ProjectScope, invitation_id: str, version: int, now: datetime
    ) -> RowMapping:
        row = await repo.invitation(tx, scope.project_id, invitation_id, for_update=True)
        if row is None:
            raise NotAvailable()
        if row["version"] != version:
            raise VersionConflict((await self._views([row], now))[0])
        return row

    async def _resolve(
        self, tx: Tx, credential: str, *, for_update: bool = False
    ) -> tuple[RowMapping, RowMapping]:
        if not well_formed_credential(credential):
            raise invitation_invalid(InvitationFailure.UNKNOWN)
        row = await repo.invitation_by_digest(
            tx, self._firm_id, credential_digest(credential), for_update=for_update
        )
        if row is None:
            raise invitation_invalid(InvitationFailure.UNKNOWN)
        project = await repo.project_row(tx, self._firm_id, row["project_id"])
        if project is None or Lifecycle(project["lifecycle"]) is not Lifecycle.ACTIVE:
            raise invitation_invalid(InvitationFailure.PROJECT_UNAVAILABLE)
        return row, project

    def _replay(self, row: RowMapping, actor: Actor) -> AcceptanceResult | None:
        if (
            InvitationState(row["state"]) is InvitationState.ACCEPTED
            and actor.user_id is not None
            and row["accepted_by"] == actor.user_id
        ):
            return AcceptanceResult(
                project_id=row["project_id"], role=ProjectRole(row["role"]), actor=actor
            )
        return None

    def _check_acceptable(self, row: RowMapping, now: datetime) -> None:
        state = effective_state(InvitationState(row["state"]), row["expires_at"], now)
        if state in _STATE_FAILURES:
            raise invitation_invalid(_STATE_FAILURES[state])

    async def _account_for(
        self, role: ProjectRole, actor: Actor, identity: VerifiedIdentity
    ) -> Actor:
        """The person category must match the invited role; a Team Member invitation never
        confers Team Member status on an address outside the approved domains."""
        if role in TEAM_ROLES:
            if actor.is_active:
                if not actor.is_team_member:
                    raise AppError(ErrorCode.INVITATION_EMAIL_MISMATCH)
                return actor
            assert identity.email is not None  # noqa: S101 - checked by accept
            if not await self._directory.is_approved_domain(email_domain(identity.email)):
                raise AppError(ErrorCode.INVITATION_EMAIL_MISMATCH)
            return await self._directory.activate_employee(identity)
        if actor.is_active and actor.is_team_member:
            raise AppError(ErrorCode.INVITATION_EMAIL_MISMATCH)
        return actor

    async def _notify_inviter(self, tx: Tx, row: RowMapping) -> None:
        inviter = (await self._directory.profiles([row["invited_by"]])).get(row["invited_by"])
        if inviter is None or not inviter.active:
            return
        await self._notifications.enqueue(
            tx,
            recipient_user_id=inviter.user_id,
            trigger=NotificationTrigger.INVITATION_ACCEPTED,
            template_key="invitation.accepted",
            subject_ref=SafeSubjectRef("Project", row["project_id"]),
            safe_context={},
            project_id=row["project_id"],
        )

    async def _expire(
        self,
        tx: Tx,
        now: datetime,
        *,
        project_id: str | None = None,
        email: str | None = None,
    ) -> builtins.list[tuple[str, str]]:
        expired = await repo.expire_due_invitations(tx, now, project_id=project_id, email=email)
        for invitation_id, invitation_project in expired:
            await repo.record_invitation_event(
                tx, invitation_id, InvitationEventKind.EXPIRE, at=now, actor_user_id=None
            )
            await self._audit.record(
                tx,
                actor=SystemActor("Invitation expiry"),
                action=AuditAction.INVITATION_EXPIRED,
                target=TargetRef("invitation", invitation_id),
                outcome=Outcome.SUCCEEDED,
                project_id=invitation_project,
            )
        return expired

    async def _views(
        self, rows: builtins.list[RowMapping], now: datetime
    ) -> builtins.list[InvitationView]:
        profiles = await self._directory.profiles(r["invited_by"] for r in rows)
        views = []
        for row in rows:
            inviter = profiles.get(row["invited_by"])
            views.append(
                InvitationView(
                    invitation_id=row["id"],
                    email=str(row["email"]),
                    role=ProjectRole(row["role"]),
                    inviter=InviterView(
                        user_id=row["invited_by"],
                        display_name=inviter.display_name if inviter else "Deleted User",
                    ),
                    issued_at=row["issued_at"],
                    expires_at=row["expires_at"],
                    state=effective_state(InvitationState(row["state"]), row["expires_at"], now),
                    version=row["version"],
                )
            )
        return views
