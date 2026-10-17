from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from app.modules.governance.domain import (
    AUDIT_CATALOG,
    BACKOFF_CAP,
    ActorKind,
    ActorRef,
    AuditAction,
    AuditCategory,
    JobState,
    Meter,
    Priority,
    QuotaPeriod,
    SafeFacts,
    SystemActor,
    TargetRef,
    actor_columns,
    check_job_payload,
    period_key,
    retry_delay,
)
from app.modules.governance.registry import DEFAULT_REGISTRY, SweepPayload
from app.platform.cron import CronSchedule


def test_job_state_terminals() -> None:
    from app.modules.governance.domain import TERMINAL_STATES

    assert {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED} == TERMINAL_STATES
    assert JobState.WAITING_FOR_USER not in TERMINAL_STATES


@pytest.mark.parametrize(
    ("attempt", "lo", "hi"),
    [(1, 1.6, 2.4), (2, 3.2, 4.8), (3, 6.4, 9.6), (5, 25.6, 38.4)],
)
def test_retry_backoff_is_exponential_with_jitter(attempt: int, lo: float, hi: float) -> None:
    for seed in range(20):
        delay = retry_delay(attempt, rng=random.Random(seed))  # noqa: S311
        assert timedelta(seconds=lo) <= delay <= timedelta(seconds=hi)


def test_retry_backoff_is_capped() -> None:
    assert retry_delay(30, rng=random.Random(1)) == BACKOFF_CAP  # noqa: S311
    assert retry_delay(1, retry_after=timedelta(hours=3)) == BACKOFF_CAP
    assert retry_delay(1, retry_after=timedelta(seconds=12)) == timedelta(seconds=12)


def test_priority_ordering_is_descending() -> None:
    assert Priority.CRITICAL > Priority.HIGH > Priority.NORMAL > Priority.LOW


def test_period_key() -> None:
    now = datetime(2026, 3, 9, tzinfo=UTC)
    assert period_key(QuotaPeriod.TOTAL, now) == "total"
    assert period_key(QuotaPeriod.MONTHLY, now) == "2026-03"


# --------------------------------------------------------------------------- audit catalog


def test_every_action_has_a_category_and_allowlisted_facts() -> None:
    assert set(AUDIT_CATALOG) == set(AuditAction)
    for action, spec in AUDIT_CATALOG.items():
        assert isinstance(spec.category, AuditCategory), action
        assert spec.facts, action
        assert all(key[0].islower() for key in spec.facts), action


def test_project_actions_are_project_scoped_and_governance_actions_are_not() -> None:
    for action in (AuditAction.DOCUMENT_ACCEPTED, AuditAction.TASK_STATUS_CHANGED):
        assert AUDIT_CATALOG[action].category is AuditCategory.PROJECT
    for action in (AuditAction.IDENTITY_REVEALED, AuditAction.ERASURE_DECIDED):
        assert AUDIT_CATALOG[action].category is AuditCategory.GOVERNANCE
    for action in (AuditAction.QUOTA_CHANGED, AuditAction.RETENTION_EXECUTED):
        assert AUDIT_CATALOG[action].category is AuditCategory.OPERATIONAL


def test_reasoned_actions_require_a_reason() -> None:
    reasoned = {a for a, s in AUDIT_CATALOG.items() if s.reason_required}
    assert {
        AuditAction.IDENTITY_REVEALED,
        AuditAction.ERASURE_DECIDED,
        AuditAction.EXTRACTED_ITEM_REJECTED,
        AuditAction.FIRM_MANAGER_REMOVED,
    } <= reasoned


# --------------------------------------------------------------------------- SafeFacts


def test_safe_facts_accept_the_contract_value_types() -> None:
    facts = SafeFacts(
        {
            "meter": "OcrPages",
            "count": 3,
            "confidence": 0.72,
            "edited": True,
            "at": datetime(2026, 1, 1, tzinfo=UTC),
            "missing": None,
        }
    )
    assert dict(facts)["count"] == 3
    assert facts.to_json()["at"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "value",
    [
        {"nested": "dict"},
        ["a", "list"],
        ("a", "tuple"),
        {"a", 1},
        b"bytes",
        3 + 4j,
    ],
)
def test_safe_facts_reject_structured_values(value: object) -> None:
    with pytest.raises(TypeError, match="not allowed"):
        SafeFacts({"value": value})


def test_safe_facts_reject_free_text_and_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="personal data"):
        SafeFacts({"reason": "Riley asked for the Atlas Expansion numbers"})
    with pytest.raises(ValueError, match="personal data"):
        SafeFacts({"value": "secret-name@example.com"})
    with pytest.raises(ValueError, match="free-text"):
        SafeFacts({"value": "two words"})
    with pytest.raises(ValueError, match="timezone-aware"):
        SafeFacts({"at": datetime(2026, 1, 1)})  # noqa: DTZ001


def test_safe_facts_keys_must_be_camel_case() -> None:
    with pytest.raises(ValueError, match="camelCase"):
        SafeFacts({"not a key": 1})
    with pytest.raises(ValueError, match="camelCase"):
        SafeFacts({"snake_case": 1})


def test_target_refs_reject_free_text() -> None:
    assert TargetRef("document_projection", "doc_01J9Z3Q8M8V3W7G2E1N4R5T6Y7").type
    with pytest.raises(ValueError, match="snake_case"):
        TargetRef("Document Projection", "doc_1")
    with pytest.raises(ValueError, match="free-text"):
        TargetRef("document", "Atlas Expansion contract")


# --------------------------------------------------------------------------- actors


