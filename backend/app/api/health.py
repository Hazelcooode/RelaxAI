"""Health probes (contract §14). Unauthenticated; Nginx exposes only ``/healthz`` publicly."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.v1.schemas.base import ApiModel
from app.container import Container

router = APIRouter(tags=["health"])


class HealthStatus(ApiModel):
    status: Literal["ok"]


class ReadinessChecks(ApiModel):
    database: Literal["ok", "unavailable"]
    migrations: Literal["ok", "pending", "unknown"]


class ReadinessStatus(ApiModel):
    status: Literal["ready", "unavailable"]
    checks: ReadinessChecks


@router.get("/healthz", response_model=HealthStatus, operation_id="getHealthz")
async def healthz() -> HealthStatus:
    """The process is alive. Never touches dependencies."""
    return HealthStatus(status="ok")


@router.get(
    "/readyz",
    response_model=ReadinessStatus,
    operation_id="getReadyz",
    responses={503: {"model": ReadinessStatus, "description": "Not ready"}},
)
async def readyz(request: Request) -> JSONResponse:
    """The database is reachable and the Alembic head is applied."""
    container: Container = request.app.state.container
    report = await container.readiness.check()
    body = ReadinessStatus(
        status="ready" if report.ready else "unavailable",
        checks=ReadinessChecks.model_validate(
            {"database": report.database.value, "migrations": report.migrations.value}
        ),
    )
    return JSONResponse(
        status_code=200 if report.ready else 503,
        content=body.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )
