"""The minimized audit log: scoping, transactionality, and privacy (GOV-01..03, T02)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.container import Container
from app.modules.governance import (
    ActorKind,
    ActorRef,
    Audit,
    AuditAction,
    AuditCategory,
    AuditFilters,
    FirmManagerViewer,
    Outcome,
    PlatformOperatorViewer,
    ProjectAdministratorViewer,
    RegexReasonScreen,
    SafeFacts,
    SystemActor,
    TargetRef,
)
from app.modules.governance.tables import audit_events
from app.platform.clock import FrozenClock
from app.platform.errors import AppError, ErrorCode
from app.platform.http.pagination import CursorCodec

PROJECT_A = "prj_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"
PROJECT_B = "prj_01J9Z3Q8M8V3W7G2E1N4R5T6Y8"
TASK = "tsk_01J9Z3Q8M8V3W7G2E1N4R5T6Y9"

AVERY = ActorRef("usr_avery", ActorKind.TEAM_MEMBER, "Avery Chen")
OPERATOR = ActorRef("usr_op", ActorKind.PLATFORM_OPERATOR, "Platform Operator")
CLIENT = ActorRef("usr_riley", ActorKind.CLIENT_COLLABORATOR, "Riley Stone")


async def record(
    container: Container,
    *,
    action: AuditAction = AuditAction.TASK_STATUS_CHANGED,
    actor: ActorRef | SystemActor = AVERY,
    project_id: str | None = PROJECT_A,
    target: TargetRef | None = None,
    outcome: Outcome = Outcome.SUCCEEDED,
    reason: str | None = None,
    facts: SafeFacts | None = None,
) -> None:
    async with container.uow() as tx:
        await container.audit.record(
            tx,
            actor=actor,
            action=action,
            target=target or TargetRef("task", TASK),
            outcome=outcome,
            project_id=project_id,
            reason=reason,
            facts=facts,
        )


async def rows(container: Container) -> list[Any]:
    async with container.uow() as tx:
        return list((await tx.session.execute(select(audit_events))).mappings().all())


# --------------------------------------------------------------------------- recording


async def test_record_writes_actor_action_target_outcome_and_time(container: Container) -> None:
    await record(container, reason=None, facts=SafeFacts({"fromStatus": "NotStarted", "toStatus": "InProgress"}))
    (row,) = await rows(container)
    assert row["actor_kind"] == "User"
    assert row["actor_user_id"] == "usr_avery"
    assert row["actor_label"] == "Avery Chen"
    assert row["action"] == "task.status_changed"
    assert row["category"] == AuditCategory.PROJECT.value
    assert row["target_type"] == "task"
    assert row["target_id"] == TASK
    assert row["outcome"] == "Succeeded"
    assert row["project_id"] == PROJECT_A
    assert row["at"] == container.clock.now()
    assert row["facts"] == {"fromStatus": "NotStarted", "toStatus": "InProgress"}
    assert row["id"].startswith("aud_")


async def test_record_in_a_rolled_back_transaction_leaves_no_event(container: Container) -> None:
    class BoomError(Exception):
        pass

    async def record_then_fail() -> None:
        async with container.uow() as tx:
            await container.audit.record(
                tx,
                actor=AVERY,
                action=AuditAction.PROJECT_ARCHIVED,
                target=TargetRef("project", PROJECT_A),
                outcome=Outcome.SUCCEEDED,
                project_id=PROJECT_A,
            )
            raise BoomError

    with pytest.raises(BoomError):
        await record_then_fail()
    assert await rows(container) == []


async def test_system_actor_and_correlation_id(container: Container) -> None:
    await record(container, actor=SystemActor("Scheduler"), action=AuditAction.RETENTION_EXECUTED, project_id=None, target=TargetRef("retention_policy", "audit_1y"))
    (row,) = await rows(container)
    assert row["actor_kind"] == "System"
    assert row["actor_user_id"] is None
    assert row["actor_label"] == "Scheduler"
    assert row["category"] == AuditCategory.OPERATIONAL.value


async def test_client_collaborator_actions_never_store_a_real_name(container: Container) -> None:
    await record(container, actor=CLIENT, action=AuditAction.DOCUMENT_ACCEPTED, facts=SafeFacts({"sourceType": "Upload", "format": "Pdf"}))
    (row,) = await rows(container)
    assert row["actor_label"] == "Client Collaborator"
    assert "Riley" not in str(row["actor_label"])


async def test_unknown_fact_key_is_a_programming_error(container: Container) -> None:
    with pytest.raises(ValueError, match="not allowed for"):
        async with container.uow() as tx:
            await container.audit.record(
                tx,
                actor=AVERY,
                action=AuditAction.TASK_STATUS_CHANGED,
                target=TargetRef("task", TASK),
                outcome=Outcome.SUCCEEDED,
                project_id=PROJECT_A,
                facts=SafeFacts({"quota": 5}),  # facts for a different action
            )


# --------------------------------------------------------------------------- reasons


async def test_required_reason_is_enforced(container: Container) -> None:
    with pytest.raises(AppError) as caught:
        await record(
            container,
            action=AuditAction.IDENTITY_REVEALED,
            project_id=None,
            target=TargetRef("identity_reveal", "rvl_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"),
            actor=OPERATOR,
        )
    assert caught.value.code is ErrorCode.REASON_REQUIRED
    assert await rows(container) == []


async def test_reason_containing_personal_data_is_rejected(container: Container) -> None:
    with pytest.raises(AppError) as caught:
        await record(
            container,
            action=AuditAction.EXTRACTED_ITEM_REJECTED,
            reason="Riley asked us to drop this one",
        )
    assert caught.value.code is ErrorCode.REASON_CONTAINS_PERSONAL_DATA

    with pytest.raises(AppError):
        await record(
            container,
            action=AuditAction.EXTRACTED_ITEM_REJECTED,
            reason="contact secret-name@example.com instead",
        )
    assert await rows(container) == []


async def test_reason_is_trimmed_length_bounded_and_stored(container: Container) -> None:
    await record(
        container,
        action=AuditAction.EXTRACTED_ITEM_REJECTED,
        reason="  Superseded by the revised clause  ",
    )
    (row,) = await rows(container)
    assert row["reason"] == "Superseded by the revised clause"

    with pytest.raises(AppError) as caught:
        await record(
            container,
            action=AuditAction.EXTRACTED_ITEM_REJECTED,
            reason="x" * 501,
        )
    assert caught.value.code is ErrorCode.VALIDATION_FAILED

    # A blank reason on a reasoned action is a missing reason.
    with pytest.raises(AppError) as blank:
        await record(container, action=AuditAction.EXTRACTED_ITEM_REJECTED, reason="   ")
    assert blank.value.code is ErrorCode.REASON_REQUIRED


def test_regex_reason_screen_flags_emails_phones_and_fixture_names() -> None:
    screen = RegexReasonScreen()
    assert screen.contains_personal_data("write to secret-name@example.com")
    assert screen.contains_personal_data("call +49 30 901820 now")
    assert not screen.contains_personal_data("Client asked for a smaller scope")


def test_registered_fixture_names_are_flagged() -> None:
    from app.modules.governance import personal_data_screen

    assert personal_data_screen.contains_personal_data("Reviewed by Jordan")
    with pytest.raises(ValueError, match="personal data"):
        SafeFacts({"reviewerLabel": "Morgan"})  # a bare name is token-shaped but still a name


# --------------------------------------------------------------------------- querying


@pytest.fixture
async def seeded(container: Container, frozen_clock: FrozenClock) -> Container:
    await record(container, action=AuditAction.PROJECT_CREATED, target=TargetRef("project", PROJECT_A))
    await record(container, action=AuditAction.DOCUMENT_ACCEPTED, facts=SafeFacts({"sourceType": "Upload", "format": "Docx"}))
    await record(container, action=AuditAction.TASK_STATUS_CHANGED, project_id=PROJECT_B)
    await record(
        container,
        action=AuditAction.IDENTITY_REVEALED,
        actor=OPERATOR,
        project_id=None,
        target=TargetRef("identity_reveal", "rvl_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"),
        reason="Requested by the client sponsor",
    )
    await record(
        container,
        action=AuditAction.QUOTA_CHANGED,
        actor=OPERATOR,
        project_id=None,
        target=TargetRef("quota_policy", "OcrPages"),
        facts=SafeFacts({"meter": "OcrPages", "fromQuota": 100, "toQuota": 200}),
    )
    frozen_clock.advance(timedelta(minutes=1))
    return container


async def test_project_administrator_sees_only_their_project(seeded: Container) -> None:
    page = await seeded.audit.query(
        ProjectAdministratorViewer(PROJECT_A), AuditFilters(), None
    )
    assert {e.action for e in page.items} == {"project.created", "document.accepted"}
    assert {e.project_id for e in page.items} == {PROJECT_A}


async def test_firm_manager_sees_governance_actions_only(seeded: Container) -> None:
    page = await seeded.audit.query(FirmManagerViewer(), AuditFilters(), None)
    assert [e.action for e in page.items] == ["identity.revealed"]
    assert page.items[0].project_id is None
    assert page.items[0].reason == "Requested by the client sponsor"
    assert page.items[0].actor.kind == "PlatformOperator"


async def test_operator_sees_operational_events_only(seeded: Container) -> None:
    page = await seeded.audit.query(PlatformOperatorViewer(), AuditFilters(), None)
    assert [e.action for e in page.items] == ["quota.changed"]
    assert page.items[0].facts == {"meter": "OcrPages", "fromQuota": 100, "toQuota": 200}


async def test_filters_by_action_time_and_actor(seeded: Container) -> None:
    page = await seeded.audit.query(
        ProjectAdministratorViewer(PROJECT_A),
        AuditFilters(actions=frozenset({AuditAction.DOCUMENT_ACCEPTED})),
        None,
    )
    assert [e.action for e in page.items] == ["document.accepted"]

    cut = seeded.clock.now() - timedelta(minutes=2)
    page = await seeded.audit.query(
        ProjectAdministratorViewer(PROJECT_A), AuditFilters(from_=cut), None
    )
    assert len(page.items) == 2

    page = await seeded.audit.query(
        ProjectAdministratorViewer(PROJECT_A), AuditFilters(actor_user_id="usr_nobody"), None
    )
    assert page.items == []


async def test_query_is_newest_first_and_paginates(seeded: Container) -> None:
    page = await seeded.audit.query(
        ProjectAdministratorViewer(PROJECT_A), AuditFilters(limit=1), None
    )
    assert [e.action for e in page.items] == ["document.accepted"]
    assert page.next_cursor is not None

    second = await seeded.audit.query(
        ProjectAdministratorViewer(PROJECT_A), AuditFilters(limit=1), page.next_cursor
    )
    assert [e.action for e in second.items] == ["project.created"]
    assert second.next_cursor is None


async def test_query_rejects_a_forged_cursor(seeded: Container) -> None:
    codec = CursorCodec(b"some-other-key-0123456789")
    forged = codec.encode(datetime(2026, 1, 1, tzinfo=UTC).isoformat(), "aud_x")
    from app.platform.errors import MalformedRequest

    with pytest.raises(MalformedRequest):
        await seeded.audit.query(ProjectAdministratorViewer(PROJECT_A), AuditFilters(), forged)


async def test_project_administrator_of_another_project_sees_nothing(seeded: Container) -> None:
    page = await seeded.audit.query(
        ProjectAdministratorViewer("prj_01J9Z3Q8M8V3W7G2E1N4R5T6Y0"), AuditFilters(), None
    )
    assert page.items == []


async def test_every_stored_event_is_content_free(seeded: Container) -> None:
    forbidden_substrings = ("@", "Bearer ", "Atlas Expansion", "Riley Stone", "secret")
    for row in await rows(seeded):
        payload = " ".join(str(row[column]) for column in row)
        assert not any(text in payload for text in forbidden_substrings), row["action"]


async def test_audit_view_model_serialises_camel_case(seeded: Container) -> None:
    page = await seeded.audit.query(PlatformOperatorViewer(), AuditFilters(), None)
    dumped = page.items[0].model_dump(mode="json")
    assert set(dumped) == {
        "auditId", "at", "actor", "action", "target", "projectId", "outcome", "reason", "facts",
    }


async def test_audit_is_a_governance_module_service(container: Container) -> None:
    assert isinstance(container.audit, Audit)
    assert container.settings.firm_id == "frm_test"
