"""Token validation matrix (T03): every invalid variant is a 401 with a distinct reason."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

from app.platform.auth import (
    HttpJwksSource,
    JwtTokenVerifier,
    RejectionReason,
    TokenRejected,
    TokenVerifier,
    UnconfiguredTokenVerifier,
    entra_token_verifier,
)
from app.platform.clock import FrozenClock
from app.platform.errors import ErrorCode, UpstreamError
from app.settings import EntraSettings
from tests.auth import AUDIENCE, ISSUER, TestTokenIssuer, TestTokenVerifier
from tests.fixtures.synthetic.personas import AVERY, OTHER_TENANT_ID, TENANT_ID

NOW = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
R = RejectionReason


@pytest.fixture(scope="module")
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture(scope="module")
def issuer(clock: FrozenClock) -> TestTokenIssuer:
    return TestTokenIssuer(clock)


@pytest.fixture
def verifier(issuer: TestTokenIssuer, clock: FrozenClock) -> TestTokenVerifier:
    return TestTokenVerifier(issuer, clock)


async def test_valid_token_yields_normalized_identity(
    issuer: TestTokenIssuer, verifier: TestTokenVerifier
) -> None:
    identity = await verifier.verify(
        issuer.token_for(AVERY, email="Avery.Chen@RelaxAI-Consulting.TEST")
    )
    assert identity.subject == AVERY.subject  # oid wins over the pairwise sub
    assert identity.tenant_id == TENANT_ID
    assert identity.email == "avery.chen@relaxai-consulting.test"
    assert identity.email_domain == "relaxai-consulting.test"
    assert identity.email_verified is True
    assert identity.display_name == "Avery Chen"


async def _rejection(verifier: TokenVerifier, token: str) -> TokenRejected:
    with pytest.raises(TokenRejected) as caught:
        await verifier.verify(token)
    assert caught.value.code is ErrorCode.UNAUTHENTICATED
    assert caught.value.http_status == 401
    return caught.value


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"issuer": "https://evil.test/v2.0"}, R.WRONG_ISSUER),
        ({"audience": "api://someone-else"}, R.WRONG_AUDIENCE),
        ({"tenant_id": OTHER_TENANT_ID}, R.WRONG_TENANT),
        ({"expires_in": timedelta(minutes=-5)}, R.EXPIRED),
        ({"not_before": NOW + timedelta(minutes=10)}, R.NOT_YET_VALID),
    ],
)
async def test_claim_violations_are_rejected(
    issuer: TestTokenIssuer, verifier: TestTokenVerifier, overrides: dict[str, object],
    reason: RejectionReason,
) -> None:  # fmt: skip
    rejected = await _rejection(verifier, issuer.token_for(AVERY, **overrides))
    assert rejected.reason is reason
    assert rejected.unverified_subject == AVERY.subject


async def test_clock_skew_tolerates_a_just_expired_token(
    issuer: TestTokenIssuer, verifier: TestTokenVerifier
) -> None:
    identity = await verifier.verify(issuer.token_for(AVERY, expires_in=timedelta(seconds=-30)))
    assert identity.subject == AVERY.subject


async def test_bad_signature_and_unknown_kid(
    issuer: TestTokenIssuer, verifier: TestTokenVerifier
) -> None:
    forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = issuer.claims_for(AVERY)
    assert (await _rejection(verifier, issuer.sign(claims, key=forger))).reason is R.BAD_SIGNATURE
    unknown = issuer.sign(claims, kid="rotated-key", key=forger)
    assert (await _rejection(verifier, unknown)).reason is R.UNKNOWN_KEY
    assert (await _rejection(verifier, "not.a.jwt")).reason is R.MALFORMED


async def test_hs256_tokens_are_refused(
    issuer: TestTokenIssuer, verifier: TestTokenVerifier
) -> None:
    import jwt

    token = jwt.encode(
        issuer.claims_for(AVERY), "x" * 32, algorithm="HS256", headers={"kid": issuer.kid}
    )
    assert (await _rejection(verifier, token)).reason is R.WRONG_ALGORITHM


async def test_missing_or_unverified_email_still_signs_in(
    issuer: TestTokenIssuer, verifier: TestTokenVerifier
) -> None:
    missing = await verifier.verify(issuer.token_for(AVERY, email=None))
    assert missing.email is None
    assert missing.email_verified is False
    unverified = await verifier.verify(issuer.token_for(AVERY, email_verified=False))
    assert unverified.email == AVERY.email
    assert unverified.email_verified is False


# --------------------------------------------------------------------------- Entra adapter

AUTHORITY = f"https://login.test/{TENANT_ID}/v2.0"
SETTINGS = EntraSettings(
    authority=AUTHORITY,
    audience=AUDIENCE,
    allowed_tenant_ids=[TENANT_ID],
    jwks_min_refresh_seconds=60,
)


def _discovery(router: respx.MockRouter, issuer: TestTokenIssuer) -> respx.Route:
    router.get(f"{AUTHORITY}/.well-known/openid-configuration").respond(
        json={"issuer": ISSUER, "jwks_uri": "https://login.test/keys"}
    )
    return router.get("https://login.test/keys").respond(json=issuer.jwks())


async def test_entra_verifier_discovers_and_caches_keys(issuer: TestTokenIssuer) -> None:
    clock = FrozenClock(NOW)
    with respx.mock(assert_all_called=True) as router:
        jwks = _discovery(router, issuer)
        async with httpx.AsyncClient() as http:
            verifier = entra_token_verifier(SETTINGS, http, clock)
            assert isinstance(verifier, JwtTokenVerifier)
            for _ in range(3):
                assert (await verifier.verify(issuer.token_for(AVERY))).subject == AVERY.subject
    assert jwks.call_count == 1


async def test_unknown_kid_refreshes_at_most_once_per_minute(issuer: TestTokenIssuer) -> None:
    clock = FrozenClock(NOW)
    rotated = TestTokenIssuer(clock, kid="rotated-key")
    with respx.mock() as router:
        jwks = _discovery(router, issuer)
        async with httpx.AsyncClient() as http:
            verifier = entra_token_verifier(SETTINGS, http, clock)
            await verifier.verify(issuer.token_for(AVERY))
            # A token signed with a key the IdP has not published yet: one refresh, no more.
            for _ in range(3):
                await _rejection(verifier, rotated.token_for(AVERY))
            assert jwks.call_count == 1
            # After the interval the IdP publishes the rotated key; a refresh picks it up.
            clock.advance(timedelta(seconds=61))
            jwks.respond(json={"keys": issuer.jwks()["keys"] + rotated.jwks()["keys"]})
            assert (await verifier.verify(rotated.token_for(AVERY))).subject == AVERY.subject
            assert jwks.call_count == 2


async def test_identity_provider_outage_is_503_not_401(issuer: TestTokenIssuer) -> None:
    with respx.mock() as router:
        router.get(f"{AUTHORITY}/.well-known/openid-configuration").respond(status_code=503)
        async with httpx.AsyncClient() as http:
            verifier = HttpJwksSource(http, SETTINGS, FrozenClock(NOW))
            with pytest.raises(UpstreamError):
                await verifier.key("test-key-1")


async def test_unconfigured_environment_refuses_every_token(issuer: TestTokenIssuer) -> None:
    async with httpx.AsyncClient() as http:
        verifier = entra_token_verifier(EntraSettings(), http, FrozenClock(NOW))
    assert isinstance(verifier, UnconfiguredTokenVerifier)
    rejected = await _rejection(verifier, issuer.token_for(AVERY))
    assert rejected.reason is R.NOT_CONFIGURED
