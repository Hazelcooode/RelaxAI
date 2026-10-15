"""Minimal five-field cron expressions (minute hour day-of-month month day-of-week), UTC.

Supports ``*``, numbers, lists (``1,15``), ranges (``1-5``) and steps (``*/15``, ``0-30/10``).
Day-of-week uses 0-6 with 0 = Sunday (7 is also Sunday). When both day fields are
restricted, a time matches if either matches (standard cron semantics).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7))


def _parse_field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError(f"invalid cron step: {part!r}")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = int(base)
            end = high if step_text else start
        if not (low <= start <= end <= high):
            raise ValueError(f"cron value out of range: {part!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class CronSchedule:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError(f"cron expression needs 5 fields: {expression!r}")
        parsed = [_parse_field(p, lo, hi) for p, (_, lo, hi) in zip(parts, _FIELDS, strict=True)]
        weekdays = frozenset(0 if d == 7 else d for d in parsed[4])
        return cls(
            expression, parsed[0], parsed[1], parsed[2], parsed[3], weekdays,
            day_restricted=parts[2] != "*", weekday_restricted=parts[4] != "*",
        )  # fmt: skip

    def matches(self, at: datetime) -> bool:
        if at.minute not in self.minutes or at.hour not in self.hours:
            return False
        if at.month not in self.months:
            return False
        day_ok = at.day in self.days
        weekday_ok = (at.isoweekday() % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def fire_times(self, after: datetime, until: datetime) -> list[datetime]:
        """Every matching minute in ``(after, until]`` (both timezone-aware)."""
        start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        times = []
        at = start
        while at <= until:
            if self.matches(at):
                times.append(at)
            at += timedelta(minutes=1)
        return times
