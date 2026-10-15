"""Platform Operator routes (contract §13). T01/T02 shape: health, usage, quotas.

The job list, job retry/cancel, rebuilds, audit, and domains routes arrive with T32; they
need an operator identity that T03 supplies. Until then every route here is behind the
temporary bootstrap bearer token (see ``app/platform/http/operator_auth.py``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, Field

from app.api.v1.schemas.base import ApiModel, ApiRequest
from app.modules.governance import (
    WORKER_PROBE,
    AuditAction,
    Meter,
    Outcome,
    Priority,
    SafeFacts,
    TargetRef,
    WorkerProbePayload,
)
from app.platform.errors import FieldIssue, ValidationFailed
from app.platform.http.operator import OperatorContext, operator_context
from app.platform.ids import new_id

router = APIRouter(prefix="/operator", tags=["operator"])

Operator = Annotated[OperatorContext, Depends(operator_context)]


class ServiceHealth(ApiModel):
    name: str
    status: Literal["Ok", "Degraded", "Unavailable"]
    detail: str | None = None


class QueueHealth(ApiModel):
    kind: str
    depth: int
    oldest_age_seconds: int
    failed_24h: int = Field(alias="failed24h")  # to_camel would give failed24H


class ProjectionLag(ApiModel):
    project_id: str
    lag_seconds: float


class LastBackup(ApiModel):
    at: AwareDatetime
    verified_restore_at: AwareDatetime | None = None


class OperatorHealth(ApiModel):
    """Contract §13 ``/operator/health``. ``degraded`` is true while telemetry delivery is off."""

    services: list[ServiceHealth]
    queue: list[QueueHealth]
    projection_lag: list[ProjectionLag]
    last_backup: LastBackup | None
    degraded: bool


class MeterUsageView(ApiModel):
    meter: Meter
    used: int
    quota: int
    estimated_eur: float


class HaltedWorkView(ApiModel):
    kind: str
    count: int


class OperatorUsage(ApiModel):
    period: str
    meters: list[MeterUsageView]
    halted_work: list[HaltedWorkView]
    planning_ceilings_eur: dict[str, float]


class QuotaChange(ApiRequest):
    quota: int


class QuotaChanged(ApiModel):
    meter: Meter
    quota: int


@router.get("/health", response_model=OperatorHealth, operation_id="getOperatorHealth")
async def get_operator_health(ctx: Operator) -> OperatorHealth:
    report = await ctx.usage.usage()
    stats = await ctx.queue.stats()
    halted = sum(item.count for item in report.halted_work)
    telemetry_enabled = ctx.settings.telemetry.azure_monitor_connection_string is not None
    return OperatorHealth(
        services=[
            ServiceHealth(name="api", status="Ok"),
            ServiceHealth(
                name="worker",
                status="Ok" if halted == 0 else "Degraded",
                detail="Paid work halted by quota" if halted else None,
            ),
        ],
        queue=[
            QueueHealth(
                kind=stat.kind,
                depth=stat.depth,
                oldest_age_seconds=stat.oldest_age_seconds,
                failed_24h=stat.failed_24h,
            )
            for stat in stats
        ],
        # Empty until the retrieval projection exists (T16).
        projection_lag=[],
        # Backup/restore evidence arrives with NFR-07 tooling (T32).
        last_backup=None,
        degraded=not telemetry_enabled,
    )


@router.get("/usage", response_model=OperatorUsage, operation_id="getOperatorUsage")
async def get_operator_usage(ctx: Operator) -> OperatorUsage:
    report = await ctx.usage.usage()
    return OperatorUsage(
        period=report.period,
        meters=[
            MeterUsageView(
                meter=m.meter,
                used=m.used,
                quota=m.quota,
                estimated_eur=m.estimated_eur,
            )
            for m in report.meters
        ],
        halted_work=[HaltedWorkView(kind=h.kind, count=h.count) for h in report.halted_work],
        planning_ceilings_eur=report.planning_ceilings_eur,
    )


@router.put("/quotas/{meter}", response_model=QuotaChanged, operation_id="setOperatorQuota")
async def set_operator_quota(meter: Meter, body: QuotaChange, ctx: Operator) -> QuotaChanged:
    if body.quota < 0:
        raise ValidationFailed(
            [FieldIssue(field="quota", code="greater_than_equal", message="Quota must be >= 0.")]
        )
    async with ctx.uow() as tx:
        previous = await ctx.usage.policy(tx, meter)
        await ctx.usage.set_quota(tx, meter, body.quota)
        await ctx.audit.record(
            tx,
            actor=ctx.actor,
            action=AuditAction.QUOTA_CHANGED,
            target=TargetRef("quota_policy", meter.value),
            outcome=Outcome.SUCCEEDED,
            facts=SafeFacts(
                {"meter": meter.value, "fromQuota": previous.quota, "toQuota": body.quota}
            ),
        )
    return QuotaChanged(meter=meter, quota=body.quota)


class ProbeRequest(ApiRequest):
    steps: int = 3
    step_delay_ms: int = 500


class ProbeAccepted(ApiModel):
    job_id: str


@router.post(
    "/jobs/probe",
    response_model=ProbeAccepted,
    status_code=202,
    operation_id="probeWorkers",
)
async def probe_workers(body: ProbeRequest, ctx: Operator) -> ProbeAccepted:
    """Enqueue a harmless job that reports progress; watching it run proves the worker loop.

    ``relaxai-admin jobs probe`` calls this. Remove it once T32's job list exists.
    """
    payload = WorkerProbePayload(steps=body.steps, step_delay_ms=body.step_delay_ms)
    idempotency_key = f"probe:{new_id('job')}"
    async with ctx.uow() as tx:
        job_id = await ctx.queue.enqueue(
            tx, WORKER_PROBE, payload, idempotency_key=idempotency_key, priority=Priority.LOW
        )
    return ProbeAccepted(job_id=job_id)
