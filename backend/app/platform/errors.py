"""Stable, user-safe application errors (conventions §3.3, contract §2).

Raise an ``AppError`` (or a convenience subclass) anywhere; one HTTP handler turns it into
the error envelope. Messages are fixed, user-safe English defaults keyed by code — never
interpolate request data, document text, or personal data into them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class ErrorCode(StrEnum):
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    ACCOUNT_NOT_ACTIVATED = "ACCOUNT_NOT_ACTIVATED"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    GOVERNANCE_ONLY = "GOVERNANCE_ONLY"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    PROJECT_READ_ONLY = "PROJECT_READ_ONLY"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    FINAL_ADMINISTRATOR = "FINAL_ADMINISTRATOR"
    FINAL_FIRM_MANAGER = "FINAL_FIRM_MANAGER"
    CHAT_SCOPE_LOCKED = "CHAT_SCOPE_LOCKED"
    REVIEW_ALREADY_RESOLVED = "REVIEW_ALREADY_RESOLVED"
    UNDO_EXPIRED = "UNDO_EXPIRED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    OPERATION_NOT_DISMISSIBLE = "OPERATION_NOT_DISMISSIBLE"
    ARCHIVAL_NOT_CANDIDATE = "ARCHIVAL_NOT_CANDIDATE"
    CONNECTOR_CONSENT_REQUIRED = "CONNECTOR_CONSENT_REQUIRED"
    CONNECTOR_NOT_OWNER = "CONNECTOR_NOT_OWNER"
    PLANNER_NOT_CONNECTED = "PLANNER_NOT_CONNECTED"
    PREVIEW_STALE = "PREVIEW_STALE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SCOPE_INVALID = "SCOPE_INVALID"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"
    REASON_REQUIRED = "REASON_REQUIRED"
    REASON_CONTAINS_PERSONAL_DATA = "REASON_CONTAINS_PERSONAL_DATA"
    ASSIGNEE_INELIGIBLE = "ASSIGNEE_INELIGIBLE"
    VISIBILITY_CONSTRAINT = "VISIBILITY_CONSTRAINT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    ACTIVATION_DOMAIN_NOT_APPROVED = "ACTIVATION_DOMAIN_NOT_APPROVED"
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
    INVITATION_INVALID = "INVITATION_INVALID"
    INVITATION_EMAIL_MISMATCH = "INVITATION_EMAIL_MISMATCH"
    PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"
    INDEX_REBUILDING = "INDEX_REBUILDING"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_C = ErrorCode

HTTP_STATUS: Mapping[ErrorCode, int] = {
    _C.MALFORMED_REQUEST: 400,
    _C.UNAUTHENTICATED: 401,
    _C.ACCOUNT_NOT_ACTIVATED: 403,
    _C.CAPABILITY_DENIED: 403,
    _C.GOVERNANCE_ONLY: 403,
    _C.NOT_AVAILABLE: 404,
    _C.VERSION_CONFLICT: 409,
    _C.PROJECT_READ_ONLY: 409,
    _C.INVALID_TRANSITION: 409,
    _C.FINAL_ADMINISTRATOR: 409,
    _C.FINAL_FIRM_MANAGER: 409,
    _C.CHAT_SCOPE_LOCKED: 409,
    _C.REVIEW_ALREADY_RESOLVED: 409,
    _C.UNDO_EXPIRED: 409,
    _C.ALREADY_EXISTS: 409,
    _C.OPERATION_NOT_DISMISSIBLE: 409,
    _C.ARCHIVAL_NOT_CANDIDATE: 409,
    _C.CONNECTOR_CONSENT_REQUIRED: 409,
    _C.CONNECTOR_NOT_OWNER: 409,
    _C.PLANNER_NOT_CONNECTED: 409,
    _C.PREVIEW_STALE: 409,
    _C.FILE_TOO_LARGE: 413,
    _C.UNSUPPORTED_FORMAT: 415,
    _C.VALIDATION_FAILED: 422,
    _C.SCOPE_INVALID: 422,
    _C.CONFIRMATION_MISMATCH: 422,
    _C.REASON_REQUIRED: 422,
    _C.REASON_CONTAINS_PERSONAL_DATA: 422,
    _C.ASSIGNEE_INELIGIBLE: 422,
    _C.VISIBILITY_CONSTRAINT: 422,
    _C.IDEMPOTENCY_KEY_REUSED: 422,
    _C.ACTIVATION_DOMAIN_NOT_APPROVED: 422,
    _C.EMAIL_NOT_VERIFIED: 422,
    _C.INVITATION_INVALID: 422,
    _C.INVITATION_EMAIL_MISMATCH: 422,
    _C.PRECONDITION_REQUIRED: 428,
    _C.RATE_LIMITED: 429,
    _C.RETRIEVAL_UNAVAILABLE: 503,
    _C.INDEX_REBUILDING: 503,
    _C.UPSTREAM_UNAVAILABLE: 503,
    _C.QUOTA_EXCEEDED: 503,
    _C.INTERNAL_ERROR: 500,
}

DEFAULT_MESSAGES: Mapping[ErrorCode, str] = {
    _C.MALFORMED_REQUEST: "The request could not be read.",
    _C.UNAUTHENTICATED: "Sign in to continue.",
    _C.ACCOUNT_NOT_ACTIVATED: "Your account is not activated yet.",
    _C.CAPABILITY_DENIED: "Your role does not allow this action.",
    _C.GOVERNANCE_ONLY: "This area is limited to governance roles.",
    _C.NOT_AVAILABLE: "This item is not available.",
    _C.VERSION_CONFLICT: "This item was changed by someone else. Review the latest version.",
    _C.PROJECT_READ_ONLY: "This Project is archived and read-only.",
    _C.INVALID_TRANSITION: "That change is not allowed from the current state.",
    _C.FINAL_ADMINISTRATOR: "A Project must keep at least one Administrator.",
    _C.FINAL_FIRM_MANAGER: "The firm must keep at least one active Firm Manager.",
    _C.CHAT_SCOPE_LOCKED: "The scope of a chat cannot change after the first message.",
    _C.REVIEW_ALREADY_RESOLVED: "Another reviewer has already resolved this.",
    _C.UNDO_EXPIRED: "The undo window has closed.",
    _C.ALREADY_EXISTS: "This already exists.",
    _C.OPERATION_NOT_DISMISSIBLE: "Active operations cannot be dismissed.",
    _C.ARCHIVAL_NOT_CANDIDATE: "This Project is not an archival candidate.",
    _C.CONNECTOR_CONSENT_REQUIRED: "Microsoft 365 consent is required.",
    _C.CONNECTOR_NOT_OWNER: "Only the connected account owner can do this.",
    _C.PLANNER_NOT_CONNECTED: "Planner is not connected for this Project.",
    _C.PREVIEW_STALE: "The preview has expired. Preview again.",
    _C.FILE_TOO_LARGE: "The file is larger than 50 MB.",
    _C.UNSUPPORTED_FORMAT: "This file format is not supported.",
    _C.VALIDATION_FAILED: "Some fields are invalid.",
    _C.SCOPE_INVALID: "The selected Project set is not valid.",
    _C.CONFIRMATION_MISMATCH: "The confirmation text does not match.",
    _C.REASON_REQUIRED: "A reason is required.",
    _C.REASON_CONTAINS_PERSONAL_DATA: "The reason appears to contain personal data. Rephrase it.",
    _C.ASSIGNEE_INELIGIBLE: "This person cannot be assigned.",
    _C.VISIBILITY_CONSTRAINT: "This conflicts with the item's visibility.",
    _C.IDEMPOTENCY_KEY_REUSED: "This request key was already used for a different request.",
    _C.ACTIVATION_DOMAIN_NOT_APPROVED: "Your email domain is not approved for activation.",
    _C.EMAIL_NOT_VERIFIED: "Your email address is not verified.",
    _C.INVITATION_INVALID: "This invitation can no longer be used.",
    _C.INVITATION_EMAIL_MISMATCH: "This invitation was sent to a different email address.",
    _C.PRECONDITION_REQUIRED: "An If-Match header is required.",
    _C.RATE_LIMITED: "Too many requests. Try again shortly.",
    _C.RETRIEVAL_UNAVAILABLE: "Search is temporarily unavailable.",
    _C.INDEX_REBUILDING: "Part of the search index is being rebuilt.",
    _C.UPSTREAM_UNAVAILABLE: "A connected service is temporarily unavailable.",
    _C.QUOTA_EXCEEDED: "The usage quota has been reached.",
    _C.INTERNAL_ERROR: "Something went wrong.",
}


@dataclass(frozen=True, slots=True)
class FieldIssue:
    code: str
    message: str
    field: str | None = None


class AppError(Exception):
    """Base for every error that reaches a client as an error envelope."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        code: ErrorCode | None = None,
        *,
        message: str | None = None,
        details: Sequence[FieldIssue] = (),
        current: BaseModel | None = None,
        retry_after_seconds: int | None = None,
        allowed_transitions: Sequence[str] | None = None,
    ) -> None:
        if code is not None:
            self.code = code
        self.message = message or DEFAULT_MESSAGES[self.code]
        self.details = list(details)
        self.current = current
        self.retry_after_seconds = retry_after_seconds
        self.allowed_transitions = (
            list(allowed_transitions) if allowed_transitions is not None else None
        )
        super().__init__(self.code.value)

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]


