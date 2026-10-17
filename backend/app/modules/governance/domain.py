"""Pure governance rules: job states, priorities, backoff, the audit catalog, safe facts,
and the privacy screens.

Nothing here performs I/O.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel

from app.platform.ids import is_opaque_id
from app.platform.schema import OmitWhenNone

# --------------------------------------------------------------------------- jobs


class JobState(StrEnum):
    QUEUED = "Queued"
    RUNNING = "Running"
    RETRYING = "Retrying"
    WAITING_FOR_USER = "WaitingForUser"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


TERMINAL_STATES: Final = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})


class Priority(IntEnum):
    """Claim order (higher first). Privacy reductions, consent revocation, erasure, and
    deletion use HIGH/CRITICAL; enrichment uses NORMAL/LOW."""

    LOW = 10
    NORMAL = 20
    HIGH = 30
    CRITICAL = 40


BACKOFF_CAP: Final = timedelta(minutes=10)


def retry_delay(
    attempt: int, *, rng: random.Random | None = None, retry_after: timedelta | None = None
) -> timedelta:
    """Delay before retrying after failed ``attempt`` (1-based).

    2^attempt seconds, ±20 % jitter, capped at 10 minutes.

    An upstream ``Retry-After`` hint wins when given (it is still capped).
    """
    if retry_after is not None:
        return min(max(retry_after, timedelta(0)), BACKOFF_CAP)
    base = 2.0 ** min(attempt, 20)
    jitter = (rng or random).uniform(0.8, 1.2)  # jitter only; not a security decision
    return min(timedelta(seconds=base * jitter), BACKOFF_CAP)


class Destination(OmitWhenNone):
    """An authorized frontend route (contract §3 ``Destination``; ``projectId?``)."""

    omit_when_none = frozenset({"project_id"})

    kind: str
    project_id: str | None = None
    path: str


# --------------------------------------------------------------------------- privacy screens

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\d[\d ()\-]{7,}\d")
# A date or timestamp is not a phone number: "2026-05-04T03:00:00Z" must never be flagged.
_ISO_DATE_OR_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)
_PHONE_MIN_DIGITS = 9
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:+\-]{1,256}$")
_WORD = re.compile(r"[A-Za-z]+")


class PersonalDataScreen:
    """Local regex screen for personal data in short values.

    Integration point: T11's detector replaces this for audit reasons (see ``ReasonScreen``).
    ``add_names`` lets tests register the synthetic fixture personas so any leak of them
    into audit facts or job payloads fails loudly.
    """

    def __init__(self) -> None:
        self._names: set[str] = set()

    def add_names(self, names: Iterable[str]) -> None:
        for name in names:
            self._names.update(word.lower() for word in _WORD.findall(name))

    def contains_personal_data(self, text: str) -> bool:
        if _EMAIL.search(text):
            return True
        stripped = text.strip()
        if not _ISO_DATE_OR_TIME.match(stripped):
            match = _PHONE.search(text)
            # Require a plausible phone length: real numbers carry at least nine digits.
            if match and sum(c.isdigit() for c in match.group()) >= _PHONE_MIN_DIGITS:
                return True
        return any(word.lower() in self._names for word in _WORD.findall(text))

    def require_safe_token(self, value: str, *, what: str) -> None:
        """Values that must be identifiers/enum-like: no spaces, no free text, no personal data.

        An opaque identifier minted by ``new_id`` is random, so it is accepted as is: its
        ULID can contain a nine-digit run or a three-letter run that the screen would
        otherwise mistake for a phone number or a fixture name.
        """
        if is_opaque_id(value):
            return
        if self.contains_personal_data(value):
            raise ValueError(f"{what}: value looks like personal data")
        if not _SAFE_TOKEN.match(value):
            raise ValueError(f"{what}: free-text value rejected (identifiers/enum values only)")


personal_data_screen: Final = PersonalDataScreen()


# Job payloads carry opaque IDs and enum values only (conventions §9.5).
FORBIDDEN_PAYLOAD_KEYS: Final = frozenset(
    {
        "text", "body", "content", "html", "markdown", "excerpt", "summary", "question",
        "answer", "message", "subject", "title", "filename", "reason", "comment", "note",
        "email", "emailaddress", "name", "displayname", "firstname", "lastname", "fullname",
        "realname", "phone", "token", "accesstoken", "refreshtoken", "idtoken", "password",
        "secret", "apikey", "credential", "confirmation", "confirmationname",
        # compound spellings of the same idea
        "documenttext", "chattext", "rawtext", "documentcontent", "rawcontent", "messagebody",
    }
)  # fmt: skip


def _normalise_key(key: str) -> str:
    return key.replace("_", "").replace("-", "").lower()


def check_job_payload(
    payload: BaseModel, screen: PersonalDataScreen = personal_data_screen
) -> None:
    """Raise ``ValueError`` if a payload could carry content, credentials, or personal data."""

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if _normalise_key(str(key)) in FORBIDDEN_PAYLOAD_KEYS:
                    raise ValueError(f"job payload key {path}{key!r} is forbidden")
                walk(item, f"{path}{key}.")
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item, path)
        elif isinstance(value, str):
            screen.require_safe_token(value, what=f"job payload {path.rstrip('.') or 'value'}")

    # mode="python" keeps datetimes as datetimes: a scheduled instant is not content.
    walk(payload.model_dump(mode="python", by_alias=False), "")


# --------------------------------------------------------------------------- usage


class Meter(StrEnum):
    AI_TOKENS_IN = "AiTokensIn"
    AI_TOKENS_OUT = "AiTokensOut"
    EMBEDDING_TOKENS = "EmbeddingTokens"
    OCR_PAGES = "OcrPages"
    BLOB_BYTES = "BlobBytes"


class QuotaPeriod(StrEnum):
    TOTAL = "Total"  # the Portfolio Release's EUR 100 cap is a total, not a monthly budget
    MONTHLY = "Monthly"


def period_key(period: QuotaPeriod, now: datetime) -> str:
    return "total" if period is QuotaPeriod.TOTAL else now.astimezone(UTC).strftime("%Y-%m")


class ReservationState(StrEnum):
    RESERVED = "Reserved"
    COMMITTED = "Committed"
    RELEASED = "Released"
    EXPIRED = "Expired"


# --------------------------------------------------------------------------- audit


class AuditCategory(StrEnum):
    ACCOUNT = "Account"
    PROJECT = "Project"
    GOVERNANCE = "Governance"  # Firm Manager view
    OPERATIONAL = "Operational"  # Platform Operator view


class Outcome(StrEnum):
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    DENIED = "Denied"


class AuditAction(StrEnum):
    """Every audited action (GOV-01). Later tasks add members and catalog entries together."""

    # account
    ACCOUNT_ACTIVATION = "account.activation"
    ACCOUNT_SIGN_IN_FAILED = "account.sign_in_failed"
    PERMISSION_DENIED = "permission.denied"
    # project and membership
    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_ARCHIVED = "project.archived"
    PROJECT_RESTORED = "project.restored"
    PROJECT_DELETION_REQUESTED = "project.deletion_requested"
    PROJECT_DELETION_CANCELLED = "project.deletion_cancelled"
    PROJECT_DELETED = "project.deleted"
    MEMBERSHIP_ADDED = "membership.added"
    MEMBERSHIP_ROLE_CHANGED = "membership.role_changed"
    MEMBERSHIP_REMOVED = "membership.removed"
    INVITATION_CREATED = "invitation.created"
    INVITATION_RESENT = "invitation.resent"
    INVITATION_REVOKED = "invitation.revoked"
    INVITATION_EXPIRED = "invitation.expired"
    INVITATION_ACCEPTED = "invitation.accepted"
    # content lifecycle
    SOURCE_CREATED = "source.created"
    SOURCE_UPDATED = "source.updated"
    SOURCE_DISABLED = "source.disabled"
    INGESTION_QUEUED = "ingestion.queued"
    INGESTION_FAILED = "ingestion.failed"
    DOCUMENT_ACCEPTED = "document.accepted"
    DOCUMENT_VISIBILITY_CHANGED = "document.visibility_changed"
    PRIVACY_REVIEW_RESOLVED = "privacy.review_resolved"
    PRIVACY_REVIEW_EXPIRED = "privacy.review_expired"
    EXTRACTED_ITEM_APPROVED = "extracted_item.approved"
    EXTRACTED_ITEM_REJECTED = "extracted_item.rejected"
    RECOMMENDATION_GENERATED = "recommendation.generated"
    RECOMMENDATION_REVIEWED = "recommendation.reviewed"
    ACTION_ITEM_CREATED = "action_item.created"
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_STATUS_CHANGED = "task.status_changed"
    TASK_CONVERTED = "task.converted"
    CHAT_SESSION_DELETED = "chat_session.deleted"
    # firm governance
    FIRM_MANAGER_APPOINTED = "firm_manager.appointed"  # first-manager bootstrap (T03)
    FIRM_MANAGER_NOMINATED = "firm_manager.nominated"
    FIRM_MANAGER_APPROVED = "firm_manager.approved"
    FIRM_MANAGER_REJECTED = "firm_manager.rejected"
    FIRM_MANAGER_REMOVED = "firm_manager.removed"
    ARCHIVAL_SCHEDULED = "archival.scheduled"
    ARCHIVAL_UNSCHEDULED = "archival.unscheduled"
    IDENTITY_REVEALED = "identity.revealed"
    IDENTITY_CASE_RESOLVED = "identity_case.resolved"
    ERASURE_REQUESTED = "erasure.requested"
    ERASURE_VERIFIED = "erasure.verified"
    ERASURE_DECIDED = "erasure.decided"
    ERASURE_COMPLETED = "erasure.completed"
    # platform operations
    FIRM_INITIALIZED = "firm.initialized"
    DOMAIN_ADDED = "domain.added"
    DOMAIN_REMOVED = "domain.removed"
    QUOTA_CHANGED = "quota.changed"
    JOB_RETRIED = "job.retried"
    JOB_CANCELLED = "job.cancelled"
    REBUILD_STARTED = "rebuild.started"
    RETENTION_EXECUTED = "retention.executed"
    CONFIGURATION_CHANGED = "configuration.changed"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    category: AuditCategory
    facts: frozenset[str] = frozenset()
    reason_required: bool = False


_COMMON_FACTS: Final = frozenset({"operationId", "jobId", "count", "correlationId"})


def _spec(category: AuditCategory, *facts: str, reason_required: bool = False) -> ActionSpec:
    return ActionSpec(category, _COMMON_FACTS | frozenset(facts), reason_required)


_A, _C = AuditAction, AuditCategory
AUDIT_CATALOG: Final[Mapping[AuditAction, ActionSpec]] = {
    _A.ACCOUNT_ACTIVATION: _spec(_C.ACCOUNT, "failureCode", "domainApproved", "personCategory"),
    _A.ACCOUNT_SIGN_IN_FAILED: _spec(_C.ACCOUNT, "failureCode", "subjectHash"),
    _A.PERMISSION_DENIED: _spec(_C.PROJECT, "capability", "routeTemplate", "errorCode"),
    _A.PROJECT_CREATED: _spec(_C.PROJECT),
    _A.PROJECT_UPDATED: _spec(_C.PROJECT, "fields"),
    _A.PROJECT_ARCHIVED: _spec(_C.PROJECT, "initiatedBy"),
    _A.PROJECT_RESTORED: _spec(_C.PROJECT),
    _A.PROJECT_DELETION_REQUESTED: _spec(_C.PROJECT, "executesAt"),
    _A.PROJECT_DELETION_CANCELLED: _spec(_C.PROJECT),
    _A.PROJECT_DELETED: _spec(_C.PROJECT, "storesVerified"),
    _A.MEMBERSHIP_ADDED: _spec(_C.PROJECT, "role"),
    _A.MEMBERSHIP_ROLE_CHANGED: _spec(_C.PROJECT, "fromRole", "toRole"),
    _A.MEMBERSHIP_REMOVED: _spec(_C.PROJECT, "role"),
    _A.INVITATION_CREATED: _spec(_C.PROJECT, "role", "expiresAt"),
    _A.INVITATION_RESENT: _spec(_C.PROJECT, "previousInvitationId", "expiresAt"),
    _A.INVITATION_REVOKED: _spec(_C.PROJECT),
    _A.INVITATION_EXPIRED: _spec(_C.PROJECT),
    _A.INVITATION_ACCEPTED: _spec(_C.PROJECT, "role"),
    _A.SOURCE_CREATED: _spec(_C.PROJECT, "sourceType", "visibility"),
    _A.SOURCE_UPDATED: _spec(_C.PROJECT, "sourceType", "visibility"),
    _A.SOURCE_DISABLED: _spec(_C.PROJECT, "sourceType"),
    _A.INGESTION_QUEUED: _spec(_C.PROJECT, "sourceType", "format"),
    _A.INGESTION_FAILED: _spec(_C.PROJECT, "sourceType", "format", "failureCode"),
    _A.DOCUMENT_ACCEPTED: _spec(_C.PROJECT, "sourceType", "format", "visibility"),
    _A.DOCUMENT_VISIBILITY_CHANGED: _spec(_C.PROJECT, "fromVisibility", "toVisibility"),
    _A.PRIVACY_REVIEW_RESOLVED: _spec(_C.PROJECT, "decision", "candidateCount"),
    _A.PRIVACY_REVIEW_EXPIRED: _spec(_C.PROJECT),
    _A.EXTRACTED_ITEM_APPROVED: _spec(_C.PROJECT, "itemType", "edited"),
    _A.EXTRACTED_ITEM_REJECTED: _spec(_C.PROJECT, "itemType", reason_required=True),
    _A.RECOMMENDATION_GENERATED: _spec(_C.PROJECT),
    _A.RECOMMENDATION_REVIEWED: _spec(_C.PROJECT, "decision"),
    _A.ACTION_ITEM_CREATED: _spec(_C.PROJECT, "visibility"),
    _A.TASK_CREATED: _spec(_C.PROJECT, "visibility", "priority"),
    _A.TASK_UPDATED: _spec(_C.PROJECT, "fields"),
    _A.TASK_STATUS_CHANGED: _spec(_C.PROJECT, "fromStatus", "toStatus"),
    _A.TASK_CONVERTED: _spec(_C.PROJECT, "plannerTaskLinkId"),
    _A.CHAT_SESSION_DELETED: _spec(_C.PROJECT),
    _A.FIRM_MANAGER_APPOINTED: _spec(_C.GOVERNANCE, "userId"),
    _A.FIRM_MANAGER_NOMINATED: _spec(_C.GOVERNANCE),
    _A.FIRM_MANAGER_APPROVED: _spec(_C.GOVERNANCE),
    _A.FIRM_MANAGER_REJECTED: _spec(_C.GOVERNANCE, reason_required=True),
    _A.FIRM_MANAGER_REMOVED: _spec(_C.GOVERNANCE, reason_required=True),
    _A.ARCHIVAL_SCHEDULED: _spec(_C.GOVERNANCE, "executesAt"),
    _A.ARCHIVAL_UNSCHEDULED: _spec(_C.GOVERNANCE, "outcome"),
    _A.IDENTITY_REVEALED: _spec(_C.GOVERNANCE, "identityCaseId", reason_required=True),
    _A.IDENTITY_CASE_RESOLVED: _spec(_C.GOVERNANCE, "resolutionKind", reason_required=True),
    _A.ERASURE_REQUESTED: _spec(_C.GOVERNANCE, "channel"),
    _A.ERASURE_VERIFIED: _spec(_C.GOVERNANCE, "method"),
    _A.ERASURE_DECIDED: _spec(_C.GOVERNANCE, "decision", reason_required=True),
    _A.ERASURE_COMPLETED: _spec(_C.GOVERNANCE, "storesVerified", "affectedProjectCount"),
    _A.FIRM_INITIALIZED: _spec(_C.OPERATIONAL, "stamp"),
    _A.DOMAIN_ADDED: _spec(_C.OPERATIONAL, "domain"),
    _A.DOMAIN_REMOVED: _spec(_C.OPERATIONAL, "domain"),
    _A.QUOTA_CHANGED: _spec(_C.OPERATIONAL, "meter", "fromQuota", "toQuota"),
    _A.JOB_RETRIED: _spec(_C.OPERATIONAL, "jobKind", reason_required=True),
    _A.JOB_CANCELLED: _spec(_C.OPERATIONAL, "jobKind", reason_required=True),
    _A.REBUILD_STARTED: _spec(_C.OPERATIONAL, "scopeKind"),
    _A.RETENTION_EXECUTED: _spec(_C.OPERATIONAL, "policy", "deletedCount"),
    _A.CONFIGURATION_CHANGED: _spec(_C.OPERATIONAL, "setting"),
}

MAX_REASON_LENGTH: Final = 500


class ActorKind(StrEnum):
    TEAM_MEMBER = "TeamMember"
    CLIENT_COLLABORATOR = "ClientCollaborator"
    PLATFORM_OPERATOR = "PlatformOperator"


CLIENT_COLLABORATOR_LABEL: Final = "Client Collaborator"


@dataclass(frozen=True, slots=True)
class ActorRef:
    """A signed-in actor. A Client Collaborator's real name is never stored (GOV-02):
    their label is always the constant ``"Client Collaborator"``."""

    user_id: str
    kind: ActorKind
    label: str

    @property
    def stored_label(self) -> str:
        return (
            CLIENT_COLLABORATOR_LABEL if self.kind is ActorKind.CLIENT_COLLABORATOR else self.label
        )


@dataclass(frozen=True, slots=True)
class SystemActor:
    label: str = "System"


@dataclass(frozen=True, slots=True)
class TargetRef:
    type: str
    id: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z_]{0,63}", self.type):
            raise ValueError("audit target type must be a snake_case word")
        personal_data_screen.require_safe_token(self.id, what="audit target id")


type FactValue = str | int | float | bool | datetime | None

_FACT_KEY = re.compile(r"^[a-z][A-Za-z0-9]{0,63}$")


class SafeFacts(Mapping[str, FactValue]):
    """Content-free audit facts: scalar values only, identifier-like strings only.

    A dict, list, or free-text value is impossible: construction raises ``TypeError`` or
    ``ValueError``. The per-action key allowlist is enforced by ``Audit.record``.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any] | None = None, /, **kwargs: Any) -> None:
        merged = {**(values or {}), **kwargs}
        for key, value in merged.items():
            if not _FACT_KEY.match(key):
                raise ValueError(f"audit fact key {key!r} must be camelCase")
            if isinstance(value, bool | int | float) or value is None:
                continue
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    raise ValueError(f"audit fact {key!r}: datetimes must be timezone-aware")
                continue
            if isinstance(value, str):
                personal_data_screen.require_safe_token(value, what=f"audit fact {key!r}")
                continue
            raise TypeError(f"audit fact {key!r}: {type(value).__name__} values are not allowed")
        self._values: dict[str, FactValue] = dict(merged)

    def __getitem__(self, key: str) -> FactValue:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def to_json(self) -> dict[str, str | int | float | bool | None]:
        return {
            k: v.astimezone(UTC).isoformat() if isinstance(v, datetime) else v
            for k, v in self._values.items()
        }


AuditActorKind = Literal["User", "System", "PlatformOperator"]


def actor_columns(actor: ActorRef | SystemActor) -> tuple[AuditActorKind, str | None, str]:
    """(actor_kind, actor_user_id, actor_label) as stored and returned (contract ``AuditEvent``)."""
    if isinstance(actor, SystemActor):
        return "System", None, actor.label
    kind: AuditActorKind = (
        "PlatformOperator" if actor.kind is ActorKind.PLATFORM_OPERATOR else "User"
    )
    return kind, actor.user_id, actor.stored_label
