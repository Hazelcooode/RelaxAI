"""Actions: notifications (T04); internal tasks and Planner arrive with T25/T26.

This is the module's public interface; other modules import only from here.
"""

from app.modules.actions import jobs as _jobs  # registers the module's job handlers
from app.modules.actions.domain import (
    ALL_CHANNELS,
    TEMPLATES,
    Channel,
    DeliveryState,
    NotificationTemplate,
    NotificationTrigger,
    SafeSubjectRef,
)
from app.modules.actions.notifications import (
    EMAIL_DELIVER,
    DestinationAccess,
    EmailDeliveryPayload,
    NotificationReadState,
    Notifications,
    NotificationTarget,
    NotificationView,
    Recipient,
    RecipientDirectory,
)

del _jobs

__all__ = [
    "ALL_CHANNELS",
    "EMAIL_DELIVER",
    "TEMPLATES",
    "Channel",
    "DeliveryState",
    "DestinationAccess",
    "EmailDeliveryPayload",
    "NotificationReadState",
    "NotificationTarget",
    "NotificationTemplate",
    "NotificationTrigger",
    "NotificationView",
    "Notifications",
    "Recipient",
    "RecipientDirectory",
    "SafeSubjectRef",
]
