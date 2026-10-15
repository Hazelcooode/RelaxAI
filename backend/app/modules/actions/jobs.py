"""actions' job handlers."""

from __future__ import annotations

from datetime import timedelta

from app.modules.actions.notifications import (
    EMAIL_DELIVER,
    EmailDeliveryPayload,
    Notifications,
)
from app.modules.governance import JobContext, Priority, RetryableError, job_handler
from app.platform.errors import UpstreamError


@job_handler(
    EMAIL_DELIVER,
    payload=EmailDeliveryPayload,
    max_attempts=6,
    concurrency=4,
    priority=Priority.HIGH,
    timeout=timedelta(seconds=30),
)
async def deliver_email(ctx: JobContext, payload: EmailDeliveryPayload) -> None:
    """Completion check: ``deliver_email`` skips a delivery already sent or skipped."""
    try:
        await ctx.service(Notifications).deliver_email(payload.notification_id)
    except UpstreamError:
        raise RetryableError("EmailUnavailable") from None