class MalformedRequest(AppError):
    code = ErrorCode.MALFORMED_REQUEST


class Unauthenticated(AppError):
    code = ErrorCode.UNAUTHENTICATED


class AccountNotActivated(AppError):
    code = ErrorCode.ACCOUNT_NOT_ACTIVATED


class CapabilityDenied(AppError):
    code = ErrorCode.CAPABILITY_DENIED


class GovernanceOnly(AppError):
    code = ErrorCode.GOVERNANCE_ONLY


class NotAvailable(AppError):
    """Missing, forbidden, stale, or hidden — deliberately indistinguishable."""

    code = ErrorCode.NOT_AVAILABLE


class VersionConflict(AppError):
    """Stale ``If-Match``/version. ``current`` is the authoritative resource for the client.

    Repository helpers that only have a row raise with ``row`` set; the owning service maps
    it to its response model and re-raises with ``current``.
    """

    code = ErrorCode.VERSION_CONFLICT

    def __init__(
        self, current: BaseModel | None = None, *, row: Mapping[Any, Any] | None = None
    ) -> None:
        super().__init__(current=current)
        self.row = row


class ProjectReadOnly(AppError):
    code = ErrorCode.PROJECT_READ_ONLY


class InvalidTransition(AppError):
    code = ErrorCode.INVALID_TRANSITION

    def __init__(self, allowed_transitions: Sequence[str]) -> None:
        super().__init__(allowed_transitions=allowed_transitions)


