"""organization_access domain rules (T03): activation policy, domains, capabilities."""

from __future__ import annotations

import pytest

from app.modules.organization_access import Activation, Actor, PersonCategory
from app.modules.organization_access.domain import (
    ActivationFailure,
    GlobalCapability,
    authorization_version,
    employee_activation_failure,
    global_capabilities,
    normalize_domain,
    subject_reference,
)
from app.platform.auth import VerifiedIdentity

APPROVED = frozenset({"relaxai-consulting.test", "xn--bcher-kva.test"})


def _identity(email: str | None, *, verified: bool = True) -> VerifiedIdentity:
    return VerifiedIdentity("sub-1", "tid", email, verified, "Avery Chen")


@pytest.mark.parametrize(
    ("email", "verified", "failure"),
    [
        ("avery.chen@relaxai-consulting.test", True, None),
        ("avery.chen@relaxai-consulting.test", False, ActivationFailure.EMAIL_NOT_VERIFIED),
        (None, True, ActivationFailure.EMAIL_NOT_VERIFIED),
        ("jordan@atlas-client.test", True, ActivationFailure.DOMAIN_NOT_APPROVED),
        ("x@sub.relaxai-consulting.test", True, ActivationFailure.DOMAIN_NOT_APPROVED),
        ("x@bücher.test", True, None),  # IDN email matches the punycode approval
        ("x@relaxai-consulting.test.", True, None),
    ],
)
def test_employee_activation_policy(
    email: str | None, verified: bool, failure: ActivationFailure | None
) -> None:
    assert employee_activation_failure(_identity(email, verified=verified), APPROVED) is failure


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("RelaxAI-Consulting.TEST", "relaxai-consulting.test"),
        (" example.com. ", "example.com"),
        ("bücher.test", "xn--bcher-kva.test"),
    ],
)
def test_normalize_domain(raw: str, normalized: str) -> None:
    assert normalize_domain(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    [
        "localhost",
        "not a domain",
        "-bad.test",
        "bad-.test",
        "a..test",
        "1.2.3.4",
        "x" * 64 + ".test",
    ],
)
def test_normalize_domain_rejects_non_domains(raw: str) -> None:
    with pytest.raises(ValueError, match="domain"):
        normalize_domain(raw)


def _actor(**overrides: object) -> Actor:
    values: dict[str, object] = {
        "user_id": "usr_1", "firm_id": "frm_1", "subject": "s", "email": "a@b.test",
        "display_name": "A", "person_category": PersonCategory.TEAM_MEMBER,
        "activation": Activation.ACTIVE, "firm_manager": False, "platform_operator": False,
    }  # fmt: skip
    values.update(overrides)
    return Actor(**values)  # type: ignore[arg-type]


def test_global_capabilities_by_role() -> None:
    cap = GlobalCapability
    assert global_capabilities(_actor()) == [cap.PROJECT_CREATE]
    assert global_capabilities(_actor(firm_manager=True)) == [
        cap.PROJECT_CREATE,
        cap.GOVERNANCE_ENTER,
    ]
    assert global_capabilities(_actor(person_category=PersonCategory.CLIENT_COLLABORATOR)) == []
    not_active = _actor(user_id=None, activation=Activation.NOT_ACTIVATED, person_category=None,
                        firm_manager=True, platform_operator=True)  # fmt: skip
    assert global_capabilities(not_active) == [cap.OPERATOR_ENTER]


def test_authorization_version_tracks_navigation_inputs() -> None:
    base = authorization_version(_actor())
    assert base == authorization_version(_actor(display_name="Renamed"))
    assert base != authorization_version(_actor(firm_manager=True))
    assert base != authorization_version(_actor(platform_operator=True))


def test_subject_reference_is_keyed_and_stable() -> None:
    a = subject_reference("sub-avery", b"k" * 32)
    assert a == subject_reference("sub-avery", b"k" * 32)
    assert a != subject_reference("sub-avery", b"j" * 32)
    assert a.startswith("sub_")
    assert "avery" not in a
    assert len(a) <= 40
