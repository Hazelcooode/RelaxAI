"""projects' job handlers: invitation expiry, scheduled archival, Project deletion."""

from __future__ import annotations

from datetime import timedelta

from app.modules.governance import JobContext, Priority, SweepPayload, job_handler, sweep
from app.modules.projects.invitations import Invitations
from app.modules.projects.lifecycle import (
    ARCHIVAL_EXECUTE,
    DELETION_MAX_ATTEMPTS,
    PROJECT_DELETE,
    ArchivalExecutePayload,
    ProjectDeletePayload,
    ProjectLifecycle,
)

INVITATION_EXPIRE = "projects.invitation_expire"


@sweep(INVITATION_EXPIRE, cron="7 * * * *")
async def expire_invitations(ctx: JobContext, payload: SweepPayload) -> None:
    """Hourly: persist lazy expiry so status views and the audit trail agree. Idempotent."""
    del payload
    await ctx.service(Invitations).expire_due()


@job_handler(
    ARCHIVAL_EXECUTE,
    payload=ArchivalExecutePayload,
    max_attempts=5,
    concurrency=2,
    priority=Priority.HIGH,
    timeout=timedelta(minutes=2),
)
async def execute_archival(ctx: JobContext, payload: ArchivalExecutePayload) -> None:
    """Completion check: a schedule that is no longer ``Scheduled`` is left alone."""
    await ctx.service(ProjectLifecycle).execute_archival(payload.schedule_id)


@job_handler(
    PROJECT_DELETE,
    payload=ProjectDeletePayload,
    max_attempts=DELETION_MAX_ATTEMPTS,
    concurrency=1,
    priority=Priority.CRITICAL,
    timeout=timedelta(minutes=30),
)
async def delete_project(ctx: JobContext, payload: ProjectDeletePayload) -> None:
    """Completion check: completed stores are skipped and an executed or cancelled request
    is a no-op, so any attempt may be re-run."""

    async def report(done: int, total: int) -> None:
        await ctx.report("PurgingStores", done, total, unit="Stores")

    await ctx.service(ProjectLifecycle).execute_deletion(
        payload.request_id, attempt=ctx.attempt, report=report
    )