class AlreadyExists(AppError):
    code = ErrorCode.ALREADY_EXISTS


class ValidationFailed(AppError):
    code = ErrorCode.VALIDATION_FAILED

    def __init__(self, details: Sequence[FieldIssue]) -> None:
        super().__init__(details=details)


class ScopeInvalid(AppError):
    code = ErrorCode.SCOPE_INVALID


class PreconditionRequired(AppError):
    code = ErrorCode.PRECONDITION_REQUIRED


class RateLimited(AppError):
    code = ErrorCode.RATE_LIMITED

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        super().__init__(retry_after_seconds=retry_after_seconds)


class QuotaExceeded(AppError):
    code = ErrorCode.QUOTA_EXCEEDED

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        super().__init__(retry_after_seconds=retry_after_seconds)


class RetrievalUnavailable(AppError):
    code = ErrorCode.RETRIEVAL_UNAVAILABLE


class IndexRebuilding(AppError):
    code = ErrorCode.INDEX_REBUILDING


class UpstreamCategory(StrEnum):
    """Which external capability failed; surfaced as ``details[0].code`` (SYS-08)."""

    GRAPH = "Graph"
    AI = "AzureOpenAI"
    DOCUMENT_INTELLIGENCE = "DocumentIntelligence"
    BLOB = "BlobStorage"
    KEY_VAULT = "KeyVault"
    EMAIL = "Email"
    IDENTITY = "Identity"
    QDRANT = "Qdrant"


class UpstreamError(AppError):
    """An external dependency failed. Adapters raise this; raw upstream bodies never escape."""

    code = ErrorCode.UPSTREAM_UNAVAILABLE

    def __init__(self, category: UpstreamCategory) -> None:
        super().__init__(details=[FieldIssue(code=category.value, message="Service unavailable.")])
        self.category = category
