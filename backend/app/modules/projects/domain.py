"""Pure projects rules: roles, lifecycles, the capability matrix, landing, Project codes,
archival candidacy, invitation credentials, and the lifecycle state machine.

The role → capability table lives here and nowhere else (T05). Nothing here performs I/O.
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Final

# --------------------------------------------------------------------------- vocabulary


class ProjectRole(StrEnum):
    ADMINISTRATOR = "Administrator"
    CONTRIBUTOR = "Contributor"
    VIEWER = "Viewer"
    CLIENT_COLLABORATOR = "ClientCollaborator"


TEAM_ROLES: Final = frozenset(
    {ProjectRole.ADMINISTRATOR, ProjectRole.CONTRIBUTOR, ProjectRole.VIEWER}
)


class Lifecycle(StrEnum):
    ACTIVE = "Active"
    ARCHIVED = "Archived"
    PENDING_DELETION = "PendingDeletion"
    DELETED = "Deleted"


ACTIVE: Final = frozenset({Lifecycle.ACTIVE})
VISIBLE_LIFECYCLES: Final = frozenset({Lifecycle.ACTIVE, Lifecycle.ARCHIVED})


class Visibility(StrEnum):
    INTERNAL = "Internal"
    CLIENT_VISIBLE = "ClientVisible"


class AccessPurpose(StrEnum):
    """Why a scope is requested.

    ``RECOVERY`` lets an Administrator reach a Pending Deletion Project (recovery list,
    cancellation, deletion report). ``GOVERNANCE`` is reserved for Firm Manager reads that
    never need a scope (T07's deletion report, T30); ``require`` treats it exactly like
    ``ORDINARY`` because Firm Manager status never bypasses membership.
    """

    ORDINARY = "Ordinary"
    RECOVERY = "Recovery"
    GOVERNANCE = "Governance"


class Capability(StrEnum):
    """Exactly the list in contract §3."""

    PROJECT_READ = "project.read"
    PROJECT_UPDATE = "project.update"
    MEMBERSHIP_MANAGE = "membership.manage"
    LIFECYCLE_MANAGE = "lifecycle.manage"
    DOCUMENT_UPLOAD = "document.upload"
    SOURCE_CONNECT = "source.connect"
    SOURCE_DISABLE = "source.disable"
    INGESTION_VIEW = "ingestion.view"
    REVIEW_PSEUDONYMIZATION = "review.pseudonymization"
    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_REVIEW = "knowledge.review"
    VISIBILITY_CHANGE = "visibility.change"
    SEARCH = "search"
    CHAT = "chat"
    TIMELINE_READ = "timeline.read"
    NETWORK_READ = "network.read"
    WORKLOAD_READ = "workload.read"
    RECOMMENDATION_READ = "recommendation.read"
    RECOMMENDATION_REVIEW = "recommendation.review"
    TASK_BOARD_READ = "task.board.read"
    TASK_ADMINISTER = "task.administer"
    TASK_REOPEN = "task.reopen"
    TASK_OWN_UPDATE = "task.own.update"
    PLANNER_CONNECT = "planner.connect"
    PLANNER_DRAFT = "planner.draft"
    AUDIT_PROJECT_READ = "audit.project.read"


# --------------------------------------------------------------------------- capability matrix

_C = Capability
_R = ProjectRole
_ALL = frozenset(ProjectRole)
_TEAM = TEAM_ROLES
_EDITORS = frozenset({_R.ADMINISTRATOR, _R.CONTRIBUTOR})
_ADMIN = frozenset({_R.ADMINISTRATOR})
_UPLOADERS = frozenset({_R.ADMINISTRATOR, _R.CONTRIBUTOR, _R.CLIENT_COLLABORATOR})
_OWN_UPDATE = frozenset({_R.ADMINISTRATOR, _R.CONTRIBUTOR, _R.CLIENT_COLLABORATOR})

# Capability -> roles that hold it (T05 matrix). Client Collaborators hold read capabilities
# for ClientVisible content only; the restriction travels as ``ProjectScope.visibility``.
CAPABILITY_MATRIX: Final[Mapping[Capability, frozenset[ProjectRole]]] = {
    _C.PROJECT_READ: _ALL,
    _C.KNOWLEDGE_READ: _ALL,
    _C.SEARCH: _ALL,
    _C.CHAT: _ALL,
    _C.TIMELINE_READ: _ALL,
    _C.DOCUMENT_UPLOAD: _UPLOADERS,
    _C.SOURCE_CONNECT: _EDITORS,
    _C.SOURCE_DISABLE: _ADMIN,
    _C.INGESTION_VIEW: _ALL,  # Client Collaborators: own uploads only (T09 filters)
    _C.REVIEW_PSEUDONYMIZATION: _EDITORS,
    _C.KNOWLEDGE_REVIEW: _EDITORS,
    _C.VISIBILITY_CHANGE: _EDITORS,
    _C.RECOMMENDATION_REVIEW: _EDITORS,
    _C.NETWORK_READ: _TEAM,
    _C.WORKLOAD_READ: _TEAM,
    _C.RECOMMENDATION_READ: _TEAM,
    _C.TASK_BOARD_READ: _TEAM,
    _C.TASK_ADMINISTER: _EDITORS,
    _C.TASK_REOPEN: _ADMIN,
    _C.TASK_OWN_UPDATE: _OWN_UPDATE,  # Client Collaborators: eligible tasks only (T25)
    _C.MEMBERSHIP_MANAGE: _ADMIN,
    _C.LIFECYCLE_MANAGE: _ADMIN,
    _C.AUDIT_PROJECT_READ: _ADMIN,
    _C.PROJECT_UPDATE: _ADMIN,
    _C.PLANNER_CONNECT: _ADMIN,
    _C.PLANNER_DRAFT: _EDITORS,
}

# Capabilities that remain usable while a Project is Archived (read-only, PRJ-08, LIFE-11).
# ``lifecycle.manage`` stays so an Administrator can restore or delete; ``membership.manage``
# stays for reading the member list — membership *writes* call ``ProjectScope.require_writable``.
ARCHIVED_CAPABILITIES: Final = frozenset(
    {
        _C.PROJECT_READ,
        _C.KNOWLEDGE_READ,
        _C.SEARCH,
        _C.TIMELINE_READ,
        _C.INGESTION_VIEW,
        _C.NETWORK_READ,
        _C.WORKLOAD_READ,
        _C.RECOMMENDATION_READ,
        _C.TASK_BOARD_READ,
        _C.AUDIT_PROJECT_READ,
        _C.MEMBERSHIP_MANAGE,
        _C.LIFECYCLE_MANAGE,
    }
)

# Pending Deletion under AccessPurpose.RECOVERY: Administrators only, these capabilities only.
RECOVERY_CAPABILITIES: Final = frozenset({_C.PROJECT_READ, _C.LIFECYCLE_MANAGE})


def role_holds(role: ProjectRole, capability: Capability) -> bool:
    return role in CAPABILITY_MATRIX[capability]


def capabilities_for(role: ProjectRole, lifecycle: Lifecycle) -> list[Capability]:
    """What the role may do now, in contract order (drives ``Me`` and ``Project``)."""
    if lifecycle is Lifecycle.ACTIVE:
        return [c for c in Capability if role_holds(role, c)]
    if lifecycle is Lifecycle.ARCHIVED:
        return [c for c in Capability if role_holds(role, c) and c in ARCHIVED_CAPABILITIES]
    if lifecycle is Lifecycle.PENDING_DELETION and role is ProjectRole.ADMINISTRATOR:
        return [c for c in Capability if c in RECOVERY_CAPABILITIES]
    return []


def visibility_for(role: ProjectRole) -> frozenset[Visibility]:
    if role is ProjectRole.CLIENT_COLLABORATOR:
        return frozenset({Visibility.CLIENT_VISIBLE})
    return frozenset(Visibility)


@dataclass(frozen=True, slots=True)
class RoleChange:
    gained: list[Capability]
    lost: list[Capability]


def role_change(current: ProjectRole, target: ProjectRole) -> RoleChange:
    """Capabilities gained and lost moving between roles on an Active Project."""
    before = set(capabilities_for(current, Lifecycle.ACTIVE))
    after = set(capabilities_for(target, Lifecycle.ACTIVE))
    return RoleChange(
        gained=[c for c in Capability if c in after - before],
        lost=[c for c in Capability if c in before - after],
    )


# --------------------------------------------------------------------------- activity


class ActivityKind(StrEnum):
    """The only events that move ``latest_activity_at`` (PRJ-05). Views, searches, chats,
    failed imports, and background reprocessing are deliberately absent."""

    DOCUMENT_ACCEPTED = "DocumentAccepted"
    IMPORT_SUCCEEDED = "ImportSucceeded"
    MEMBERSHIP_CHANGED = "MembershipChanged"
    ADMINISTRATOR_MARKED_ACTIVE = "AdministratorMarkedActive"


# --------------------------------------------------------------------------- landing


class LandingKind(StrEnum):
    ACTIVATION = "Activation"
    HOME = "Home"
    PROJECT = "Project"
    PROJECT_CHOOSER = "ProjectChooser"
    ZERO_STATE = "ZeroState"


@dataclass(frozen=True, slots=True)
class Landing:
    kind: LandingKind
    project_id: str | None = None
    can_create_project: bool = False


def landing_for(
    *,
    activated: bool,
    team_member: bool,
    firm_manager: bool,
    active_project_ids: Sequence[str],
    archived_count: int,
) -> Landing:
    """Post Sign In Routing (architecture): Team Members and Firm Managers land on Home;
    a Client Collaborator with exactly one Active Project lands in it, with several on the
    chooser; nobody with a Project lands on the zero state."""
    if not activated:
        return Landing(LandingKind.ACTIVATION)
    if firm_manager:
        return Landing(LandingKind.HOME)
    has_projects = bool(active_project_ids) or archived_count > 0
    if not has_projects:
        return Landing(LandingKind.ZERO_STATE, can_create_project=team_member)
    if team_member:
        return Landing(LandingKind.HOME)
    if len(active_project_ids) == 1:
        return Landing(LandingKind.PROJECT, project_id=active_project_ids[0])
    return Landing(LandingKind.PROJECT_CHOOSER)


# --------------------------------------------------------------------------- Project codes

_LETTERS = re.compile(r"[A-Za-z]+")


def code_prefix(name: str) -> str:
    """Three upper-case letters from the name: the first word's leading letters, topped up
    with the initials of later words ("Atlas Expansion" -> "ATL", "AI Ops" -> "AIO")."""
    words: list[str] = [w.upper() for w in _LETTERS.findall(name)]
    if not words:
        return "PRJ"
    prefix = words[0][:3]
    for word in words[1:]:
        if len(prefix) >= 3:
            break
        prefix += word[0]
    return (prefix + "XX")[:3] if len(prefix) < 3 else prefix


def project_code(name: str, sequence: int) -> str:
    return f"{code_prefix(name)}-{sequence:03d}"


# --------------------------------------------------------------------------- archival candidacy


def add_calendar_months(moment: datetime, months: int, zone: tzinfo) -> datetime:
    """``moment`` plus ``months`` calendar months, evaluated on the firm's local calendar.
    A day that does not exist in the target month clamps to its last day (31 Jan + 1 month
    = 28/29 Feb)."""
    local = moment.astimezone(zone)
    month_index = local.month - 1 + months
    year = local.year + month_index // 12
    month = month_index % 12 + 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day)


ARCHIVAL_INACTIVITY_MONTHS: Final = 2


def is_archival_candidate(
    latest_activity_at: datetime | None, created_at: datetime, now: datetime, zone: tzinfo
) -> bool:
    """PRJ-06: strictly more than two calendar months since the latest activity (creation
    counts when there has been none). Exactly two calendar months is not a candidate."""
    since = latest_activity_at or created_at
    return now > add_calendar_months(since, ARCHIVAL_INACTIVITY_MONTHS, zone)


# --------------------------------------------------------------------------- lifecycle


class LifecycleCommand(StrEnum):
    ARCHIVE = "Archive"
    RESTORE = "Restore"
    REQUEST_DELETION = "RequestDeletion"
    CANCEL_DELETION = "CancelDeletion"
    COMPLETE_DELETION = "CompleteDeletion"


TRANSITIONS: Final[Mapping[Lifecycle, Mapping[LifecycleCommand, Lifecycle]]] = {
    Lifecycle.ACTIVE: {
        LifecycleCommand.ARCHIVE: Lifecycle.ARCHIVED,
        LifecycleCommand.REQUEST_DELETION: Lifecycle.PENDING_DELETION,
    },
    Lifecycle.ARCHIVED: {LifecycleCommand.RESTORE: Lifecycle.ACTIVE},
    Lifecycle.PENDING_DELETION: {
        LifecycleCommand.CANCEL_DELETION: Lifecycle.ACTIVE,
        LifecycleCommand.COMPLETE_DELETION: Lifecycle.DELETED,
    },
    Lifecycle.DELETED: {},
}


def next_lifecycle(current: Lifecycle, command: LifecycleCommand) -> Lifecycle | None:
    return TRANSITIONS[current].get(command)


def allowed_commands(current: Lifecycle) -> list[str]:
    """For ``409 INVALID_TRANSITION``'s ``allowedTransitions`` (user commands only)."""
    return [c.value for c in TRANSITIONS[current] if c is not LifecycleCommand.COMPLETE_DELETION]


class ScheduleState(StrEnum):
    SCHEDULED = "Scheduled"
    EXECUTED = "Executed"
    CANCELLED = "Cancelled"


class DeletionState(StrEnum):
    PENDING = "Pending"
    EXECUTED = "Executed"
    CANCELLED = "Cancelled"


class WorkItemState(StrEnum):
    PENDING = "Pending"
    RUNNING = "Running"
    RETRYING = "Retrying"
    FAILED = "Failed"
    COMPLETED = "Completed"


DELETION_RECOVERY_WINDOW: Final = timedelta(days=30)
ARCHIVAL_NOTICE_PERIOD: Final = timedelta(days=7)


def confirmation_matches(confirmation_name: str, project_name: str) -> bool:
    """Exact, case-sensitive match after trimming surrounding whitespace (PRJ-09)."""
    return hmac.compare_digest(
        confirmation_name.strip().encode("utf-8"), project_name.strip().encode("utf-8")
    )


# --------------------------------------------------------------------------- invitations


class InvitationState(StrEnum):
    PENDING = "Pending"
    ACCEPTED = "Accepted"
    EXPIRED = "Expired"
    REVOKED = "Revoked"


class InvitationFailure(StrEnum):
    """``details[0].code`` of ``422 INVITATION_INVALID`` (contract §2)."""

    UNKNOWN = "Unknown"
    EXPIRED = "Expired"
    REVOKED = "Revoked"
    ACCEPTED = "Accepted"
    PROJECT_UNAVAILABLE = "ProjectUnavailable"


class InvitationEventKind(StrEnum):
    ISSUE = "Issue"
    RESEND = "Resend"
    REVOKE = "Revoke"
    ACCEPT = "Accept"
    EXPIRE = "Expire"


INVITATION_TTL: Final = timedelta(days=7)
_CREDENTIAL_BYTES: Final = 32
_CREDENTIAL = re.compile(r"^[A-Za-z0-9_\-]{43}$")


def new_credential() -> str:
    """32 random bytes, base64url without padding (43 characters)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_CREDENTIAL_BYTES)).rstrip(b"=").decode()


def credential_digest(credential: str) -> bytes:
    return hashlib.sha256(credential.encode("ascii")).digest()


def well_formed_credential(credential: str) -> bool:
    return bool(_CREDENTIAL.match(credential))


def effective_state(state: InvitationState, expires_at: datetime, now: datetime) -> InvitationState:
    """Expiry is applied lazily: a Pending row past its expiry reads as Expired."""
    if state is InvitationState.PENDING and now >= expires_at:
        return InvitationState.EXPIRED
    return state


def mask_email(email: str) -> str:
    """``jordan.kim@client.example`` -> ``j•••@client.example``."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}"


_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")


def normalize_invitee_email(value: str) -> str | None:
    """Lower-cased, trimmed address, or ``None`` when it is not an email address."""
    candidate = value.strip().lower()
    if not _EMAIL.match(candidate) or "." not in candidate.rsplit("@", 1)[1]:
        return None
    return candidate


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[1]
