"""The one source of "now". Every deadline reads a ``Clock`` (conventions §3.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test clock that only moves when told to."""

    def __init__(self, now: datetime) -> None:
        self._now = _require_aware(now)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("FrozenClock cannot move backwards; use set()")
        self._now += delta
        return self._now

    def set(self, now: datetime) -> datetime:
        self._now = _require_aware(now)
        return self._now


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Clock datetimes must be timezone-aware")
    return value.astimezone(UTC)
