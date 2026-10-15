"""Authentication port (conventions §1 decision 4): bearer token -> ``VerifiedIdentity``."""

from app.platform.auth.jwt_verifier import (
    HttpJwksSource,
    JwksSource,
    JwtTokenVerifier,
    StaticJwksSource,
    UnconfiguredTokenVerifier,
    entra_token_verifier,
)
from app.platform.auth.ports import (
    RejectionReason,
    TokenRejected,
    TokenVerifier,
    VerifiedIdentity,
    normalize_email,
)

__all__ = [
    "HttpJwksSource",
    "JwksSource",
    "JwtTokenVerifier",
    "RejectionReason",
    "StaticJwksSource",
    "TokenRejected",
    "TokenVerifier",
    "UnconfiguredTokenVerifier",
    "VerifiedIdentity",
    "entra_token_verifier",
    "normalize_email",
]
