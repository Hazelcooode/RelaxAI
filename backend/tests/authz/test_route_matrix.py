"""Authorization matrix for the routes added by T03 and T04 (conventions §8).

Every route x caller kind → expected status. Rows for later tasks are added alongside their
routes. Statuses here are the gate only (authentication, account state, operator role):
"passes" means the request reached the route (any status other than 401/403).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from app.modules.organization_access import Actor
from tests.auth import TestTokenIssuer
from tests.fixtures.synthetic.personas import AVERY, OPS, RILEY, Persona

NOOP_ID = "op_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"
NTF_ID = "ntf_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"
IDEM = {"Idempotency-Key": "6f1c1c3e-4b8e-4c4b-9a59-2f2f0f8b6a10"}

ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/api/v1/me", None),
    ("GET", "/api/v1/operations?group=Running", None),
    ("GET", "/api/v1/operations/summary", None),
    ("GET", f"/api/v1/operations/{NOOP_ID}", None),
    ("POST", f"/api/v1/operations/{NOOP_ID}/dismiss", None),
    ("POST", f"/api/v1/operations/{NOOP_ID}/retry", None),
    ("POST", "/api/v1/undo", {"token": "und_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"}),
    ("GET", "/api/v1/notifications", None),
    ("POST", f"/api/v1/notifications/{NTF_ID}/read", None),
    ("POST", "/api/v1/notifications/read-all", None),
    ("GET", f"/api/v1/notifications/{NTF_ID}/target", None),
    ("GET", "/api/v1/operator/domains", None),
    ("POST", "/api/v1/operator/domains", {"domain": "example.test"}),
    ("DELETE", "/api/v1/operator/domains/example.test", None),
    ("POST", "/api/v1/operator/firm-managers", {"userId": "usr_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"}),
    ("GET", "/api/v1/operator/health", None),
    ("GET", "/api/v1/operator/usage", None),
    # Last: it activates an approved-domain caller, which would change every later row.
    ("POST", "/api/v1/activation/employee", {}),
]

OPEN_TO_NOT_ACTIVATED = {"/api/v1/me", "/api/v1/activation/employee"}

PASS = "pass"
# caller -> expected gate outcome per route class
#   anonymous: 401 everywhere
#   not_activated (Riley, no account): /me and activation pass;
#     everything else 403 ACCOUNT_NOT_ACTIVATED
#   team_member (Avery, active): ordinary routes pass; operator routes 403 GOVERNANCE_ONLY
#   operator (OPS, no account): /me, activation, operator routes pass;
#     others 403 ACCOUNT_NOT_ACTIVATED


def expected(caller: str, path: str) -> tuple[int, str | None] | str:
    operator_route = path.startswith("/api/v1/operator/")
    if caller == "anonymous":
        return (401, "UNAUTHENTICATED")
    if path.split("?")[0] in OPEN_TO_NOT_ACTIVATED:
        return PASS
    if caller == "not_activated":
        return (403, "ACCOUNT_NOT_ACTIVATED")
    if caller == "team_member":
        return (403, "GOVERNANCE_ONLY") if operator_route else PASS
    # operator
    return PASS if operator_route else (403, "ACCOUNT_NOT_ACTIVATED")


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as http:
        yield http


@pytest.mark.parametrize("caller", ["anonymous", "not_activated", "team_member", "operator"])
async def test_route_gate_matrix(
    caller: str,
    client: httpx.AsyncClient,
    token_issuer: TestTokenIssuer,
    activate: Callable[[Persona], Awaitable[Actor]],
) -> None:
    await activate(AVERY)
    persona = {"not_activated": RILEY, "team_member": AVERY, "operator": OPS}.get(caller)
    headers = {**IDEM, **(token_issuer.headers_for(persona) if persona else {})}
    failures = []
    for method, path, body in ROUTES:
        r = await client.request(method, path, headers=headers, json=body)
        want = expected(caller, path)
        if isinstance(want, str):
            ok = r.status_code not in (401, 403)
        else:
            status, code = want
            ok = r.status_code == status and r.json()["error"]["code"] == code
        if not ok:
            failures.append(f"{method} {path}: {r.status_code} {r.text[:120]}")
    if failures:
        pytest.fail(f"{caller}:\n" + "\n".join(failures))
