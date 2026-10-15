"""RS256 JWT validation against a JWKS: the Entra External ID adapter and its test twin.

``JwtTokenVerifier`` holds every rule (algorithm, signature, ``iss``, ``aud``, ``exp``,
``nbf``, tenant, subject, verified email). Only the key source differs: ``HttpJwksSource``
discovers and caches Entra's keys, refreshing on an unknown ``kid`` at most once per
``jwks_min_refresh_seconds``; ``StaticJwksSource`` serves a test signer's keys. Expiry is
checked against the injected ``Clock`` so ``FrozenClock`` tests are deterministic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx
import jwt

from app.platform.auth.ports import (
    RejectionReason,
    TokenRejected,
    VerifiedIdentity,
    normalize_email,
)
from app.platform.clock import Clock
from app.platform.errors import UpstreamCategory, UpstreamError
from app.settings import EntraSettings

ALGORITHMS = ("RS256",)
_R = RejectionReason


class JwksSource(Protocol):
    async def issuer(self) -> str: ...

    async def key(self, kid: str) -> jwt.PyJWK | None:
        """The signing key for ``kid``, refreshing the cache if allowed; ``None`` if unknown."""
        ...


def _parse_jwks(document: Mapping[str, Any]) -> dict[str, jwt.PyJWK]:
    keys: dict[str, jwt.PyJWK] = {}
    for entry in document.get("keys", []):
        kid = entry.get("kid")
        if not isinstance(kid, str) or entry.get("kty") != "RSA":
            continue
        try:
            keys[kid] = jwt.PyJWK(dict(entry), algorithm="RS256")
        except jwt.PyJWTError:
            continue
    return keys


class StaticJwksSource:
    def __init__(self, jwks: Mapping[str, Any], *, issuer: str) -> None:
        self._keys = _parse_jwks(jwks)
        self._issuer = issuer

    async def issuer(self) -> str:
        return self._issuer

    async def key(self, kid: str) -> jwt.PyJWK | None:
        return self._keys.get(kid)


class HttpJwksSource:
    """OIDC discovery + JWKS over ``httpx``. Network failures become ``503`` (not ``401``):
    an identity-provider outage is not the caller's fault."""

    def __init__(self, http: httpx.AsyncClient, settings: EntraSettings, clock: Clock) -> None:
        self._http = http
        self._settings = settings
        self._clock = clock
        self._issuer = settings.issuer
        self._jwks_url = settings.jwks_url
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def _get_json(self, url: str) -> Mapping[str, Any]:
        try:
            response = await self._http.get(url, timeout=self._settings.http_timeout_seconds)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise UpstreamError(UpstreamCategory.IDENTITY) from None
        if not isinstance(body, dict):
            raise UpstreamError(UpstreamCategory.IDENTITY)
        return body

    async def _discover(self) -> None:
        if self._issuer is not None and self._jwks_url is not None:
            return
        authority = (self._settings.authority or "").rstrip("/")
        if not authority:
            raise TokenRejected(_R.NOT_CONFIGURED)
        metadata = await self._get_json(f"{authority}/.well-known/openid-configuration")
        issuer, jwks_uri = metadata.get("issuer"), metadata.get("jwks_uri")
        if not isinstance(issuer, str) or not isinstance(jwks_uri, str):
            raise UpstreamError(UpstreamCategory.IDENTITY)
        self._issuer = self._issuer or issuer
        self._jwks_url = self._jwks_url or jwks_uri

    async def issuer(self) -> str:
        async with self._lock:
            await self._discover()
        assert self._issuer is not None  # noqa: S101 - set by _discover
        return self._issuer

    async def key(self, kid: str) -> jwt.PyJWK | None:
        if kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            if kid in self._keys:
                return self._keys[kid]
            now = self._clock.now().timestamp()
            due = (
                self._fetched_at is None
                or now - self._fetched_at >= self._settings.jwks_min_refresh_seconds
            )
            if due:
                await self._discover()
                assert self._jwks_url is not None  # noqa: S101 - set by _discover
                self._keys = _parse_jwks(await self._get_json(self._jwks_url))
                self._fetched_at = now
            return self._keys.get(kid)


