"""The email port and its in-memory adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.platform.errors import UpstreamCategory, UpstreamError


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """Plain-text email. ``to`` is personal data: adapters never log it."""

    to: str
    subject: str
    text: str


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None:
        """Hand the message to the provider; raise ``UpstreamError(EMAIL)`` on failure."""
        ...


class OutboxEmailSender:
    """Keeps messages in memory (tests, local runs). ``fail_next`` simulates outages."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_next = 0

    async def send(self, message: EmailMessage) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise UpstreamError(UpstreamCategory.EMAIL)
        self.sent.append(message)
