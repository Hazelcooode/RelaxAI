from __future__ import annotations

from app.platform.errors import (
    DEFAULT_MESSAGES,
    HTTP_STATUS,
    ErrorCode,
    UpstreamCategory,
    UpstreamError,
    VersionConflict,
)

# Copied from api-contract.md §2. Adding a code to the contract must add it here and to
# ErrorCode in the same change.
CONTRACT_CODES = {
    "MALFORMED_REQUEST": 400,
    "UNAUTHENTICATED": 401,
    "ACCOUNT_NOT_ACTIVATED": 403,
    "CAPABILITY_DENIED": 403,
    "GOVERNANCE_ONLY": 403,
    "NOT_AVAILABLE": 404,
    "VERSION_CONFLICT": 409,
    "PROJECT_READ_ONLY": 409,
    "INVALID_TRANSITION": 409,
    "FINAL_ADMINISTRATOR": 409,
    "FINAL_FIRM_MANAGER": 409,
    "CHAT_SCOPE_LOCKED": 409,
    "REVIEW_ALREADY_RESOLVED": 409,
    "UNDO_EXPIRED": 409,
    "ALREADY_EXISTS": 409,
    "OPERATION_NOT_DISMISSIBLE": 409,
    "ARCHIVAL_NOT_CANDIDATE": 409,
    "CONNECTOR_CONSENT_REQUIRED": 409,
    "CONNECTOR_NOT_OWNER": 409,
    "PLANNER_NOT_CONNECTED": 409,
    "PREVIEW_STALE": 409,
    "FILE_TOO_LARGE": 413,
    "UNSUPPORTED_FORMAT": 415,
    "VALIDATION_FAILED": 422,
    "SCOPE_INVALID": 422,
    "CONFIRMATION_MISMATCH": 422,
    "REASON_REQUIRED": 422,
    "REASON_CONTAINS_PERSONAL_DATA": 422,
    "ASSIGNEE_INELIGIBLE": 422,
    "VISIBILITY_CONSTRAINT": 422,
    "IDEMPOTENCY_KEY_REUSED": 422,
    "ACTIVATION_DOMAIN_NOT_APPROVED": 422,
    "EMAIL_NOT_VERIFIED": 422,
    "INVITATION_INVALID": 422,
    "INVITATION_EMAIL_MISMATCH": 422,
    "PRECONDITION_REQUIRED": 428,
    "RATE_LIMITED": 429,
    "RETRIEVAL_UNAVAILABLE": 503,
    "INDEX_REBUILDING": 503,
    "UPSTREAM_UNAVAILABLE": 503,
    "QUOTA_EXCEEDED": 503,
    "INTERNAL_ERROR": 500,
}


def test_error_codes_match_contract_exactly() -> None:
    assert {c.value: HTTP_STATUS[c] for c in ErrorCode} == CONTRACT_CODES


def test_every_code_has_a_message() -> None:
    assert set(DEFAULT_MESSAGES) == set(ErrorCode)


def test_upstream_error_names_category_only() -> None:
    err = UpstreamError(UpstreamCategory.GRAPH)
    assert err.http_status == 503
    assert err.details[0].code == "Graph"


def test_version_conflict_keeps_row() -> None:
    err = VersionConflict(row={"id": "x", "version": 2})
    assert err.code is ErrorCode.VERSION_CONFLICT
    assert err.row == {"id": "x", "version": 2}