class JwtTokenVerifier:
    def __init__(
        self,
        source: JwksSource,
        *,
        audience: str,
        allowed_tenant_ids: Sequence[str],
        clock: Clock,
        clock_skew_seconds: int = 60,
    ) -> None:
        self._source = source
        self._audience = audience
        self._tenants = frozenset(allowed_tenant_ids)
        self._clock = clock
        self._skew = clock_skew_seconds

    async def verify(self, bearer: str) -> VerifiedIdentity:
        try:
            header = jwt.get_unverified_header(bearer)
            unverified = jwt.decode(bearer, options={"verify_signature": False})
        except jwt.PyJWTError:
            raise TokenRejected(_R.MALFORMED) from None
        subject = _subject(unverified)

        def reject(reason: RejectionReason) -> TokenRejected:
            return TokenRejected(reason, unverified_subject=subject)

        if header.get("alg") not in ALGORITHMS:
            raise reject(_R.WRONG_ALGORITHM)
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise reject(_R.UNKNOWN_KEY)
        key = await self._source.key(kid)
        if key is None:
            raise reject(_R.UNKNOWN_KEY)
        try:
            claims: dict[str, Any] = jwt.decode(
                bearer,
                key.key,
                algorithms=list(ALGORITHMS),
                audience=self._audience,
                issuer=await self._source.issuer(),
                options={
                    # Time claims are checked below against the injected Clock.
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "require": ["exp", "iss", "aud"],
                },
            )
        except jwt.InvalidSignatureError:
            raise reject(_R.BAD_SIGNATURE) from None
        except jwt.InvalidAudienceError:
            raise reject(_R.WRONG_AUDIENCE) from None
        except jwt.InvalidIssuerError:
            raise reject(_R.WRONG_ISSUER) from None
        except jwt.PyJWTError:
            raise reject(_R.MALFORMED) from None

        now = self._clock.now().timestamp()
        exp, nbf = claims.get("exp"), claims.get("nbf")
        if not isinstance(exp, int | float) or now > exp + self._skew:
            raise reject(_R.EXPIRED)
        if nbf is not None and (not isinstance(nbf, int | float) or now + self._skew < nbf):
            raise reject(_R.NOT_YET_VALID)
        tenant = claims.get("tid")
        if not isinstance(tenant, str) or tenant not in self._tenants:
            raise reject(_R.WRONG_TENANT)
        if subject is None:
            raise TokenRejected(_R.MISSING_SUBJECT)

        email = _email(claims)
        return VerifiedIdentity(
            subject=subject,
            tenant_id=tenant,
            email=email,
            email_verified=email is not None and claims.get("email_verified") is True,
            display_name=_display_name(claims),
        )


def _subject(claims: Mapping[str, Any]) -> str | None:
    """Entra's ``oid`` is stable across applications; fall back to ``sub``."""
    for name in ("oid", "sub"):
        value = claims.get(name)
        if isinstance(value, str) and 0 < len(value) <= 128:
            return value
    return None


def _email(claims: Mapping[str, Any]) -> str | None:
    value = claims.get("email")
    if not isinstance(value, str):
        emails = claims.get("emails")  # External ID user flows may emit a list
        value = emails[0] if isinstance(emails, list) and emails else None
    if not isinstance(value, str):
        return None
    email = normalize_email(value)
    local, _, domain = email.partition("@")
    if not local or "." not in domain or len(email) > 254:
        return None
    return email


def _display_name(claims: Mapping[str, Any]) -> str:
    value = claims.get("name")
    return value.strip()[:200] if isinstance(value, str) else ""


class UnconfiguredTokenVerifier:
    """Used when ``entra`` is not configured (local runs): every token is refused."""

    async def verify(self, bearer: str) -> VerifiedIdentity:
        del bearer
        raise TokenRejected(RejectionReason.NOT_CONFIGURED)


def entra_token_verifier(
    settings: EntraSettings, http: httpx.AsyncClient, clock: Clock
) -> JwtTokenVerifier | UnconfiguredTokenVerifier:
    if not settings.configured or settings.audience is None:
        return UnconfiguredTokenVerifier()
    return JwtTokenVerifier(
        HttpJwksSource(http, settings, clock),
        audience=settings.audience,
        allowed_tenant_ids=settings.allowed_tenant_ids,
        clock=clock,
        clock_skew_seconds=settings.clock_skew_seconds,
    )