def test_client_collaborator_label_is_never_a_real_name() -> None:
    actor = ActorRef("usr_1", ActorKind.CLIENT_COLLABORATOR, "Riley Client")
    assert actor.stored_label == "Client Collaborator"
    assert actor_columns(actor) == ("User", "usr_1", "Client Collaborator")


def test_actor_columns_for_team_member_operator_and_system() -> None:
    team = ActorRef("usr_2", ActorKind.TEAM_MEMBER, "Avery Chen")
    assert actor_columns(team) == ("User", "usr_2", "Avery Chen")
    operator = ActorRef("usr_3", ActorKind.PLATFORM_OPERATOR, "Platform Operator")
    assert actor_columns(operator) == ("PlatformOperator", "usr_3", "Platform Operator")
    assert actor_columns(SystemActor()) == ("System", None, "System")


# --------------------------------------------------------------------------- job payloads


class GoodPayload(BaseModel):
    document_id: str
    visibility: str
    page_count: int
    attachment_ids: list[str]


class ContentPayload(BaseModel):
    text: str


class EmailPayload(BaseModel):
    email_address: str


class FreeTextValuePayload(BaseModel):
    label: str


def test_payload_accepts_opaque_ids_and_enums() -> None:
    check_job_payload(
        GoodPayload(
            document_id="doc_01J9Z3Q8M8V3W7G2E1N4R5T6Y7",
            visibility="ClientVisible",
            page_count=12,
            attachment_ids=["cit_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"],
        )
    )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (ContentPayload(text="quarterly numbers"), "forbidden"),
        (EmailPayload(email_address="secret-name@example.com"), "forbidden"),
        (FreeTextValuePayload(label="Atlas Expansion"), "free-text"),
    ],
)
def test_payload_rejects_content_and_personal_data(payload: BaseModel, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        check_job_payload(payload)


class SnakeCaseContent(BaseModel):
    document_text: str


def test_snake_case_content_keys_are_caught() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        check_job_payload(SnakeCaseContent(document_text="x"))


# --------------------------------------------------------------------------- registry


def test_default_registry_exposes_the_probe_handler() -> None:
    spec = DEFAULT_REGISTRY["governance.worker_probe"]
    assert not spec.paid
    assert spec.concurrency == 1
    assert spec.lease_duration == timedelta(minutes=4)


def test_registry_rejects_duplicates_and_unknown_dependencies() -> None:
    from app.modules.governance.registry import JobRegistry

    reg = JobRegistry()
    reg.handler(
        "x",
        payload=GoodPayload,
        max_attempts=1,
        concurrency=1,
        priority=Priority.LOW,
        timeout=timedelta(seconds=5),
    )(lambda ctx, payload: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(ValueError, match="already registered"):
        reg.handler(
            "x",
            payload=GoodPayload,
            max_attempts=1,
            concurrency=1,
            priority=Priority.LOW,
            timeout=timedelta(seconds=5),
        )(lambda ctx, payload: None)  # type: ignore[arg-type,return-value]
    reg.handler(
        "y",
        payload=GoodPayload,
        max_attempts=1,
        concurrency=1,
        priority=Priority.LOW,
        timeout=timedelta(seconds=5),
        depends_on=("nope",),
    )(lambda ctx, payload: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(ValueError, match="unregistered kinds"):
        reg.validate()


# --------------------------------------------------------------------------- cron


def test_cron_matches_daily_three_am() -> None:
    schedule = CronSchedule.parse("0 3 * * *")
    assert schedule.matches(datetime(2026, 5, 4, 3, 0, tzinfo=UTC))
    assert not schedule.matches(datetime(2026, 5, 4, 4, 0, tzinfo=UTC))


def test_cron_weekday_list() -> None:
    weekdays = CronSchedule.parse("30 7 * * 1-5")
    assert weekdays.matches(datetime(2026, 5, 4, 7, 30, tzinfo=UTC))  # Monday
    assert not weekdays.matches(datetime(2026, 5, 3, 7, 30, tzinfo=UTC))  # Sunday


def test_cron_day_or_weekday_semantics() -> None:
    schedule = CronSchedule.parse("0 0 1 * 1")
    assert schedule.matches(datetime(2026, 6, 1, tzinfo=UTC))  # first of the month
    assert schedule.matches(datetime(2026, 6, 8, tzinfo=UTC))  # a Monday
    assert not schedule.matches(datetime(2026, 6, 9, tzinfo=UTC))


def test_cron_steps_and_fire_times() -> None:
    schedule = CronSchedule.parse("*/15 * * * *")
    times = schedule.fire_times(
        datetime(2026, 5, 4, 3, 0, tzinfo=UTC), datetime(2026, 5, 4, 4, 0, tzinfo=UTC)
    )
    assert times == [
        datetime(2026, 5, 4, 3, 15, tzinfo=UTC),
        datetime(2026, 5, 4, 3, 30, tzinfo=UTC),
        datetime(2026, 5, 4, 3, 45, tzinfo=UTC),
        datetime(2026, 5, 4, 4, 0, tzinfo=UTC),
    ]


@pytest.mark.parametrize("expression", ["", "* * * *", "60 * * * *", "* 24 * * *", "* * * * 9"])
def test_cron_rejects_invalid_expressions(expression: str) -> None:
    with pytest.raises(ValueError, match="cron"):
        CronSchedule.parse(expression)


def test_sweep_payload_requires_aware_datetime() -> None:
    SweepPayload(scheduled_for=datetime(2026, 5, 4, 3, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        SweepPayload(scheduled_for=datetime(2026, 5, 4, 3, 0))  # noqa: DTZ001


def test_meters_are_the_contract_meters() -> None:
    assert [m.value for m in Meter] == [
        "AiTokensIn",
        "AiTokensOut",
        "EmbeddingTokens",
        "OcrPages",
        "BlobBytes",
    ]
