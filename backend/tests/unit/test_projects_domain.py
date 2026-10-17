"""Pure projects rules (T05-T07): capability matrix, landing, codes, candidacy, invitation
credentials and expiry, the lifecycle state machine, and the unforgeable scope objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.organization_access import Activation, Actor, PersonCategory
from app.modules.projects import (
    AttemptLimiter,
    Capability,
    InvitationState,
    LandingKind,
    Lifecycle,
    ProjectRole,
    ProjectScope,
    ScopeSet,
    Visibility,
    scope_key,
)
from app.modules.projects.domain import (
    LifecycleCommand,
    allowed_commands,
    capabilities_for,
    code_prefix,
    confirmation_matches,
    credential_digest,
    effective_state,
    is_archival_candidate,
    landing_for,
    mask_email,
    new_credential,
    next_lifecycle,
    normalize_invitee_email,
    project_code,
    role_change,
    role_holds,
    well_formed_credential,
)
from app.modules.projects.membership import person_ref_id
from app.platform.clock import FrozenClock
from app.platform.errors import RateLimited

C = Capability
R = ProjectRole
UTC_ZONE = ZoneInfo("UTC")

# --------------------------------------------------------------------------- matrix

# Independent transcription of the T05 table (expected values do not come from the code).
EXPECTED = {
    C.PROJECT_READ: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER, R.CLIENT_COLLABORATOR},
    C.KNOWLEDGE_READ: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER, R.CLIENT_COLLABORATOR},
    C.SEARCH: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER, R.CLIENT_COLLABORATOR},
    C.CHAT: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER, R.CLIENT_COLLABORATOR},
    C.TIMELINE_READ: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER, R.CLIENT_COLLABORATOR},
    C.DOCUMENT_UPLOAD: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.CLIENT_COLLABORATOR},
    C.SOURCE_CONNECT: {R.ADMINISTRATOR, R.CONTRIBUTOR},
    C.SOURCE_DISABLE: {R.ADMINISTRATOR},
    C.INGESTION_VIEW: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER, R.CLIENT_COLLABORATOR},
    C.REVIEW_PSEUDONYMIZATION: {R.ADMINISTRATOR, R.CONTRIBUTOR},
    C.KNOWLEDGE_REVIEW: {R.ADMINISTRATOR, R.CONTRIBUTOR},
    C.VISIBILITY_CHANGE: {R.ADMINISTRATOR, R.CONTRIBUTOR},
    C.RECOMMENDATION_REVIEW: {R.ADMINISTRATOR, R.CONTRIBUTOR},
    C.NETWORK_READ: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER},
    C.WORKLOAD_READ: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER},
    C.RECOMMENDATION_READ: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER},
    C.TASK_BOARD_READ: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.VIEWER},
    C.TASK_ADMINISTER: {R.ADMINISTRATOR, R.CONTRIBUTOR},
    C.TASK_REOPEN: {R.ADMINISTRATOR},
    C.TASK_OWN_UPDATE: {R.ADMINISTRATOR, R.CONTRIBUTOR, R.CLIENT_COLLABORATOR},
    C.MEMBERSHIP_MANAGE: {R.ADMINISTRATOR},
    C.LIFECYCLE_MANAGE: {R.ADMINISTRATOR},
    C.AUDIT_PROJECT_READ: {R.ADMINISTRATOR},
    C.PROJECT_UPDATE: {R.ADMINISTRATOR},
    C.PLANNER_CONNECT: {R.ADMINISTRATOR},
    C.PLANNER_DRAFT: {R.ADMINISTRATOR, R.CONTRIBUTOR},
}


def test_capability_list_is_exactly_the_contract_list() -> None:
    assert {c.value for c in Capability} == {
        "project.read", "project.update", "membership.manage", "lifecycle.manage",
        "document.upload", "source.connect", "source.disable", "ingestion.view",
        "review.pseudonymization", "knowledge.read", "knowledge.review", "visibility.change",
        "search", "chat", "timeline.read", "network.read", "workload.read",
        "recommendation.read", "recommendation.review", "task.board.read", "task.administer",
        "task.reopen", "task.own.update", "planner.connect", "planner.draft",
        "audit.project.read",
    }  # fmt: skip


@pytest.mark.parametrize("capability", list(Capability))
@pytest.mark.parametrize("role", list(ProjectRole))
def test_matrix_matches_t05_table(capability: Capability, role: ProjectRole) -> None:
    assert role_holds(role, capability) == (role in EXPECTED[capability])


def test_client_collaborator_never_holds_internal_views() -> None:
    for capability in (C.NETWORK_READ, C.WORKLOAD_READ, C.RECOMMENDATION_READ, C.MEMBERSHIP_MANAGE):
        assert capability not in capabilities_for(R.CLIENT_COLLABORATOR, Lifecycle.ACTIVE)


def test_archived_keeps_reads_and_administration_only() -> None:
    archived = set(capabilities_for(R.ADMINISTRATOR, Lifecycle.ARCHIVED))
    assert {C.PROJECT_READ, C.KNOWLEDGE_READ, C.LIFECYCLE_MANAGE} <= archived
    assert not {C.DOCUMENT_UPLOAD, C.PROJECT_UPDATE, C.TASK_ADMINISTER, C.CHAT} & archived


def test_pending_deletion_offers_recovery_to_administrators_only() -> None:
    assert capabilities_for(R.ADMINISTRATOR, Lifecycle.PENDING_DELETION) == [
        C.PROJECT_READ,
        C.LIFECYCLE_MANAGE,
    ]
    assert capabilities_for(R.CONTRIBUTOR, Lifecycle.PENDING_DELETION) == []
    assert capabilities_for(R.ADMINISTRATOR, Lifecycle.DELETED) == []


def test_role_change_preview_viewer_to_contributor() -> None:
    change = role_change(R.VIEWER, R.CONTRIBUTOR)
    assert C.DOCUMENT_UPLOAD in change.gained
    assert C.KNOWLEDGE_REVIEW in change.gained
    assert change.lost == []


def test_role_change_preview_administrator_to_viewer() -> None:
    change = role_change(R.ADMINISTRATOR, R.VIEWER)
    assert change.gained == []
    assert {C.MEMBERSHIP_MANAGE, C.LIFECYCLE_MANAGE, C.DOCUMENT_UPLOAD} <= set(change.lost)
    assert C.PROJECT_READ not in change.lost


# --------------------------------------------------------------------------- landing


def _landing(**overrides: object) -> LandingKind:
    args: dict[str, object] = {
        "activated": True,
        "team_member": True,
        "firm_manager": False,
        "active_project_ids": ["prj_a"],
        "archived_count": 0,
    }
    args.update(overrides)
    return landing_for(**args).kind  # type: ignore[arg-type]


def test_landing_rules() -> None:
    assert _landing(activated=False) is LandingKind.ACTIVATION
    assert _landing() is LandingKind.HOME
    assert _landing(team_member=False, active_project_ids=["prj_a"]) is LandingKind.PROJECT
    assert (
        _landing(team_member=False, active_project_ids=["prj_a", "prj_b"])
        is LandingKind.PROJECT_CHOOSER
    )
    assert _landing(active_project_ids=[]) is LandingKind.ZERO_STATE
    assert _landing(firm_manager=True, active_project_ids=[]) is LandingKind.HOME


def test_zero_state_offers_creation_to_team_members_only() -> None:
    team = landing_for(
        activated=True, team_member=True, firm_manager=False, active_project_ids=[],
        archived_count=0,
    )  # fmt: skip
    client = landing_for(
        activated=True, team_member=False, firm_manager=False, active_project_ids=[],
        archived_count=0,
    )  # fmt: skip
    assert team.can_create_project is True
    assert client.can_create_project is False


def test_single_project_landing_names_the_project() -> None:
    landing = landing_for(
        activated=True, team_member=False, firm_manager=False, active_project_ids=["prj_x"],
        archived_count=0,
    )  # fmt: skip
    assert landing.project_id == "prj_x"


# --------------------------------------------------------------------------- codes


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("Atlas Expansion", "ATL"),
        ("Northstar Transformation", "NOR"),
        ("AI Ops", "AIO"),
        ("Q", "QXX"),
        ("2026 - ???", "PRJ"),
    ],
)
def test_code_prefix(name: str, prefix: str) -> None:
    assert code_prefix(name) == prefix


def test_project_code_format() -> None:
    assert project_code("Atlas Expansion", 1) == "ATL-001"
    assert project_code("Harbor Analytics", 42) == "HAR-042"


# --------------------------------------------------------------------------- candidacy

ACTIVITY = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
CREATED = datetime(2025, 12, 1, tzinfo=UTC)


def test_exactly_two_calendar_months_is_not_a_candidate() -> None:
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    assert not is_archival_candidate(ACTIVITY, CREATED, now, UTC_ZONE)


def test_two_months_and_a_day_is_a_candidate() -> None:
    now = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
    assert is_archival_candidate(ACTIVITY, CREATED, now, UTC_ZONE)


def test_calendar_months_not_sixty_days() -> None:
    # 10 Jan + 60 days = 11 Mar; still not past two calendar months at 10 Mar 11:00.
    assert not is_archival_candidate(
        ACTIVITY, CREATED, datetime(2026, 3, 10, 11, 0, tzinfo=UTC), UTC_ZONE
    )


def test_month_end_activity_31_january() -> None:
    activity = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
    assert not is_archival_candidate(
        activity, CREATED, datetime(2026, 3, 31, 9, 0, tzinfo=UTC), UTC_ZONE
    )
    assert is_archival_candidate(
        activity, CREATED, datetime(2026, 3, 31, 9, 0, 1, tzinfo=UTC), UTC_ZONE
    )


def test_creation_counts_when_there_is_no_activity() -> None:
    assert is_archival_candidate(None, CREATED, datetime(2026, 2, 2, tzinfo=UTC), UTC_ZONE)
    assert not is_archival_candidate(None, CREATED, datetime(2026, 2, 1, tzinfo=UTC), UTC_ZONE)


def test_candidacy_uses_the_firm_calendar() -> None:
    helsinki = ZoneInfo("Europe/Helsinki")
    # 30 Nov 23:30 UTC is already 1 Dec 01:30 in Helsinki, so two calendar months later is
    # 1 Feb 01:30 local (31 Jan 23:30 UTC) there, but 30 Jan 23:30 on the UTC calendar.
    activity = datetime(2025, 11, 30, 23, 30, tzinfo=UTC)
    now = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    assert not is_archival_candidate(activity, activity, now, helsinki)
    assert is_archival_candidate(activity, activity, now, UTC_ZONE)


# --------------------------------------------------------------------------- lifecycle


def test_state_machine() -> None:
    assert next_lifecycle(Lifecycle.ACTIVE, LifecycleCommand.ARCHIVE) is Lifecycle.ARCHIVED
    assert next_lifecycle(Lifecycle.ARCHIVED, LifecycleCommand.RESTORE) is Lifecycle.ACTIVE
    assert (
        next_lifecycle(Lifecycle.ACTIVE, LifecycleCommand.REQUEST_DELETION)
        is Lifecycle.PENDING_DELETION
    )
    assert (
        next_lifecycle(Lifecycle.PENDING_DELETION, LifecycleCommand.CANCEL_DELETION)
        is Lifecycle.ACTIVE
    )
    assert next_lifecycle(Lifecycle.ARCHIVED, LifecycleCommand.REQUEST_DELETION) is None
    assert next_lifecycle(Lifecycle.ACTIVE, LifecycleCommand.RESTORE) is None
    for command in LifecycleCommand:
        assert next_lifecycle(Lifecycle.DELETED, command) is None


def test_allowed_commands_hide_internal_completion() -> None:
    assert allowed_commands(Lifecycle.PENDING_DELETION) == ["CancelDeletion"]
    assert allowed_commands(Lifecycle.DELETED) == []


def test_confirmation_is_exact_and_case_sensitive() -> None:
    assert confirmation_matches("  Atlas Expansion ", "Atlas Expansion")
    assert not confirmation_matches("atlas expansion", "Atlas Expansion")
    assert not confirmation_matches("Atlas", "Atlas Expansion")


# --------------------------------------------------------------------------- invitations


def test_credential_shape_and_digest() -> None:
    credential = new_credential()
    assert well_formed_credential(credential)
    assert len(credential) == 43
    assert credential_digest(credential) != credential.encode()
    assert len(credential_digest(credential)) == 32
    assert new_credential() != credential
    assert not well_formed_credential("short")


def test_expiry_boundary() -> None:
    issued = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    expires = issued + timedelta(days=7)
    almost = issued + timedelta(days=6, hours=23, minutes=59)
    assert effective_state(InvitationState.PENDING, expires, almost) is InvitationState.PENDING
    assert effective_state(InvitationState.PENDING, expires, expires) is InvitationState.EXPIRED
    assert effective_state(InvitationState.REVOKED, expires, expires) is InvitationState.REVOKED


def test_email_masking_and_normalization() -> None:
    assert mask_email("jordan.kim@client.example") == "j•••@client.example"
    assert normalize_invitee_email(" Jordan.Kim@Client.Example ") == "jordan.kim@client.example"
    assert normalize_invitee_email("not-an-email") is None
    assert normalize_invitee_email("a@localhost") is None


def test_attempt_limiter_blocks_the_eleventh_attempt_in_fifteen_minutes() -> None:
    clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
    limiter = AttemptLimiter(clock)
    for _ in range(10):
        limiter.hit("ip:a")
    with pytest.raises(RateLimited) as caught:
        limiter.hit("ip:a")
    assert caught.value.retry_after_seconds is not None
    limiter.hit("ip:b")  # other keys are unaffected
    clock.advance(timedelta(minutes=15))
    limiter.hit("ip:a")


# --------------------------------------------------------------------------- scopes


def _actor() -> Actor:
    return Actor(
        user_id="usr_01J9Z3Q8M8V3W7G2E1N4R5T6Y7",
        firm_id="frm_test",
        subject="sub",
        email="x@example.test",
        display_name="X",
        person_category=PersonCategory.TEAM_MEMBER,
        activation=Activation.ACTIVE,
        firm_manager=False,
        platform_operator=False,
    )


def test_project_scope_cannot_be_constructed_outside_project_access() -> None:
    with pytest.raises(TypeError):
        ProjectScope(
            firm_id="frm_test",
            project_id="prj_01J9Z3Q8M8V3W7G2E1N4R5T6Y7",
            role=R.ADMINISTRATOR,
            lifecycle=Lifecycle.ACTIVE,
            visibility=frozenset(Visibility),
            actor=_actor(),
            membership_id="mem_01J9Z3Q8M8V3W7G2E1N4R5T6Y7",
        )
    with pytest.raises(TypeError):
        ProjectScope(
            firm_id="frm_test",
            project_id="prj_01J9Z3Q8M8V3W7G2E1N4R5T6Y7",
            role=R.ADMINISTRATOR,
            lifecycle=Lifecycle.ACTIVE,
            visibility=frozenset(Visibility),
            actor=_actor(),
            membership_id="mem_01J9Z3Q8M8V3W7G2E1N4R5T6Y7",
            _token=object(),
        )
    with pytest.raises(TypeError):
        ScopeSet(scopes=(), scope_key="x")


def test_scope_key_ignores_order_and_duplicates() -> None:
    assert scope_key(["prj_b", "prj_a"]) == scope_key(["prj_a", "prj_b", "prj_a"])
    assert scope_key(["prj_a"]) != scope_key(["prj_a", "prj_b"])


def test_person_refs_are_stable_and_spaces_disjoint() -> None:
    key = b"k" * 32
    account = person_ref_id(key, "Account", "prj_a", "usr_1")
    assert account == person_ref_id(key, "Account", "prj_a", "usr_1")
    assert account.startswith("ppl_")
    assert len(account) == 30
    assert account != person_ref_id(key, "Evidence", "prj_a", "usr_1")
    assert account != person_ref_id(key, "Account", "prj_b", "usr_1")
