"""Notification templates and safe context (T04)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.modules.actions import TEMPLATES, NotificationTrigger, SafeSubjectRef
from app.modules.actions.domain import dedup_key

SAMPLE_CONTEXT: dict[str, dict[str, str | int]] = {
    "task.changed": {"changedField": "Priority"},
    "task.due_soon": {"dueDate": "2026-01-09"},
    "task.overdue": {"dueDate": "2026-01-04"},
    "review.reminder": {"pendingCount": 3},
    "archival.scheduled": {"executesOn": "2026-02-01"},
    "deletion.requested": {"executesOn": "2026-02-04"},
    "erasure.decision": {"decision": "Rejected"},
}
SUBJECT = SafeSubjectRef("Task", "tsk_01J9Z3Q8M8V3W7G2E1N4R5T6Y7")


def test_every_contract_trigger_has_a_template() -> None:
    assert {t.trigger for t in TEMPLATES.values()} == set(NotificationTrigger)


@pytest.mark.parametrize("key", sorted(TEMPLATES))
def test_every_template_renders_without_placeholders(key: str) -> None:
    template = TEMPLATES[key]
    context = template.validate(SAMPLE_CONTEXT.get(key, {}))
    title, body = template.render(context)
    assert title
    assert body
    assert "{" not in title + body


def test_templates_reaching_client_collaborators_are_marked() -> None:
    visible = {k for k, t in TEMPLATES.items() if t.client_visible}
    assert visible == {
        "task.assigned", "task.changed", "task.due_soon", "task.overdue", "erasure.decision"
    }  # fmt: skip


@pytest.mark.parametrize(
    ("key", "context"),
    [
        ("task.changed", {"changedField": "Title"}),  # not an allowed enum value
        ("review.reminder", {"pendingCount": -1}),
        ("review.reminder", {"pendingCount": True}),
        ("review.reminder", {"pendingCount": "3"}),
        ("task.due_soon", {"dueDate": "9 January"}),
        ("task.due_soon", {"dueDate": "2026-01-09", "note": "x"}),
    ],
)
def test_context_outside_the_allowlist_is_refused(key: str, context: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="notification context"):
        TEMPLATES[key].validate(context)  # type: ignore[arg-type]


def test_dedup_key_is_per_recipient_trigger_subject_and_day() -> None:
    due = TEMPLATES["task.due_soon"]
    morning = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    evening = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)
    assert dedup_key(due, "usr_a", SUBJECT, morning) == dedup_key(due, "usr_a", SUBJECT, evening)
    assert dedup_key(due, "usr_a", SUBJECT, morning) != dedup_key(due, "usr_b", SUBJECT, morning)
    assert dedup_key(TEMPLATES["task.assigned"], "usr_a", SUBJECT, morning) is None


def test_destinations_point_inside_the_project() -> None:
    destination = TEMPLATES["task.assigned"].destination("prj_1", SUBJECT)
    assert destination is not None
    assert destination.path == f"/projects/prj_1/tasks/{SUBJECT.id}"
    assert TEMPLATES["task.assigned"].destination(None, SUBJECT) is None
