"""Test identity provider: an RSA signer whose JWKS feeds the real ``JwtTokenVerifier``.

``TestTokenIssuer.token_for(persona)`` mints a valid Entra-shaped access token; keyword
overrides produce the invalid variants of the token-validation matrix.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Final

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.platform.auth import JwtTokenVerifier, StaticJwksSource
from app.platform.clock import Clock
from tests.fixtures.synthetic.personas import TENANT_ID, Persona

ISSUER: Final = f"https://login.test/{TENANT_ID}/v2.0"
AUDIENCE: Final = "api://relaxai-test"
_UNSET: Final = object()


class TestTokenIssuer:
    __test__ = False  # not a pytest class

    def __init__(self, clock: Clock, *, kid: str = "test-key-1", key: Any = None) -> None:
        self._clock = clock
        self.kid = kid
        self._key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def using(self, clock: Clock) -> TestTokenIssuer:
        """The same signing key, minting tokens relative to ``clock`` (key generation is slow,
        so tests share one key and swap clocks)."""
        return TestTokenIssuer(clock, kid=self.kid, key=self._key)

    def jwks(self) -> dict[str, Any]:
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def sign(self, claims: dict[str, Any], *, kid: str | None = None, key: Any = None) -> str:
        return jwt.encode(
            claims, key or self._key, algorithm="RS256", headers={"kid": kid or self.kid}
        )

    def claims_for(
        self,
        persona: Persona,
        *,
        issuer: str = ISSUER,
        audience: str = AUDIENCE,
        tenant_id: str = TENANT_ID,
        email: Any = _UNSET,
        email_verified: bool = True,
        expires_in: timedelta = timedelta(hours=1),
        not_before: datetime | None = None,
    ) -> dict[str, Any]:
        now = self._clock.now()
        claims: dict[str, Any] = {
            "iss": issuer,
            "aud": audience,
            "tid": tenant_id,
            "oid": persona.subject,
            "sub": f"pairwise-{persona.subject}",
            "name": persona.display_name,
            "email": persona.email if email is _UNSET else email,
            "email_verified": email_verified,
            "iat": int(now.timestamp()),
            "nbf": int((not_before or now).timestamp()),
            "exp": int((now + expires_in).timestamp()),
            "jti": str(uuid.uuid4()),
        }
        if claims["email"] is None:
            del claims["email"]
        return claims

    def token_for(self, persona: Persona, **overrides: Any) -> str:
        return self.sign(self.claims_for(persona, **overrides))

    def headers_for(self, persona: Persona, **overrides: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token_for(persona, **overrides)}"}


class TestTokenVerifier(JwtTokenVerifier):
    """The production verifier, fed the issuer's static JWKS."""

    __test__ = False

    def __init__(self, issuer: TestTokenIssuer, clock: Clock) -> None:
        super().__init__(
            StaticJwksSource(issuer.jwks(), issuer=ISSUER),
            audience=AUDIENCE,
            allowed_tenant_ids=[TENANT_ID],
            clock=clock,
        )
