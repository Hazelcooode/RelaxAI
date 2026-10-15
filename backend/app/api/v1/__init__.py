"""``/api/v1`` route adapters: parse -> call one service method -> map the result."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import activity, me, notifications, operator
from app.platform.http.errors import ErrorEnvelope

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    "4XX": {"model": ErrorEnvelope, "description": "Error envelope (contract §2)"},
    "5XX": {"model": ErrorEnvelope, "description": "Error envelope (contract §2)"},
}

router = APIRouter(responses=ERROR_RESPONSES)

router.include_router(me.router)
router.include_router(activity.router)
router.include_router(notifications.router)
router.include_router(operator.router)

# Later tasks include their routers here, e.g. router.include_router(me.router).
