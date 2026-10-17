"""``Membership``: the member list, role changes, removal, and people pickers (T06).

Membership is the only source of Project access, so every command re-validates the
final-administrator invariant under a row lock, bumps ``authorizationVersion``, records
``MembershipChanged`` for Latest Project Activity, and is audited.
"""

from __future__ import annotations

import builtins
import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal

from sqlalchemy import RowMapping

from app.modules.governance import Audit, AuditAction, Outcome, SafeFacts, TargetRef
from app.modules.organization_access import (
    AccountProfile,
    OrganizationAccess,
    PersonCategory,
)
from app.modules.projects import repository as repo
from app.modules.projects.authorization import ProjectScope
from app.modules.projects.domain import (
    TEAM_ROLES,
    ActivityKind,
    Capability,
    ProjectRole,
    role_change,
)
from app.modules.projects.service import ProjectActivity
from app.modules.projects.tables import memberships as memberships_table
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork, optimistic_update
from app.platform.errors import (
    AppError,
    ErrorCode,
    FieldIssue,
    NotAvailable,
    ValidationFailed,
    VersionConflict,
)
from app.platform.http.pagination import CursorCodec, Page
from app.platform.schema import CamelModel, OmitWhenNone

# --------------------------------------------------------------------------- views


class MembershipView(CamelModel):
    """Contract §6.3. Never a pseudonym or Client Identity Mapping value (PRV-11)."""

    membership_id: str
    user_id: str
    display_name: str
    email: str
    person_category: PersonCategory
    role: ProjectRole
    joined_at: datetime
    version: int


class RoleChangePreview(OmitWhenNone):
    omit_when_none = frozenset({"blocked"})

    gained: list[Capability]
    lost: list[Capability]
    blocked: Literal["FINAL_ADMINISTRATOR"] | None = None


class PeoplePurpose(StrEnum):
    TASK_ASSIGNEE = "TaskAssignee"
    TASK_WATCHER = "TaskWatcher"
    ACTION_ITEM_RESPONSIBLE = "ActionItemResponsible"
    PARTICIPANT_FILTER = "ParticipantFilter"


PEOPLE_CAPABILITY: Final = {
    PeoplePurpose.TASK_ASSIGNEE: Capability.TASK_ADMINISTER,
    PeoplePurpose.TASK_WATCHER: Capability.TASK_ADMINISTER,
    PeoplePurpose.ACTION_ITEM_RESPONSIBLE: Capability.KNOWLEDGE_REVIEW,
    PeoplePurpose.PARTICIPANT_FILTER: Capability.SEARCH,
}


def people_capability(purpose: PeoplePurpose) -> Capability:
    """The capability a picker requires; the route asks ``ProjectAccess`` for it."""
    return PEOPLE_CAPABILITY[purpose]


class PersonRef(OmitWhenNone):
    """Contract §3 ``PersonRef`` plus the picker's ``eligibleForInternal`` (§6.5).

    Account-space and Evidence-space ids are derived under different keys, so the two
    spaces are disjoint and a ref in one never joins to the other.
    """

    omit_when_none = frozenset({"user_id", "eligible_for_internal"})

    person_ref_id: str
    space: Literal["Account", "Evidence"]
    kind: Literal["TeamMember", "ClientCollaborator", "UnknownPerson", "DeletedUser"]
    display_label: str
    user_id: str | None = None
    eligible_for_internal: bool | None = None


_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def person_ref_id(key: bytes, space: str, project_id: str, user_id: str) -> str:
    """``ppl_…`` stable within one Project and space, unlinkable across them."""
    digest = hmac.new(key, f"{space}|{project_id}|{user_id}".encode(), hashlib.sha256).digest()
    number = int.from_bytes(digest[:17], "big") >> 6  # 130 bits = 26 base32 characters
    chars: list[str] = []
    for _ in range(26):
        chars.append(_CROCKFORD[number & 31])
        number >>= 5
    return "ppl_" + "".join(reversed(chars))


# --------------------------------------------------------------------------- service


