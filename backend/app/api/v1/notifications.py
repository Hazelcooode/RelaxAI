"""Notifications (contract §5.3)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.v1.deps import ActiveActor, AppContainer
from app.api.v1.schemas.base import ApiModel
from app.modules.actions import NotificationTarget, NotificationView
from app.modules.governance import UndoRef
from app.platform.http.pagination import PageParams, page_params

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationPage(ApiModel):
    """``Page<Notification> & { unreadCount }``."""

    items: list[NotificationView]
    next_cursor: str | None
    unread_count: int


class ReadAccepted(ApiModel):
    """Marking read is undoable for eight seconds (contract §1.6)."""

    undo: UndoRef


@router.get("", response_model=NotificationPage, operation_id="listNotifications")
async def list_notifications(
    container: AppContainer,
    actor: ActiveActor,
    params: Annotated[PageParams, Depends(page_params)],
    unread_only: Annotated[bool, Query(alias="unreadOnly")] = False,
) -> NotificationPage:
    page, unread = await container.notifications.list(
        actor, unread_only=unread_only, cursor=params.cursor, limit=params.limit
    )
    return NotificationPage(items=page.items, next_cursor=page.next_cursor, unread_count=unread)


@router.post("/read-all", response_model=ReadAccepted, operation_id="markAllNotificationsRead")
async def mark_all_read(container: AppContainer, actor: ActiveActor) -> ReadAccepted:
    return ReadAccepted(undo=await container.notifications.mark_read(actor, None))


@router.post(
    "/{notification_id}/read", response_model=ReadAccepted, operation_id="markNotificationRead"
)
async def mark_read(
    notification_id: str, container: AppContainer, actor: ActiveActor
) -> ReadAccepted:
    return ReadAccepted(undo=await container.notifications.mark_read(actor, notification_id))


@router.get(
    "/{notification_id}/target",
    response_model=NotificationTarget,
    response_model_exclude_none=True,
    operation_id="getNotificationTarget",
)
async def notification_target(
    notification_id: str, container: AppContainer, actor: ActiveActor
) -> NotificationTarget:
    return await container.notifications.target(actor, notification_id)
