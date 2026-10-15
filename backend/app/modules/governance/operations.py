"""``Operations``: the User Operation read model behind inline status and the Activity
drawer (SYS-12, SYS-21..24, conventions §4.3, contract §5.4).

One operation per user command; any number of jobs attach to it through ``operation_id``.
The owning module starts it inside its command transaction. After that, state follows the
attached jobs (claim, report, retry, wait, success, failure — see ``project_job_event``) or
explicit ``report``/``wait_for_user``/``finish`` calls from the owning module.

Every read re-evaluates the audience (initiator, or a current holder of the recorded
capability) and the destination through ``OperationAccess``; T05 supplies the
``ProjectAccess`` adapter. Content-free types never expose a Project or record reference.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    exists,
    func,
    literal,
    not_,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert

from app.modules.governance.domain import (
    TERMINAL_STATES,
    ActorRef,
    Destination,
    JobState,
    personal_data_screen,
)
from app.modules.governance.tables import jobs, operation_dismissals, operations
from app.platform.clock import Clock
from app.platform.db import Tx, UnitOfWork
from app.platform.errors import AppError, ErrorCode, NotAvailable
from app.platform.http.pagination import CursorCodec, Page
from app.platform.ids import is_opaque_id, new_id
from app.platform.schema import CamelModel, OmitWhenNone

if TYPE_CHECKING:  # runtime imports would cycle: queue and store project into operations
    from app.modules.governance.queue import JobQueue
    from app.modules.governance.registry import JobRegistry

# --------------------------------------------------------------------------- vocabulary


class OperationType(StrEnum):
    DOCUMENT_INGESTION = "DocumentIngestion"
    HISTORICAL_IMPORT = "HistoricalImport"
    VISIBILITY_PROPAGATION = "VisibilityPropagation"
    RECOMMENDATION_GENERATION = "RecommendationGeneration"
    PLANNER_CREATE = "PlannerCreate"
    PLANNER_SYNC = "PlannerSync"
    PLANNER_CONVERSION = "PlannerConversion"
    PROJECT_DELETION = "ProjectDeletion"
    ERASURE_EXECUTION = "ErasureExecution"
    INDEX_REBUILD = "IndexRebuild"
    CONNECTOR_REVOCATION = "ConnectorRevocation"


CONTENT_FREE_TYPES: Final = frozenset(
    {
        OperationType.PROJECT_DELETION,
        OperationType.ERASURE_EXECUTION,
        OperationType.VISIBILITY_PROPAGATION,
    }
)

# Generic labels used when the subject label may not be shown.
TYPE_LABELS: Final[Mapping[OperationType, str]] = {
    OperationType.DOCUMENT_INGESTION: "Document ingestion",
    OperationType.HISTORICAL_IMPORT: "Historical import",
    OperationType.VISIBILITY_PROPAGATION: "Visibility change",
    OperationType.RECOMMENDATION_GENERATION: "Recommendation generation",
    OperationType.PLANNER_CREATE: "Planner task creation",
    OperationType.PLANNER_SYNC: "Planner synchronization",
    OperationType.PLANNER_CONVERSION: "Planner conversion",
    OperationType.PROJECT_DELETION: "Project deletion",
    OperationType.ERASURE_EXECUTION: "Erasure execution",
    OperationType.INDEX_REBUILD: "Index rebuild",
    OperationType.CONNECTOR_REVOCATION: "Connector revocation",
}


class OperationState(StrEnum):
    QUEUED = "Queued"
    RUNNING = "Running"
    RETRYING = "Retrying"
    WAITING_FOR_USER = "WaitingForUser"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


# Domain job state -> presentation state (T04 table). The authoritative ``domain_state`` is
# kept for operations screens and never sent to ordinary users.
PRESENTATION_STATE: Final[Mapping[JobState, OperationState]] = {
    JobState.QUEUED: OperationState.QUEUED,
    JobState.RUNNING: OperationState.RUNNING,
    JobState.RETRYING: OperationState.RETRYING,
    JobState.WAITING_FOR_USER: OperationState.WAITING_FOR_USER,
    JobState.SUCCEEDED: OperationState.SUCCEEDED,
    JobState.FAILED: OperationState.FAILED,
    JobState.CANCELLED: OperationState.CANCELLED,
}

ACTIVE_STATES: Final = frozenset(
    {
        OperationState.QUEUED,
        OperationState.RUNNING,
        OperationState.RETRYING,
        OperationState.WAITING_FOR_USER,
    }
)
_RUNNING_GROUP: Final = (OperationState.QUEUED, OperationState.RUNNING, OperationState.RETRYING)
_DONE_STATES: Final = frozenset(
    {OperationState.SUCCEEDED, OperationState.FAILED, OperationState.CANCELLED}
)
RECENT_WINDOW: Final = timedelta(hours=24)


class ProgressUnit(StrEnum):
    PAGES = "Pages"
    RECORDS = "Records"
    STORES = "Stores"
    MESSAGES = "Messages"


class ActivityGroup(StrEnum):
    RUNNING = "Running"
    NEEDS_ATTENTION = "NeedsAttention"
    RECENTLY_COMPLETED = "RecentlyCompleted"


class AvailableAction(StrEnum):
    RETRY = "Retry"
    REAUTHORIZE = "Reauthorize"
    REVIEW = "Review"
    OPEN = "Open"
    DISMISS = "Dismiss"


class AudienceKind(StrEnum):
    INITIATOR = "Initiator"  # only the person who started it
    CAPABILITY = "Capability"  # initiator + current holders of a Project capability


@dataclass(frozen=True, slots=True)
class Audience:
    kind: AudienceKind = AudienceKind.INITIATOR
    action_code: str | None = None
    project_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind is AudienceKind.CAPABILITY and not self.action_code:
            raise ValueError("a capability audience names the capability")

    @classmethod
    def initiator(cls) -> Audience:
        return cls()

    @classmethod
    def holders_of(cls, capability: str, project_id: str) -> Audience:
        return cls(AudienceKind.CAPABILITY, capability, project_id)


@dataclass(frozen=True, slots=True)
class SafeSubject:
    """A safe label (a file name, "Erasure case E-12") and an optional opaque record id."""

    label: str
    record_id: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.label) <= 200 or "\n" in self.label:
            raise ValueError("operation subject label must be one line of 1-200 characters")
        if personal_data_screen.contains_personal_data(self.label):
            raise ValueError("operation subject label looks like personal data")
        if self.record_id is not None and not is_opaque_id(self.record_id):
            raise ValueError("operation subject record id must be an opaque identifier")


# Failure codes a user may retry (transient causes). Anything else is terminal.
RETRYABLE_FAILURE_CODES: Final = frozenset(
    {"Timeout", "UnexpectedError", "LeaseExpired", "UpstreamUnavailable", "QuotaExceeded"}
)
FAILURE_MESSAGES: Final[Mapping[str, str]] = {
    "Timeout": "The work took too long and stopped.",
    "UpstreamUnavailable": "A connected service was unavailable.",
    "QuotaExceeded": "The usage quota was reached.",
    "UnsupportedFormat": "This file format is not supported.",
}
DEFAULT_FAILURE_MESSAGE: Final = "The operation could not be completed."
WAITING_LABELS: Final[Mapping[str, str]] = {
    "Review": "Review required",
    "Reauthorize": "Reconnect Microsoft 365",
}
DEFAULT_WAITING_LABEL: Final = "Action required"


# --------------------------------------------------------------------------- views


class ProjectLabel(CamelModel):
    project_id: str
    name: str


class OperationRef(CamelModel):
    operation_id: str
    state: OperationState
    status_url: str


class SubjectView(OmitWhenNone):
    omit_when_none = frozenset({"record_id"})

    label: str
    record_id: str | None = None


class ProgressView(CamelModel):
    done: int
    total: int
    unit: ProgressUnit


class FailureView(CamelModel):
    code: str
    message: str
    work_retained: bool


class WaitingView(CamelModel):
    action: str
    label: str


class OperationView(OmitWhenNone):
    """Contract §5.4 ``Operation``. ``project``, ``completedAt``, ``nextRetryAt``,
    ``failure``, and ``waitingFor`` are omitted (not null) when they do not apply."""

    omit_when_none = frozenset(
        {"project", "completed_at", "next_retry_at", "failure", "waiting_for"}
    )

    operation_id: str
    type: OperationType
    subject: SubjectView
    project: ProjectLabel | None = None
    state: OperationState
    stage: str
    progress: ProgressView | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    next_retry_at: datetime | None = None
    failure: FailureView | None = None
    waiting_for: WaitingView | None = None
    available_actions: list[AvailableAction]
    destination: Destination | None


class OperationSummary(CamelModel):
    badge_count: int
    running: int
    needs_attention: int


def status_url(operation_id: str) -> str:
    return f"/api/v1/operations/{operation_id}"


# --------------------------------------------------------------------------- access port


class OperationActor(Protocol):
    @property
    def user_id(self) -> str | None: ...


class OperationAccess(Protocol):
    """Reauthorization seam. T05's ``ProjectAccess`` adapter replaces the default."""

    async def holds(self, actor: Any, capability: str, project_id: str | None) -> bool: ...

    async def destination_authorized(self, actor: Any, destination: Destination) -> bool: ...

    async def project_label(self, actor: Any, project_id: str) -> ProjectLabel | None: ...