class Membership:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        audit: Audit,
        activity: ProjectActivity,
        directory: OrganizationAccess,
        cursors: CursorCodec,
        person_ref_key: bytes,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._audit = audit
        self._activity = activity
        self._directory = directory
        self._cursors = cursors
        self._ref_key = person_ref_key

    async def list(
        self, scope: ProjectScope, cursor: str | None, *, limit: int = 25
    ) -> Page[MembershipView]:
        """``membership.manage``: every active member, by join time."""
        after = None
        if cursor is not None:
            decoded = self._cursors.decode(cursor)
            if not isinstance(decoded.k, str):
                raise ValidationFailed(
                    [FieldIssue(field="cursor", code="invalid", message="Invalid cursor.")]
                )
            try:
                after = (datetime.fromisoformat(decoded.k), decoded.id)
            except ValueError:
                raise ValidationFailed(
                    [FieldIssue(field="cursor", code="invalid", message="Invalid cursor.")]
                ) from None
        async with self._uow() as tx:
            rows = await repo.page_active_members(
                tx, scope.project_id, after=after, limit=limit + 1
            )
        page = rows[:limit]
        profiles = await self._directory.profiles(r["user_id"] for r in page)
        items = [
            _membership_view(r, profiles[r["user_id"]]) for r in page if r["user_id"] in profiles
        ]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = self._cursors.encode(last["active_from"].isoformat(), last["id"])
        return Page[MembershipView](items=items, next_cursor=next_cursor)

    async def role_change_preview(
        self, scope: ProjectScope, membership_id: str, role: ProjectRole
    ) -> RoleChangePreview:
        _require_team_role(role)
        async with self._uow() as tx:
            row = await repo.active_membership(tx, scope.project_id, membership_id)
            if row is None:
                raise NotAvailable()
            current = ProjectRole(row["role"])
            _require_team_member_row(current)
            admins = await repo.administrator_user_ids(tx, scope.project_id)
        change = role_change(current, role)
        blocked = (
            current is ProjectRole.ADMINISTRATOR
            and role is not ProjectRole.ADMINISTRATOR
            and len(admins) <= 1
        )
        return RoleChangePreview(
            gained=change.gained,
            lost=change.lost,
            blocked="FINAL_ADMINISTRATOR" if blocked else None,
        )

    async def change_role(
        self, scope: ProjectScope, membership_id: str, role: ProjectRole, version: int
    ) -> MembershipView:
        """Applies immediately; the final active Administrator cannot be demoted."""
        _require_team_role(role)
        scope.require_writable()
        async with self._uow() as tx:
            await repo.lock_writable_project(tx, scope.project_id)
            admins = await repo.lock_administrators(tx, scope.project_id)
            row = await repo.active_membership(tx, scope.project_id, membership_id, for_update=True)
            if row is None:
                raise NotAvailable()
            current = ProjectRole(row["role"])
            _require_team_member_row(current)
            if row["version"] != version:
                raise VersionConflict(await self._view(row))
            if current is role:
                return await self._view(row)
            if current is ProjectRole.ADMINISTRATOR and len(admins) <= 1:
                raise AppError(ErrorCode.FINAL_ADMINISTRATOR)
            updated = await self._update(
                tx, row, version, {"role": role.value, "updated_at": self._clock.now()}
            )
            await self._membership_changed(tx, scope, membership_id)
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.MEMBERSHIP_ROLE_CHANGED,
                target=TargetRef("membership", membership_id),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
                facts=SafeFacts(fromRole=current.value, toRole=role.value),
            )
        return await self._view(updated)

    async def remove(self, scope: ProjectScope, membership_id: str, version: int) -> None:
        """Deactivates (never deletes) the membership, so history and attribution survive.
        Content the member authored is untouched."""
        scope.require_writable()
        async with self._uow() as tx:
            await repo.lock_writable_project(tx, scope.project_id)
            admins = await repo.lock_administrators(tx, scope.project_id)
            row = await repo.active_membership(tx, scope.project_id, membership_id, for_update=True)
            if row is None:
                raise NotAvailable()
            if row["version"] != version:
                raise VersionConflict(await self._view(row))
            role = ProjectRole(row["role"])
            if role is ProjectRole.ADMINISTRATOR and len(admins) <= 1:
                raise AppError(
                    ErrorCode.FINAL_ADMINISTRATOR,
                    message="A Project must keep at least one Administrator. "
                    "Make another member an Administrator first.",
                )
            now = self._clock.now()
            await self._update(tx, row, version, {"active_to": now, "updated_at": now})
            await self._membership_changed(tx, scope, membership_id)
            await self._audit.record(
                tx,
                actor=scope.actor.actor_ref,
                action=AuditAction.MEMBERSHIP_REMOVED,
                target=TargetRef("membership", membership_id),
                outcome=Outcome.SUCCEEDED,
                project_id=scope.project_id,
                facts=SafeFacts(role=role.value),
            )

    async def people(self, scope: ProjectScope, purpose: PeoplePurpose) -> builtins.list[PersonRef]:
        """Picker contents. The caller's scope carries ``people_capability(purpose)``."""
        if purpose is PeoplePurpose.PARTICIPANT_FILTER and scope.is_client_collaborator:
            raise NotAvailable()
        async with self._uow() as tx:
            rows = await repo.active_members(tx, scope.project_id)
        profiles = await self._directory.profiles(r["user_id"] for r in rows)
        members = [
            (row, profiles[row["user_id"]])
            for row in rows
            if row["user_id"] in profiles and profiles[row["user_id"]].active
        ]
        members.sort(key=lambda m: (m[1].display_name.lower(), m[0]["user_id"]))
        refs: builtins.list[PersonRef] = []
        for row, profile in members:
            client = ProjectRole(row["role"]) is ProjectRole.CLIENT_COLLABORATOR
            if purpose is PeoplePurpose.PARTICIPANT_FILTER:
                # Evidence space. Client Collaborator pseudonyms and Unknown Persons come from
                # the privacy module's evidence index (T11); account names never appear here.
                if client:
                    continue
                refs.append(self._ref(scope, "Evidence", profile, client=False))
            elif purpose is PeoplePurpose.ACTION_ITEM_RESPONSIBLE:
                if client:
                    continue
                refs.append(self._ref(scope, "Account", profile, client=False, eligible=True))
            else:
                refs.append(
                    self._ref(scope, "Account", profile, client=client, eligible=not client)
                )
        return refs

    # ------------------------------------------------------------------ internals

    def _ref(
        self,
        scope: ProjectScope,
        space: Literal["Account", "Evidence"],
        profile: AccountProfile,
        *,
        client: bool,
        eligible: bool | None = None,
    ) -> PersonRef:
        return PersonRef(
            person_ref_id=person_ref_id(self._ref_key, space, scope.project_id, profile.user_id),
            space=space,
            kind="ClientCollaborator" if client else "TeamMember",
            display_label=profile.display_name,
            user_id=None if client or scope.is_client_collaborator else profile.user_id,
            eligible_for_internal=eligible,
        )

    async def _update(
        self, tx: Tx, row: RowMapping, version: int, values: dict[str, object]
    ) -> RowMapping:
        try:
            return await optimistic_update(tx, memberships_table, row["id"], version, values)
        except VersionConflict as conflict:
            current = conflict.row if conflict.row is not None else row
            raise VersionConflict(await self._view(current)) from None

    async def _membership_changed(self, tx: Tx, scope: ProjectScope, membership_id: str) -> None:
        await self._activity.record(tx, scope, ActivityKind.MEMBERSHIP_CHANGED, membership_id)
        await repo.bump_authorization_version(tx, scope.firm_id)

    async def _view(self, row: Mapping[Any, Any]) -> MembershipView:
        profiles = await self._directory.profiles([row["user_id"]])
        profile = profiles.get(row["user_id"])
        if profile is None:
            raise NotAvailable()
        return _membership_view(row, profile)


def _membership_view(row: Mapping[Any, Any], profile: AccountProfile) -> MembershipView:
    return MembershipView(
        membership_id=row["id"],
        user_id=row["user_id"],
        display_name=profile.display_name,
        email=profile.email,
        person_category=profile.person_category,
        role=ProjectRole(row["role"]),
        joined_at=row["active_from"],
        version=row["version"],
    )


def _require_team_role(role: ProjectRole) -> None:
    if role not in TEAM_ROLES:
        raise ValidationFailed(
            [
                FieldIssue(
                    field="role",
                    code="team_role_required",
                    message="Choose Administrator, Contributor, or Viewer.",
                )
            ]
        )


def _require_team_member_row(current: ProjectRole) -> None:
    """A Client Collaborator's role never changes to a Team Member role (and vice versa)."""
    if current is ProjectRole.CLIENT_COLLABORATOR:
        raise ValidationFailed(
            [
                FieldIssue(
                    field="role",
                    code="client_collaborator_role_fixed",
                    message="A Client Collaborator's role cannot be changed.",
                )
            ]
        )
