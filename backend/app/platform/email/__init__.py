"""``EmailSender`` port (conventions §1 decision 5): Azure Communication Services Email in
production (EU data location), an in-memory outbox for tests and local development."""

from app.platform.email.acs import AcsEmailSender
from app.platform.email.ports import EmailMessage, EmailSender, OutboxEmailSender

__all__ = ["AcsEmailSender", "EmailMessage", "EmailSender", "OutboxEmailSender"]