class NoProjectAccess:
    """Until T05: nobody holds a Project capability, Project destinations fail closed, and
    no Project label is shown. Destinations outside any Project are authorized."""

    async def holds(self, actor: Any, capability: str, project_id: str | None) -> bool:
        return False

    async def destination_authorized(self, actor: Any, destination: Destination) -> bool:
        return destination.project_id is None

    async def project_label(self, actor: Any, project_id: str) -> ProjectLabel | None:
        return None


# --------------------------------------------------------------------------- job projection


JobEvent = Literal["claimed", "report", "retry", "waiting", "succeeded", "failed", "requeued"]


async def project_job_event(
    tx: Tx,
    operation_id: str,
    event: JobEvent,
    *,
    now: datetime,
    stage: str | None = None,
    done: int | None = None,
    total: int | None = None,
    unit: str | None = None,
    error_code: str | None = None,
    retryable: bool = False,
    run_at: datetime | None = None,
    waiting_action: str | None = None,
    destination: Mapping[str, Any] | None = None,
) -> None:
    """Fold one attached job's transition into its operation, in the job's transaction.

    Explicit ``finish`` by the owning module wins: a terminal operation is never reopened by
    a job, and a job success completes the operation only when no other attached job is
    still active (chains such as extract -> privacy -> index finish on their last job).
    """
    o = operations.c
    active = (
        OperationState.QUEUED.value,
        OperationState.RUNNING.value,
        OperationState.RETRYING.value,
    )
    values: dict[str, Any] = {"updated_at": now}
    condition: ColumnElement[bool] = o.state.in_(active)
    if event == "claimed":
        values |= {"state": OperationState.RUNNING.value, "domain_state": JobState.RUNNING.value,
                   "next_retry_at": None}  # fmt: skip
    elif event == "report":
        values |= {"state": OperationState.RUNNING.value, "domain_state": JobState.RUNNING.value,
                   "stage": stage or "Running", "progress_done": done, "progress_total": total,
                   "progress_unit": unit}  # fmt: skip
    elif event == "retry":
        values |= {"state": OperationState.RETRYING.value,
                   "domain_state": JobState.RETRYING.value, "next_retry_at": run_at}  # fmt: skip
    elif event == "requeued":
        condition = o.state.in_((*active, OperationState.WAITING_FOR_USER.value))
        values |= {"state": OperationState.QUEUED.value, "domain_state": JobState.QUEUED.value,
                   "waiting_action_code": None}  # fmt: skip
    elif event == "waiting":
        values |= {"state": OperationState.WAITING_FOR_USER.value,
                   "domain_state": JobState.WAITING_FOR_USER.value,
                   "waiting_action_code": waiting_action}  # fmt: skip
        if destination is not None:
            values |= {"destination_kind": destination["kind"],
                       "destination_path": destination["path"],
                       "destination_project_id": destination.get("project_id")}  # fmt: skip
    elif event == "succeeded":
        others = select(jobs.c.id).where(
            jobs.c.operation_id == operation_id,
            jobs.c.state.not_in([s.value for s in TERMINAL_STATES]),
        )
        condition = and_(condition, not_(exists(others)))
        values |= {"state": OperationState.SUCCEEDED.value,
                   "domain_state": JobState.SUCCEEDED.value, "completed_at": now,
                   "next_retry_at": None}  # fmt: skip
    elif event == "failed":
        values |= _failure_values(error_code or "UnexpectedError", retryable=retryable, now=now)
    await tx.session.execute(
        update(operations).where(o.id == operation_id, condition).values(values)
    )


