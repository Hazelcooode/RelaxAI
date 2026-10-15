"""Pure notification rules: triggers, templates, safe context, rendering (NOTE-01..07).

A notification is a ``template_key`` plus a ``safe_context`` of allowlisted keys whose
values match fixed patterns, so personal data, document text, and chat text cannot be
passed. Titles and bodies are rendered from the template on read and for email.

Nothing here performs I/O.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

from app.modules.governance import Destination, personal_data_screen


class NotificationTrigger(StrEnum):
    TASK_ASSIGNED = "TaskAssigned"
    TASK_CHANGED = "TaskChanged"
    TASK_DUE_SOON = "TaskDueSoon"
    TASK_OVERDUE = "TaskOverdue"
    MENTION = "Mention"
    REVIEW_REMINDER = "ReviewReminder"
    ARCHIVAL_SCHEDULED = "ArchivalScheduled"
    ARCHIVAL_CANCELLED = "ArchivalCancelled"
    DELETION_REQUESTED = "DeletionRequested"
    DELETION_CANCELLED = "DeletionCancelled"
    ERASURE_DECISION = "ErasureDecision"
    INVITATION_ACCEPTED = "InvitationAccepted"
    CONNECTOR_PROBLEM = "ConnectorProblem"


class Channel(StrEnum):
    IN_PRODUCT = "InProduct"
    EMAIL = "Email"


ALL_CHANNELS: Final = frozenset({Channel.IN_PRODUCT, Channel.EMAIL})


class DeliveryState(StrEnum):
    DELIVERED = "Delivered"  # in-product: visible as soon as the transaction commits
    PENDING = "Pending"
    SENT = "Sent"
    SKIPPED = "Skipped"  # recipient no longer reachable (inactive account)
    FAILED = "Failed"


@dataclass(frozen=True, slots=True)
class SafeSubjectRef:
    """What a notification is about: a record kind and its opaque id. Never a label."""

    kind: str
    id: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z][A-Za-z]{1,39}", self.kind):
            raise ValueError("subject kind must be a PascalCase record type")
        personal_data_screen.require_safe_token(self.id, what="notification subject id")


# --------------------------------------------------------------------------- safe context

_LOCAL_DATE = r"\d{4}-\d{2}-\d{2}"


@dataclass(frozen=True, slots=True)
class ContextRule:
    """One allowed context key: a string matching ``pattern`` or an int in ``int_range``."""

    pattern: str | None = None
    int_range: tuple[int, int] | None = None
    required: bool = False

    def check(self, key: str, value: object) -> None:
        if self.int_range is not None:
            low, high = self.int_range
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"notification context {key!r}: integer {low}..{high} expected")
            return
        if not isinstance(value, str) or self.pattern is None:
            raise ValueError(f"notification context {key!r}: string expected")
        if not re.fullmatch(self.pattern, value):
            raise ValueError(f"notification context {key!r}: value not allowed")
        if personal_data_screen.contains_personal_data(value):
            raise ValueError(f"notification context {key!r}: value looks like personal data")


DATE = ContextRule(pattern=_LOCAL_DATE, required=True)
COUNT = ContextRule(int_range=(0, 100_000), required=True)


def _enum(*values: str) -> ContextRule:
    return ContextRule(pattern="|".join(re.escape(v) for v in values), required=True)


# --------------------------------------------------------------------------- templates

type DestinationFor = Callable[[str | None, SafeSubjectRef], Destination | None]


def _project_path(suffix: str) -> DestinationFor:
    def build(project_id: str | None, subject: SafeSubjectRef) -> Destination | None:
        if project_id is None:
            return None
        return Destination(
            kind=subject.kind,
            project_id=project_id,
            path=f"/projects/{project_id}{suffix.format(id=subject.id)}",
        )

    return build


def _fixed(kind: str, path: str) -> DestinationFor:
    def build(project_id: str | None, subject: SafeSubjectRef) -> Destination | None:
        del project_id, subject
        return Destination(kind=kind, path=path)

    return build


@dataclass(frozen=True, slots=True)
class NotificationTemplate:
    key: str
    trigger: NotificationTrigger
    title: str
    body: str
    destination: DestinationFor
    context: Mapping[str, ContextRule] = field(default_factory=dict)
    client_visible: bool = False  # only Client Visible templates may reach a Client Collaborator
    dedup_daily: bool = False  # repeatable triggers update one notification per day

    def validate(self, safe_context: Mapping[str, str | int]) -> dict[str, str | int]:
        unknown = set(safe_context) - set(self.context)
        if unknown:
            raise ValueError(f"notification context keys {sorted(unknown)} not allowed")
        for key, rule in self.context.items():
            if key not in safe_context:
                if rule.required:
                    raise ValueError(f"notification context {key!r} is required")
                continue
            rule.check(key, safe_context[key])
        return dict(safe_context)

    def render(self, safe_context: Mapping[str, str | int]) -> tuple[str, str]:
        values = {k: str(v) for k, v in safe_context.items()}
        return self.title.format_map(values), self.body.format_map(values)


_T = NotificationTrigger
_TASK = _project_path("/tasks/{id}")

TEMPLATES: Final[Mapping[str, NotificationTemplate]] = {
    t.key: t
    for t in (
        NotificationTemplate(
            "task.assigned", _T.TASK_ASSIGNED, "A task was assigned to you",
            "Open the task to see what is needed.", _TASK, client_visible=True,
        ),
        NotificationTemplate(
            "task.changed", _T.TASK_CHANGED, "A task you follow changed",
            "The task's {changedField} changed.", _TASK,
            {"changedField": _enum("Status", "Priority", "DueDate", "Assignee", "Progress")},
            client_visible=True,
        ),
        NotificationTemplate(
            "task.due_soon", _T.TASK_DUE_SOON, "A task is due soon", "It is due on {dueDate}.",
            _TASK, {"dueDate": DATE}, client_visible=True, dedup_daily=True,
        ),
        NotificationTemplate(
            "task.overdue", _T.TASK_OVERDUE, "A task is overdue", "It was due on {dueDate}.",
            _TASK, {"dueDate": DATE}, client_visible=True, dedup_daily=True,
        ),
        NotificationTemplate(
            "mention", _T.MENTION, "You were mentioned", "Open the item to see the mention.",
            _project_path("/knowledge/items/{id}"),
        ),
        NotificationTemplate(
            "review.reminder", _T.REVIEW_REMINDER, "A pseudonymization review is waiting",
            "{pendingCount} item(s) need review.", _project_path("/privacy/reviews/{id}"),
            {"pendingCount": COUNT},
        ),
        NotificationTemplate(
            "archival.scheduled", _T.ARCHIVAL_SCHEDULED, "A Project is scheduled for archival",
            "It will be archived on {executesOn}.", _project_path("/settings"),
            {"executesOn": DATE},
        ),
        NotificationTemplate(
            "archival.cancelled", _T.ARCHIVAL_CANCELLED, "A scheduled archival was cancelled",
            "The Project stays active.", _project_path("/settings"),
        ),
        NotificationTemplate(
            "deletion.requested", _T.DELETION_REQUESTED, "A Project is pending deletion",
            "It will be deleted on {executesOn} unless the deletion is cancelled.",
            _fixed("ProjectRecovery", "/projects/recovery"), {"executesOn": DATE},
        ),
        NotificationTemplate(
            "deletion.cancelled", _T.DELETION_CANCELLED, "A Project deletion was cancelled",
            "The Project was restored.", _project_path(""),
        ),
        NotificationTemplate(
            "erasure.decision", _T.ERASURE_DECISION, "Your erasure request was decided",
            "Decision: {decision}.", _fixed("ErasureRequest", "/account/erasure"),
            {"decision": _enum("Approved", "Rejected")}, client_visible=True,
        ),
        NotificationTemplate(
            "invitation.accepted", _T.INVITATION_ACCEPTED, "An invitation was accepted",
            "A new member joined the Project.", _project_path("/members"),
        ),
        NotificationTemplate(
            "connector.problem", _T.CONNECTOR_PROBLEM, "Microsoft 365 needs attention",
            "Reconnect to keep sources importing.", _fixed("Connector", "/account/connector"),
        ),
    )
}  # fmt: skip


def dedup_key(
    template: NotificationTemplate, recipient_user_id: str, subject: SafeSubjectRef, now: datetime
) -> str | None:
    """``(recipient, trigger, subject, day)`` for repeatable triggers; ``None`` otherwise."""
    if not template.dedup_daily:
        return None
    return f"{recipient_user_id}:{template.trigger.value}:{subject.id}:{now.date().isoformat()}"
