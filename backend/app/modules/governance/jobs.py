"""Governance's own job handlers."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from pydantic import BaseModel, Field

from app.modules.governance.context import JobContext
from app.modules.governance.domain import Priority
from app.modules.governance.registry import job_handler

WORKER_PROBE = "governance.worker_probe"


class WorkerProbePayload(BaseModel):
    steps: int = Field(default=3, ge=1, le=20)
    step_delay_ms: int = Field(default=500, ge=0, le=5_000)


@job_handler(
    WORKER_PROBE,
    payload=WorkerProbePayload,
    max_attempts=1,
    concurrency=1,
    priority=Priority.LOW,
    timeout=timedelta(minutes=2),
)
async def worker_probe(ctx: JobContext, payload: WorkerProbePayload) -> None:
    """Operator smoke test (``relaxai-admin jobs probe``): proves a worker claims, reports
    progress, and completes. Touches no data, so it is trivially idempotent."""
    for step in range(1, payload.steps + 1):
        await asyncio.sleep(payload.step_delay_ms / 1000)
        await ctx.report("Probing", done=step, total=payload.steps, unit="Records")