def _failure_values(code: str, *, retryable: bool, now: datetime) -> dict[str, Any]:
    return {
        "state": OperationState.FAILED.value,
        "domain_state": JobState.FAILED.value,
        "failure_code": code,
        "failure_message_safe": FAILURE_MESSAGES.get(code, DEFAULT_FAILURE_MESSAGE),
        "failure_retryable": retryable,
        "completed_at": now,
        "next_retry_at": None,
    }


# --------------------------------------------------------------------------- service

_PAGE_SIZE = 25
_SCAN_BATCH = 100
_CURSOR_SHAPES: Final[Mapping[ActivityGroup, tuple[str, ...]]] = {
    ActivityGroup.RUNNING: ("ts", "id"),
    ActivityGroup.NEEDS_ATTENTION: ("int", "ts", "id"),
    ActivityGroup.RECENTLY_COMPLETED: ("ts", "id"),
}


class Operations:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        firm_id: str,
        queue: JobQueue,
        registry: JobRegistry,
        cursors: CursorCodec,
        access: OperationAccess | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._firm_id = firm_id
        self._queue = queue
        self._registry = registry
        self._cursors = cursors
        self.access: OperationAccess = access or NoProjectAccess()

    # ------------------------------------------------------------------ owning modules

    async def start(
        self,
        tx: Tx,
        *,
        type: OperationType,
        subject: SafeSubject,
        initiated_by: ActorRef | None,
        project_id: str | None,
        destination: Destination,
        audience: Audience,
    ) -> OperationRef:
        now = self._clock.now()
        operation_id = new_id("op")
        await tx.session.execute(
            operations.insert().values(
                id=operation_id,
                firm_id=self._firm_id,
                project_id=project_id,
                type=type.value,
                subject_label=subject.label,
                subject_record_id=subject.record_id,
                initiated_by_user_id=initiated_by.user_id if initiated_by else None,
                audience_kind=audience.kind.value,
                audience_action_code=audience.action_code,
                audience_project_id=audience.project_id,
                destination_kind=destination.kind,
                destination_path=destination.path,
                destination_project_id=destination.project_id,
                state=OperationState.QUEUED.value,
                domain_state=JobState.QUEUED.value,
                stage="Queued",
                created_at=now,
                updated_at=now,
            )
        )
        return OperationRef(
            operation_id=operation_id,
            state=OperationState.QUEUED,
            status_url=status_url(operation_id),
        )

    async def report(
        self,
        tx: Tx,
        operation_id: str,
        *,
        stage: str,
        done: int | None = None,
        total: int | None = None,
        unit: ProgressUnit | None = None,
    ) -> None:
        """Progress is trustworthy only with both counts (and a unit); otherwise give a stage
        alone. A finished operation ignores late reports."""
        if (done is None) != (total is None) or (done is not None and unit is None):
            raise ValueError("report done, total, and unit together, or none of them")
        if done is not None and total is not None and not 0 <= done <= total:
            raise ValueError("progress must satisfy 0 <= done <= total")
        await project_job_event(
            tx, operation_id, "report", now=self._clock.now(), stage=stage, done=done,
            total=total, unit=unit.value if unit else None,
        )  # fmt: skip

    async def wait_for_user(
        self, tx: Tx, operation_id: str, *, action_code: str, destination: Destination
    ) -> None:
        await project_job_event(
            tx, operation_id, "waiting", now=self._clock.now(), waiting_action=action_code,
            destination=destination.model_dump(),
        )  # fmt: skip

    async def finish(
        self,
        tx: Tx,
        operation_id: str,
        *,
        outcome: Literal["Succeeded", "Failed", "Cancelled"],
        failure_code: str | None = None,
        work_retained: bool = False,
        retryable: bool | None = None,
    ) -> None:
        """Record the outcome. ``retryable`` defaults from the failure code."""
        now = self._clock.now()
        o = operations.c
        if outcome == "Failed":
            code = failure_code or "UnexpectedError"
            values = _failure_values(
                code,
                retryable=code in RETRYABLE_FAILURE_CODES if retryable is None else retryable,
                now=now,
            )
            values["work_retained"] = work_retained
        else:
            values = {"state": outcome, "domain_state": outcome, "completed_at": now,
                      "next_retry_at": None, "waiting_action_code": None}  # fmt: skip
        values["updated_at"] = now
        await tx.session.execute(
            update(operations)
            .where(o.id == operation_id, o.state.not_in([s.value for s in _DONE_STATES]))
            .values(values)
        )

    # ------------------------------------------------------------------ readers

    async def get_for(self, actor: OperationActor, operation_id: str) -> OperationView:
        async with self._uow() as tx:
            row = await self._row(tx, operation_id)
        if row is None or not await self._visible(actor, row):
            raise NotAvailable()
        return await self._view(actor, row)

    async def list_for(
        self,
        actor: OperationActor,
        group: ActivityGroup,
        cursor: str | None,
        *,
        limit: int = _PAGE_SIZE,
    ) -> Page[OperationView]:
        """One Activity group, newest-relevant first, authorized row by row."""
        user_id = actor.user_id
        if user_id is None:
            return Page[OperationView](items=[])
        now = self._clock.now()
        o = operations.c
        state_filter, sort = self._group(group, now)
        position = self._decode(cursor, group)
        visible: list[Any] = []
        exhausted = False
        while len(visible) <= limit and not exhausted:
            conditions = [o.firm_id == self._firm_id, state_filter, self._candidate(user_id)]
            if position is not None:
                conditions.append(tuple_(*sort) < tuple_(*(literal(v) for v in position)))
            async with self._uow() as tx:
                rows = (
                    (
                        await tx.session.execute(
                            select(operations, *(s.label(f"_k{i}") for i, s in enumerate(sort)))
                            .where(*conditions)
                            .order_by(*(s.desc() for s in sort))
                            .limit(_SCAN_BATCH)
                        )
                    )
                    .mappings()
                    .all()
                )
            exhausted = len(rows) < _SCAN_BATCH
            for row in rows:
                position = tuple(row[f"_k{i}"] for i in range(len(sort)))
                if await self._visible(actor, row):
                    visible.append((row, position))
                    if len(visible) > limit:
                        break
        page = visible[:limit]
        next_cursor = None
        if len(visible) > limit:
            last = page[-1][1]
            next_cursor = self._cursors.encode(
                [v.isoformat() if isinstance(v, datetime) else v for v in last],
                f"op:{group.value}",
            )
        return Page[OperationView](
            items=[await self._view(actor, row) for row, _ in page], next_cursor=next_cursor
        )

    async def summary(self, actor: OperationActor) -> OperationSummary:
        """Badge = Queued + Running + Retrying + WaitingForUser + unresolved Failed visible to
        the actor; ordinary successes and dismissed rows are excluded."""
        user_id = actor.user_id
        if user_id is None:
            return OperationSummary(badge_count=0, running=0, needs_attention=0)
        o = operations.c
        tracked = [*(s.value for s in ACTIVE_STATES), OperationState.FAILED.value]
        async with self._uow() as tx:
            rows = (
                (
                    await tx.session.execute(
                        select(operations).where(
                            o.firm_id == self._firm_id,
                            o.state.in_(tracked),
                            self._candidate(user_id),
                        )
                    )
                )
                .mappings()
                .all()
            )
        running = needs_attention = badge = 0
        for row in rows:
            if not await self._visible(actor, row):
                continue
            state = OperationState(row["state"])
            unresolved = _unresolved_failure(row)
            if state in _RUNNING_GROUP:
                running += 1
            else:
                needs_attention += 1
            if row["badge_visible"] and (state in ACTIVE_STATES or unresolved):
                badge += 1
        return OperationSummary(badge_count=badge, running=running, needs_attention=needs_attention)

    # ------------------------------------------------------------------ commands

    async def dismiss(self, actor: OperationActor, operation_id: str) -> None:
        """Per-user: writes ``operation_dismissals`` only — never the job, the record, a
        notification, or an audit event."""
        user_id = actor.user_id
        async with self._uow() as tx:
            row = await self._row(tx, operation_id)
            if row is None or user_id is None or not await self._visible(actor, row):
                raise NotAvailable()
            if not _dismissible(row):
                raise AppError(ErrorCode.OPERATION_NOT_DISMISSIBLE)
            await tx.session.execute(
                insert(operation_dismissals)
                .values(operation_id=operation_id, user_id=user_id, dismissed_at=self._clock.now())
                .on_conflict_do_nothing()
            )

    async def retry(self, actor: OperationActor, operation_id: str, idem_key: str) -> OperationRef:
        """Re-enqueue the failed job under the same operation. Allowed only while
        ``availableActions`` contains ``Retry``; the caller's key makes it idempotent."""
        async with self._uow() as tx:
            row = await self._row(tx, operation_id, for_update=True)
            if row is None or not await self._visible(actor, row):
                raise NotAvailable()
            if AvailableAction.RETRY not in await self._actions(actor, row, destination=None):
                raise AppError(ErrorCode.INVALID_TRANSITION, allowed_transitions=[])
            failed = (
                (
                    await tx.session.execute(
                        select(jobs)
                        .where(
                            jobs.c.operation_id == operation_id,
                            jobs.c.state == JobState.FAILED.value,
                        )
                        .order_by(jobs.c.updated_at.desc(), jobs.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if failed is None:
                raise AppError(ErrorCode.INVALID_TRANSITION, allowed_transitions=[])
            spec = self._registry[failed["kind"]]
            await self._queue.enqueue(
                tx,
                failed["kind"],
                spec.payload.model_validate(failed["payload"]),
                idempotency_key=f"op-retry:{operation_id}:{idem_key}"[:200],
                operation_id=operation_id,
            )
            now = self._clock.now()
            await tx.session.execute(
                update(operations)
                .where(operations.c.id == operation_id)
                .values(
                    state=OperationState.QUEUED.value,
                    domain_state=JobState.QUEUED.value,
                    stage="Queued",
                    failure_code=None,
                    failure_message_safe=None,
                    failure_retryable=False,
                    work_retained=False,
                    completed_at=None,
                    updated_at=now,
                )
            )
        return OperationRef(
            operation_id=operation_id,
            state=OperationState.QUEUED,
            status_url=status_url(operation_id),
        )

    # ------------------------------------------------------------------ internals

    async def _row(self, tx: Tx, operation_id: str, *, for_update: bool = False) -> Any:
        stmt = select(operations).where(
            operations.c.id == operation_id, operations.c.firm_id == self._firm_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await tx.session.execute(stmt)).mappings().one_or_none()

    def _candidate(self, user_id: str) -> ColumnElement[bool]:
        """SQL prefilter: the actor's own operations or capability-audience ones (the latter
        are then checked one by one), excluding the actor's dismissals."""
        o = operations.c
        dismissed = exists().where(
            operation_dismissals.c.operation_id == o.id, operation_dismissals.c.user_id == user_id
        )
        return and_(
            or_(
                o.initiated_by_user_id == user_id, o.audience_kind == AudienceKind.CAPABILITY.value
            ),
            not_(dismissed),
        )

    def _group(
        self, group: ActivityGroup, now: datetime
    ) -> tuple[ColumnElement[bool], Sequence[ColumnElement[Any]]]:
        o = operations.c
        if group is ActivityGroup.RUNNING:
            return o.state.in_([s.value for s in _RUNNING_GROUP]), (o.created_at, o.id)
        if group is ActivityGroup.NEEDS_ATTENTION:
            # Urgency: retryable failures, then waiting for a person, then terminal failures;
            # most recent first within each. Negated so every sort key runs descending.
            urgency = case(
                (and_(o.state == OperationState.FAILED.value, o.failure_retryable), 0),
                (o.state == OperationState.WAITING_FOR_USER.value, -1),
                else_=-2,
            )
            states = [OperationState.WAITING_FOR_USER.value, OperationState.FAILED.value]
            return o.state.in_(states), (urgency, o.updated_at, o.id)
        completed = and_(
            o.state.in_([OperationState.SUCCEEDED.value, OperationState.CANCELLED.value]),
            o.completed_at >= now - RECENT_WINDOW,
        )
        return completed, (func.coalesce(o.completed_at, o.updated_at), o.id)

    def _decode(self, cursor: str | None, group: ActivityGroup) -> tuple[Any, ...] | None:
        """Cursor ``k`` holds the group's sort key; its shape is fixed per group."""
        if cursor is None:
            return None
        decoded = self._cursors.decode(cursor)
        shape = _CURSOR_SHAPES[group]
        if decoded.id != f"op:{group.value}" or not isinstance(decoded.k, list):
            raise AppError(ErrorCode.MALFORMED_REQUEST)
        if len(decoded.k) != len(shape):
            raise AppError(ErrorCode.MALFORMED_REQUEST)
        values: list[Any] = []
        try:
            for kind, value in zip(shape, decoded.k, strict=True):
                if kind == "ts" and isinstance(value, str):
                    values.append(datetime.fromisoformat(value))
                elif (kind == "int" and isinstance(value, int) and not isinstance(value, bool)) or (
                    kind == "id" and isinstance(value, str)
                ):
                    values.append(value)
                else:
                    raise ValueError(kind)
        except ValueError:
            raise AppError(ErrorCode.MALFORMED_REQUEST) from None
        return tuple(values)

    async def _visible(self, actor: OperationActor, row: Any) -> bool:
        if actor.user_id is not None and row["initiated_by_user_id"] == actor.user_id:
            return True
        if row["audience_kind"] == AudienceKind.CAPABILITY.value:
            return await self.access.holds(
                actor, row["audience_action_code"], row["audience_project_id"]
            )
        return False

    async def _authorized_destination(self, actor: OperationActor, row: Any) -> Destination | None:
        destination = Destination(
            kind=row["destination_kind"],
            project_id=row["destination_project_id"],
            path=row["destination_path"],
        )
        return destination if await self.access.destination_authorized(actor, destination) else None

    async def _actions(
        self, actor: OperationActor, row: Any, *, destination: Destination | None
    ) -> list[AvailableAction]:
        state = OperationState(row["state"])
        actions: list[AvailableAction] = []
        if (
            state is OperationState.FAILED
            and row["failure_retryable"]
            and await self._may_retry(actor, row)
        ):
            actions.append(AvailableAction.RETRY)
        if state is OperationState.WAITING_FOR_USER and destination is not None:
            actions.append(
                AvailableAction.REAUTHORIZE
                if row["waiting_action_code"] == "Reauthorize"
                else AvailableAction.REVIEW
            )
        if state is OperationState.SUCCEEDED and destination is not None:
            actions.append(AvailableAction.OPEN)
        if _dismissible(row):
            actions.append(AvailableAction.DISMISS)
        return actions

    async def _may_retry(self, actor: OperationActor, row: Any) -> bool:
        """Retry re-runs the owning command: the actor must *currently* hold its capability
        (the initiator too, once a capability is recorded), and there must be a failed job
        to re-run — an operation failed directly by its module has none."""
        code = row["audience_action_code"]
        if code is not None:
            allowed = await self.access.holds(actor, code, row["audience_project_id"])
        else:
            allowed = row["initiated_by_user_id"] == actor.user_id
        if not allowed:
            return False
        async with self._uow() as tx:
            failed_job = select(jobs.c.id).where(
                jobs.c.operation_id == row["id"], jobs.c.state == JobState.FAILED.value
            )
            return bool((await tx.session.execute(select(exists(failed_job)))).scalar())

    async def _view(self, actor: OperationActor, row: Any) -> OperationView:
        op_type = OperationType(row["type"])
        state = OperationState(row["state"])
        destination = await self._authorized_destination(actor, row)
        content_free = op_type in CONTENT_FREE_TYPES
        project = None
        subject = SubjectView(label=TYPE_LABELS[op_type])
        if destination is not None:
            subject = SubjectView(
                label=row["subject_label"],
                record_id=None if content_free else row["subject_record_id"],
            )
            if row["project_id"] is not None and not content_free:
                project = await self.access.project_label(actor, row["project_id"])
        progress = None
        if row["progress_done"] is not None and row["progress_total"] is not None:
            progress = ProgressView(
                done=row["progress_done"],
                total=row["progress_total"],
                unit=ProgressUnit(row["progress_unit"] or ProgressUnit.RECORDS),
            )
        failure = None
        if state is OperationState.FAILED:
            failure = FailureView(
                code=row["failure_code"] or "UnexpectedError",
                message=row["failure_message_safe"] or DEFAULT_FAILURE_MESSAGE,
                work_retained=row["work_retained"],
            )
        waiting = None
        if state is OperationState.WAITING_FOR_USER and row["waiting_action_code"]:
            code = row["waiting_action_code"]
            waiting = WaitingView(
                action=code, label=WAITING_LABELS.get(code, DEFAULT_WAITING_LABEL)
            )
        return OperationView(
            operation_id=row["id"],
            type=op_type,
            subject=subject,
            project=project,
            state=state,
            stage=row["stage"],
            progress=progress,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"] if state in _DONE_STATES else None,
            next_retry_at=row["next_retry_at"] if state is OperationState.RETRYING else None,
            failure=failure,
            waiting_for=waiting,
            available_actions=await self._actions(actor, row, destination=destination),
            destination=destination,
        )


def _unresolved_failure(row: Any) -> bool:
    """A retryable failure stays unresolved (Retry, not Dismiss) until retried."""
    return bool(row["state"] == OperationState.FAILED.value and row["failure_retryable"])


def _dismissible(row: Any) -> bool:
    state = OperationState(row["state"])
    if state in ACTIVE_STATES:
        return False
    return not _unresolved_failure(row)
