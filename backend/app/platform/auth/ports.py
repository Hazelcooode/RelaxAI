"""``TokenVerifier`` port and the identity it yields."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.platform.errors import Unauthenticated


class RejectionReason(StrEnum):
    """Why a bearer token was refused. Recorded as a failure code; never shown to clients."""

    MISSING = "TokenMissing"
    MALFORMED = "TokenMalformed"
    UNKNOWN_KEY = "UnknownSigningKey"
    BAD_SIGNATURE = "InvalidSignature"
    WRONG_ALGORITHM = "WrongAlgorithm"
    WRONG_ISSUER = "WrongIssuer"
    WRONG_AUDIENCE = "WrongAudience"
    WRONG_TENANT = "TenantNotAllowed"
    EXPIRED = "TokenExpired"
    NOT_YET_VALID = "TokenNotYetValid"
    MISSING_SUBJECT = "SubjectMissing"
    NOT_CONFIGURED = "VerifierNotConfigured"


class TokenRejected(Unauthenticated):
    """``401 UNAUTHENTICATED`` with the internal reason and, when the token could be parsed,
    its *unverified* subject (used only as a hashed audit reference, GOV-01)."""

    def __init__(self, reason: RejectionReason, *, unverified_subject: str | None = None) -> None:
        super().__init__()
        self.reason = reason
        self.unverified_subject = unverified_subject


def normalize_email(value: str) -> str:
    return value.strip().lower()


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """A validated token's identity claims.

    ``email`` is normalized to lower case and is ``None`` when the token carries none.
    ``email_verified`` is true only when the identity provider asserted it; activation
    requires it, sign-in alone does not.
    """

    subject: str
    tenant_id: str
    email: str | None
    email_verified: bool
    display_name: str

    @property
    def email_domain(self) -> str | None:
        if self.email is None or "@" not in self.email:
            return None
        return self.email.rsplit("@", 1)[1]


class TokenVerifier(Protocol):
    async def verify(self, bearer: str) -> VerifiedIdentity:
        """Return the token's identity or raise ``TokenRejected`` (``401``)."""
        ...
