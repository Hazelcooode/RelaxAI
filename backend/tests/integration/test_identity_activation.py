"""Identity, activation, and firm bootstrap through the real app (T03, RA-01 first half)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from sqlalchemy import select, update

from app.api.v1.deps import ActiveActor
from app.container import Container
from app.modules.governance import AuditAction, Outcome
from app.modules.governance.tables import audit_events
from app.modules.organization_access import Actor, OperatorActor
from app.modules.organization_access.tables import user_accounts
from app.platform.clock import FrozenClock
from app.platform.errors import AlreadyExists, AppError, ErrorCode, ValidationFailed
from tests.auth import TestTokenIssuer
from tests.conftest import identity_of
from tests.fixtures.synthetic.personas import (
    AVERY,
    CLIENT_DOMAIN,
    JORDAN,
    OPS,
    SAM,
    TEAM_MEMBERS,
    Persona,
)

ME_KEYS = {
    "userId", "activation", "personCategory", "email", "displayName", "firmId", "firmRoles",
    "globalCapabilities", "projects", "landing", "authorizationVersion", "connector",
}  # fmt: skip
Activate = Callable[[Persona], Awaitable[Actor]]


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    probe = APIRouter(prefix="/api/v1/_probe")

    @probe.get("/protected")
    async def protected(actor: ActiveActor) -> dict[str, str | None]:
        return {"userId": actor.user_id}

    api_app.include_router(probe)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as http:
        yield http


async def _audit_rows(container: Container, action: AuditAction) -> list[Any]:
    async with container.uow() as tx:
        stmt = select(audit_events).where(audit_events.c.action == action.value)
        return list((await tx.session.execute(stmt)).mappings().all())


# --------------------------------------------------------------------------- /me


async def test_me_requires_a_token(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_me_for_a_signed_in_but_not_activated_person(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, firm: str
) -> None:
    r = await client.get("/api/v1/me", headers=token_issuer.headers_for(AVERY))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == ME_KEYS
    assert body["userId"] is None
    assert body["activation"] == "NotActivated"
    assert body["personCategory"] is None
    assert body["email"] == AVERY.email
    assert body["firmId"] == firm
    assert body["firmRoles"] == {"firmManager": False, "platformOperator": False}
    assert body["globalCapabilities"] == []
    assert body["projects"] == []
    assert body["landing"] == {"kind": "Activation"}
    assert body["connector"] == {"state": "Disconnected"}


async def test_not_activated_actor_is_refused_on_protected_routes(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, firm: str
) -> None:
    r = await client.get("/api/v1/_probe/protected", headers=token_issuer.headers_for(AVERY))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ACCOUNT_NOT_ACTIVATED"


# --------------------------------------------------------------------------- activation


async def test_employee_activation_under_an_approved_domain(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, container: Container, firm: str
) -> None:
    headers = token_issuer.headers_for(AVERY)
    before = (await client.get("/api/v1/me", headers=headers)).json()["authorizationVersion"]

    r = await client.post("/api/v1/activation/employee", json={}, headers=headers)
    assert r.status_code == 200
    me = r.json()
    assert set(me) == ME_KEYS
    assert me["userId"].startswith("usr_")
    assert me["activation"] == "Active"
    assert me["personCategory"] == "TeamMember"
    assert me["displayName"] == "Avery Chen"
    assert me["globalCapabilities"] == ["project.create"]
    assert me["landing"] == {"kind": "ZeroState", "canCreateProject": True}
    assert me["authorizationVersion"] != before

    r = await client.get("/api/v1/_probe/protected", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"userId": me["userId"]}

    events = await _audit_rows(container, AuditAction.ACCOUNT_ACTIVATION)
    assert len(events) == 1
    assert events[0]["outcome"] == Outcome.SUCCEEDED.value
    assert events[0]["target_id"] == me["userId"]
    assert events[0]["facts"] == {"personCategory": "TeamMember", "domainApproved": True}


async def test_second_activation_is_idempotent_and_email_case_insensitive(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, container: Container, firm: str
) -> None:
    shouting = token_issuer.headers_for(AVERY, email=AVERY.email.upper())
    first = (await client.post("/api/v1/activation/employee", headers=shouting)).json()
    assert first["email"] == AVERY.email  # stored normalized

    again = await client.post(
        "/api/v1/activation/employee", headers=token_issuer.headers_for(AVERY)
    )
    assert again.status_code == 200
    assert again.json()["userId"] == first["userId"]
    async with container.uow() as tx:
        count = len((await tx.session.execute(select(user_accounts.c.id))).all())
    assert count == 1
    assert len(await _audit_rows(container, AuditAction.ACCOUNT_ACTIVATION)) == 1


async def test_a_new_subject_with_the_same_verified_email_reuses_the_account(
    container: Container, activate: Activate
) -> None:
    original = await activate(AVERY)
    moved = Persona("avery2", AVERY.display_name, AVERY.email.upper(), "sub-avery-new")
    reused = await container.organization.activate_employee(identity_of(moved))
    assert reused.user_id == original.user_id  # IAM-06: no duplicate account


async def test_unapproved_domain_is_refused_and_audited(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, container: Container, firm: str
) -> None:
    r = await client.post("/api/v1/activation/employee", headers=token_issuer.headers_for(JORDAN))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ACTIVATION_DOMAIN_NOT_APPROVED"
    events = await _audit_rows(container, AuditAction.ACCOUNT_ACTIVATION)
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == Outcome.FAILED.value
    assert event["facts"] == {"failureCode": "DomainNotApproved", "domainApproved": False}
    assert event["actor_user_id"] is None
    assert event["target_id"].startswith("sub_")
    assert JORDAN.subject not in str(dict(event))  # only a keyed hash of the subject
    assert JORDAN.email not in str(dict(event))


async def test_unverified_email_is_refused(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, firm: str
) -> None:
    for headers in (
        token_issuer.headers_for(AVERY, email_verified=False),
        token_issuer.headers_for(AVERY, email=None),
    ):
        r = await client.post("/api/v1/activation/employee", headers=headers)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


async def test_unverified_email_never_matches_an_existing_account(
    container: Container, activate: Activate
) -> None:
    await activate(AVERY)
    impostor = Persona("x", "X", AVERY.email, "sub-impostor")
    actor = await container.organization.resolve_actor(identity_of(impostor, email_verified=False))
    assert actor.user_id is None


async def test_removing_a_domain_stops_new_activations_only(
    container: Container, activate: Activate
) -> None:
    avery = await activate(AVERY)
    domain = AVERY.email.split("@")[1]
    await container.organization.remove_approved_domain(OperatorActor.cli(), domain)
    with pytest.raises(AppError) as refused:
        await activate(SAM)
    assert refused.value.code is ErrorCode.ACTIVATION_DOMAIN_NOT_APPROVED
    assert (await container.organization.resolve_actor(identity_of(AVERY))).user_id == avery.user_id


async def test_disabled_account_is_not_activated(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, container: Container,
    activate: Activate, frozen_clock: FrozenClock,
) -> None:  # fmt: skip
    avery = await activate(AVERY)
    async with container.uow() as tx:
        await tx.session.execute(
            update(user_accounts)
            .where(user_accounts.c.id == avery.user_id)
            .values(activation_state="Disabled", disabled_at=frozen_clock.now())
        )
    for method, path in (("GET", "/api/v1/me"), ("POST", "/api/v1/activation/employee")):
        r = await client.request(method, path, headers=token_issuer.headers_for(AVERY))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ACCOUNT_NOT_ACTIVATED"


async def test_every_persona_gets_a_token_and_activates(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, container: Container, firm: str
) -> None:
    for persona in TEAM_MEMBERS:
        r = await client.post(
            "/api/v1/activation/employee", headers=token_issuer.headers_for(persona)
        )
        assert r.status_code == 200, persona.key
        assert r.json()["personCategory"] == "TeamMember"
    # Jordan, a Client Collaborator, activates through invitation acceptance (T06).
    async with container.uow() as tx:
        jordan = await container.organization.activate_client_collaborator(tx, identity_of(JORDAN))
    me = (await client.get("/api/v1/me", headers=token_issuer.headers_for(JORDAN))).json()
    assert me["userId"] == jordan.user_id
    assert me["personCategory"] == "ClientCollaborator"
    assert me["globalCapabilities"] == []
    assert me["landing"] == {"kind": "ZeroState", "canCreateProject": False}
    events = await _audit_rows(container, AuditAction.ACCOUNT_ACTIVATION)
    # A Client Collaborator's real name never reaches the audit log (GOV-02).
    assert {e["actor_label"] for e in events if e["actor_user_id"] == jordan.user_id} == {
        "Client Collaborator"
    }


async def test_client_collaborator_stays_one_under_a_later_approved_domain(
    container: Container, firm: str
) -> None:
    async with container.uow() as tx:
        jordan = await container.organization.activate_client_collaborator(tx, identity_of(JORDAN))
    await container.organization.add_approved_domain(OperatorActor.cli(), CLIENT_DOMAIN)
    again = await container.organization.activate_employee(identity_of(JORDAN))
    assert again.user_id == jordan.user_id
    assert again.person_category == jordan.person_category  # documented limitation


# --------------------------------------------------------------------------- sign-in failures


async def test_sign_in_failures_are_audited_with_a_hashed_subject_and_rate_limited(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, container: Container,
    frozen_clock: FrozenClock,
) -> None:  # fmt: skip
    expired = token_issuer.headers_for(AVERY, expires_in=timedelta(hours=-1))
    for _ in range(5):
        assert (await client.get("/api/v1/me", headers=expired)).status_code == 401
    events = await _audit_rows(container, AuditAction.ACCOUNT_SIGN_IN_FAILED)
    assert len(events) == 1
    assert events[0]["facts"]["failureCode"] == "TokenExpired"
    assert events[0]["facts"]["subjectHash"] == events[0]["target_id"]
    assert AVERY.subject not in str(dict(events[0]))

    frozen_clock.advance(timedelta(minutes=2))
    expired = token_issuer.headers_for(AVERY, expires_in=timedelta(hours=-1))
    await client.get("/api/v1/me", headers=expired)
    assert len(await _audit_rows(container, AuditAction.ACCOUNT_SIGN_IN_FAILED)) == 2

    # A garbage token names no subject: logged as a metric, not audited.
    await client.get("/api/v1/me", headers={"Authorization": "Bearer garbage"})
    assert len(await _audit_rows(container, AuditAction.ACCOUNT_SIGN_IN_FAILED)) == 2


# --------------------------------------------------------------------------- operator


async def test_operator_me_without_an_account(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, firm: str
) -> None:
    me = (await client.get("/api/v1/me", headers=token_issuer.headers_for(OPS))).json()
    assert me["activation"] == "NotActivated"
    assert me["firmRoles"] == {"firmManager": False, "platformOperator": True}
    assert me["globalCapabilities"] == ["operator.enter"]


async def test_operator_domain_routes(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, container: Container, firm: str
) -> None:
    ops = token_issuer.headers_for(OPS)
    r = await client.post(
        "/api/v1/operator/domains", json={"domain": "Atlas-Client.TEST."}, headers=ops
    )
    assert r.status_code == 200
    assert [d["domain"] for d in r.json()["items"]] == [
        "atlas-client.test",
        "relaxai-consulting.test",
    ]
    # Adding again changes nothing.
    r = await client.post(
        "/api/v1/operator/domains", json={"domain": "atlas-client.test"}, headers=ops
    )
    assert len(r.json()["items"]) == 2

    r = await client.post("/api/v1/operator/domains", json={"domain": "not a domain"}, headers=ops)
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["field"] == "domain"

    r = await client.delete("/api/v1/operator/domains/atlas-client.test", headers=ops)
    assert [d["domain"] for d in r.json()["items"]] == ["relaxai-consulting.test"]
    r = await client.delete("/api/v1/operator/domains/atlas-client.test", headers=ops)
    assert r.status_code == 404

    added = await _audit_rows(container, AuditAction.DOMAIN_ADDED)
    removed = await _audit_rows(container, AuditAction.DOMAIN_REMOVED)
    assert [e["facts"]["domain"] for e in added] == ["relaxai-consulting.test", "atlas-client.test"]
    assert [e["facts"]["domain"] for e in removed] == ["atlas-client.test"]
    http_event = added[1]
    assert http_event["actor_kind"] == "PlatformOperator"
    assert http_event["actor_user_id"].startswith("sub_")  # the operator's keyed subject hash
    assert added[0]["actor_user_id"] == "cli:relaxai-admin"


async def test_first_firm_manager_bootstrap(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, container: Container,
    activate: Activate,
) -> None:  # fmt: skip
    sam = await activate(SAM)
    avery = await activate(AVERY)
    ops = token_issuer.headers_for(OPS)

    r = await client.post(
        "/api/v1/operator/firm-managers", json={"userId": sam.user_id}, headers=ops
    )
    assert r.status_code == 201
    assert r.json()["userId"] == sam.user_id
    assert r.json()["assignmentId"].startswith("fmn_")

    me = (await client.get("/api/v1/me", headers=token_issuer.headers_for(SAM))).json()
    assert me["firmRoles"] == {"firmManager": True, "platformOperator": False}
    assert me["globalCapabilities"] == ["project.create", "governance.enter"]
    roles = await container.organization.firm_roles(sam.user_id or "")
    assert roles.firm_manager is True

    # The bootstrap refuses a second "first" manager.
    r = await client.post(
        "/api/v1/operator/firm-managers", json={"userId": avery.user_id}, headers=ops
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ALREADY_EXISTS"
    with pytest.raises(AlreadyExists):
        await container.organization.appoint_first_firm_manager(
            OperatorActor.cli(), avery.user_id or ""
        )
    events = await _audit_rows(container, AuditAction.FIRM_MANAGER_APPOINTED)
    assert len(events) == 1
    assert events[0]["category"] == "Governance"


async def test_first_firm_manager_must_be_an_active_team_member(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, container: Container, firm: str
) -> None:
    async with container.uow() as tx:
        jordan = await container.organization.activate_client_collaborator(tx, identity_of(JORDAN))
    with pytest.raises(ValidationFailed):
        await container.organization.appoint_first_firm_manager(
            OperatorActor.cli(), jordan.user_id or ""
        )
    r = await client.post(
        "/api/v1/operator/firm-managers",
        json={"userId": "usr_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"},
        headers=token_issuer.headers_for(OPS),
    )
    assert r.status_code == 404


async def test_me_shows_only_a_verified_email(
    client: httpx.AsyncClient, token_issuer: TestTokenIssuer, firm: str
) -> None:
    headers = token_issuer.headers_for(AVERY, email_verified=False)
    me = (await client.get("/api/v1/me", headers=headers)).json()
    assert me["email"] == ""
