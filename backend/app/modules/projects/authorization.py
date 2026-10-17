"""``ProjectAccess``: the single place that decides whether an actor may do something in
one or more Projects, and the only minter of the trusted scope objects every repository
requires (T05, conventions §4.1).

Authentication never grants Project access: only an active membership does. Missing,
non-member, Pending Deletion, and Deleted Projects all raise the same ``NotAvailable``
(non-enumeration, NAV-08, SYS-04).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import RowMapping

from app.modules.governance import (
    Audit,
    AuditAction,
    Destination,
    Outcome,
    ProjectLabel,
    SafeFacts,
    TargetRef,
)
from app.modules.organization_access import (
    Actor,
    AuditRateLimiter,
    GlobalCapability,
    authorization_version,
    global_capabilities,
)
from app.modules.projects import repository as repo
from app.modules.projects.domain import (
    ACTIVE,
    ARCHIVED_CAPABILITIES,
    RECOVERY_CAPABILITIES,
    VISIBLE_LIFECYCLES,
    AccessPurpose,
    Capability,
    Landing,
    Lifecycle,
    ProjectRole,
    Visibility,
    capabilities_for,
    landing_for,
    role_holds,
    visibility_for,
)
from app.platform.clock import Clock
from app.platform.db import UnitOfWork
from app.platform.errors import (
    CapabilityDenied,
    ErrorCode,
    NotAvailable,
    ProjectReadOnly,
    ScopeInvalid,
)

_MINT: Final = object()
_PROJECT_ID = re.compile(r"^prj_[0-9A-HJKMNP-TV-Z]{26}$")


def is_project_id(value: str) -> bool:
    return bool(_PROJECT_ID.match(value))


# --------------------------------------------------------------------------- trusted scopes


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """Proof that ``actor`` holds an active membership in ``project_id`` and passed a
    capability check. Only ``ProjectAccess`` can construct one (sentinel token)."""

    firm_id: str
    project_id: str
    role: ProjectRole
    lifecycle: Lifecycle
    visibility: frozenset[Visibility]
    actor: Actor
    membership_id: str
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _MINT:
            raise TypeError("ProjectScope can only be minted by ProjectAccess")

    @property
    def user_id(self) -> str:
        assert self.actor.user_id is not None  # noqa: S101 - minted for activated actors only
        return self.actor.user_id

    @property
    def is_client_collaborator(self) -> bool:
        return self.role is ProjectRole.CLIENT_COLLABORATOR

    def allows(self, capability: Capability) -> bool:
        return capability in capabilities_for(self.role, self.lifecycle)

    def require_writable(self) -> None:
        """Commands that change Project state on a capability Archived Projects keep for
        reading (``membership.manage``) call this: Archived -> ``409 PROJECT_READ_ONLY``."""
        if self.lifecycle is not Lifecycle.ACTIVE:
            raise ProjectReadOnly()


@dataclass(frozen=True, slots=True)
class ScopeSet:
    """A normalized Project set: sorted by ``project_id`` and duplicate-free. Non-empty when
    it comes from ``require_many``; ``authorized_projects`` may return an empty set."""

    scopes: tuple[ProjectScope, ...]
    scope_key: str
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _MINT:
            raise TypeError("ScopeSet can only be minted by ProjectAccess")

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(s.project_id for s in self.scopes)

    def __len__(self) -> int:
        return len(self.scopes)


def scope_key(project_ids: Iterable[str]) -> str:
    """Stable hash of the sorted, de-duplicated Project ids (selection order never matters)."""
    material = ",".join(sorted(set(project_ids)))
    return hashlib.sha256(material.encode("ascii")).hexdigest()[:32]


def _mint_set(scopes: Iterable[ProjectScope]) -> ScopeSet:
    ordered = tuple(sorted(scopes, key=lambda s: s.project_id))
    return ScopeSet(
        scopes=ordered, scope_key=scope_key(s.project_id for s in ordered), _token=_MINT
    )


# --------------------------------------------------------------------------- summary for /me


@dataclass(frozen=True, slots=True)
class ProjectMembershipSummary:
    project_id: str
    name: str
    role: ProjectRole
    lifecycle: Lifecycle
    capabilities: list[Capability]
    latest_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class AccessSummary:
    projects: list[ProjectMembershipSummary]
    global_capabilities: list[GlobalCapability]
    landing: Landing
    authorization_version: str


# --------------------------------------------------------------------------- the module


class ProjectAccess:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        firm_id: str,
        audit: Audit,
        denial_limiter: AuditRateLimiter | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._firm_id = firm_id
        self._audit = audit
        self._limiter = denial_limiter or AuditRateLimiter(clock)

    async def require(
        self,
        actor: Actor,
        project_id: str,
        capability: Capability,
        *,
        purpose: AccessPurpose = AccessPurpose.ORDINARY,
    ) -> ProjectScope:
        """A scope for one Project, or ``NotAvailable`` / ``CapabilityDenied`` /
        ``ProjectReadOnly``. Denied attempts by members are audited (``permission.denied``)."""
        scope, _ = await self._check(actor, project_id, capability, purpose, audit=True)
        return scope

    async def require_many(
        self, actor: Actor, project_ids: Sequence[str], capability: Capability
    ) -> ScopeSet:
        """Every id must be an Active Project where the actor holds ``capability``; any
        failure fails the whole request with ``NotAvailable`` — never a silently narrowed
        scope (KNW-09, SRCH-02, CHAT-13). Empty or malformed input is ``SCOPE_INVALID``."""
        normalized = sorted({pid.strip() for pid in project_ids})
        if not normalized or not all(is_project_id(pid) for pid in normalized):
            raise ScopeInvalid()
        if not actor.is_active or actor.user_id is None:
            raise NotAvailable()
        async with self._uow() as tx:
            rows = await repo.access_rows(tx, self._firm_id, actor.user_id, normalized)
        if len(rows) != len(normalized):
            raise NotAvailable()
        scopes = []
        for row in rows:
            if row["membership_id"] is None or Lifecycle(row["lifecycle"]) is not Lifecycle.ACTIVE:
                raise NotAvailable()
            role = ProjectRole(row["role"])
            if not role_holds(role, capability):
                raise NotAvailable()
            scopes.append(self._mint(actor, row, role))
        return _mint_set(scopes)

    async def authorized_projects(
        self, actor: Actor, capability: Capability, *, lifecycles: frozenset[Lifecycle] = ACTIVE
    ) -> ScopeSet:
        """Every Project in ``lifecycles`` where the actor may use ``capability`` now.
        Pending Deletion and Deleted Projects are never included."""
        if not actor.is_active or actor.user_id is None:
            return _mint_set(())
        wanted = lifecycles & VISIBLE_LIFECYCLES
        async with self._uow() as tx:
            rows = await repo.member_rows(tx, self._firm_id, actor.user_id, wanted)
        scopes = []
        for row in rows:
            role = ProjectRole(row["role"])
            if capability in capabilities_for(role, Lifecycle(row["lifecycle"])):
                scopes.append(self._mint(actor, row, role))
        return _mint_set(scopes)

    async def summary(self, actor: Actor) -> AccessSummary:
        """``GET /me``: Active and Archived memberships, landing, authorization version."""
        rows: list[RowMapping] = []
        counter = 0
        async with self._uow() as tx:
            if actor.is_active and actor.user_id is not None:
                rows = await repo.member_rows(tx, self._firm_id, actor.user_id, VISIBLE_LIFECYCLES)
            counter = await repo.authorization_counter(tx, self._firm_id)
        entries = []
        for row in rows:
            role = ProjectRole(row["role"])
            lifecycle = Lifecycle(row["lifecycle"])
            entries.append(
                ProjectMembershipSummary(
                    project_id=row["id"],
                    name=row["name"],
                    role=role,
                    lifecycle=lifecycle,
                    capabilities=capabilities_for(role, lifecycle),
                    latest_activity_at=row["latest_activity_at"],
                )
            )
        active_ids = [e.project_id for e in entries if e.lifecycle is Lifecycle.ACTIVE]
        landing = landing_for(
            activated=actor.is_active,
            team_member=actor.is_team_member,
            firm_manager=actor.is_active and actor.firm_manager,
            active_project_ids=active_ids,
            archived_count=len(entries) - len(active_ids),
        )
        digest = hashlib.sha256(
            f"{authorization_version(actor)}|{counter}".encode("ascii")
        ).hexdigest()[:16]
        return AccessSummary(
            projects=entries,
            global_capabilities=global_capabilities(actor),
            landing=landing,
            authorization_version=digest,
        )

    # ------------------------------------------------------------------ silent checks

    async def check(
        self,
        actor: Any,
        project_id: str,
        capability: Capability,
        purpose: AccessPurpose = AccessPurpose.ORDINARY,
    ) -> tuple[ProjectScope, str] | None:
        """``require`` without auditing or raising: ``(scope, project name)`` or ``None``.
        For reauthorization reads (operations, notifications) that degrade silently."""
        if not isinstance(actor, Actor):
            return None
        try:
            return await self._check(actor, project_id, capability, purpose, audit=False)
        except (NotAvailable, CapabilityDenied, ProjectReadOnly):
            return None

    # ------------------------------------------------------------------ internals

    async def _check(
        self,
        actor: Actor,
        project_id: str,
        capability: Capability,
        purpose: AccessPurpose,
        *,
        audit: bool,
    ) -> tuple[ProjectScope, str]:
        if not actor.is_active or actor.user_id is None or not is_project_id(project_id):
            raise NotAvailable()
        async with self._uow() as tx:
            row = await repo.access_row(tx, self._firm_id, actor.user_id, project_id)
        if row is None:
            raise NotAvailable()
        lifecycle = Lifecycle(row["lifecycle"])
        if row["membership_id"] is None:
            if audit and lifecycle in VISIBLE_LIFECYCLES:
                await self._deny(actor, project_id, capability, ErrorCode.NOT_AVAILABLE)
            raise NotAvailable()
        role = ProjectRole(row["role"])
        if lifecycle is Lifecycle.DELETED:
            raise NotAvailable()
        if lifecycle is Lifecycle.PENDING_DELETION:
            if (
                purpose is AccessPurpose.RECOVERY
                and role is ProjectRole.ADMINISTRATOR
                and capability in RECOVERY_CAPABILITIES
            ):
                return self._mint(actor, row, role), row["name"]
            raise NotAvailable()
        if not role_holds(role, capability):
            # A Client Collaborator never learns that an internal capability exists.
            code = (
                ErrorCode.NOT_AVAILABLE
                if role is ProjectRole.CLIENT_COLLABORATOR
                else ErrorCode.CAPABILITY_DENIED
            )
            if audit:
                await self._deny(actor, project_id, capability, code)
            if code is ErrorCode.NOT_AVAILABLE:
                raise NotAvailable()
            raise CapabilityDenied()
        if lifecycle is Lifecycle.ARCHIVED and capability not in ARCHIVED_CAPABILITIES:
            raise ProjectReadOnly()
        return self._mint(actor, row, role), row["name"]

    def _mint(self, actor: Actor, row: RowMapping, role: ProjectRole) -> ProjectScope:
        return ProjectScope(
            firm_id=row["firm_id"],
            project_id=row["id"],
            role=role,
            lifecycle=Lifecycle(row["lifecycle"]),
            visibility=visibility_for(role),
            actor=actor,
            membership_id=row["membership_id"],
            _token=_MINT,
        )

    async def _deny(
        self, actor: Actor, project_id: str, capability: Capability, code: ErrorCode
    ) -> None:
        """``permission.denied`` with the capability and Project id — never the object the
        actor tried to reach. Rate-limited per actor, Project, and capability."""
        if not self._limiter.allow(f"denied:{actor.user_id}:{project_id}:{capability.value}"):
            return
        async with self._uow() as tx:
            await self._audit.record(
                tx,
                actor=actor.actor_ref,
                action=AuditAction.PERMISSION_DENIED,
                target=TargetRef("project", project_id),
                outcome=Outcome.DENIED,
                project_id=project_id,
                facts=SafeFacts(capability=capability.value, errorCode=code.value),
            )


class ProjectAccessAdapter:
    """``OperationAccess`` (governance) and ``DestinationAccess`` (actions) over
    ``ProjectAccess``: every read of an operation or notification is reauthorized here."""

    def __init__(self, access: ProjectAccess) -> None:
        self._access = access

    async def holds(self, actor: Any, capability: str, project_id: str | None) -> bool:
        if project_id is None:
            return False
        try:
            wanted = Capability(capability)
        except ValueError:
            return False
        for purpose in (AccessPurpose.ORDINARY, AccessPurpose.RECOVERY):
            if await self._access.check(actor, project_id, wanted, purpose) is not None:
                return True
        return False

    async def destination_authorized(self, actor: Any, destination: Destination) -> bool:
        if destination.project_id is None:
            return True
        return await self.holds(actor, Capability.PROJECT_READ.value, destination.project_id)

    async def project_label(self, actor: Any, project_id: str) -> ProjectLabel | None:
        checked = await self._access.check(actor, project_id, Capability.PROJECT_READ)
        if checked is None:
            return None
        return ProjectLabel(project_id=project_id, name=checked[1])
