from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.platform.clock import Clock, FrozenClock, SystemClock
from app.platform.ids import PREFIXES, is_opaque_id, new_id


@given(st.sampled_from(sorted(PREFIXES)))
def test_new_id_has_prefix_and_ulid(prefix: str) -> None:
    value = new_id(prefix)
    head, _, ulid = value.rpartition("_")
    assert head == prefix
    assert len(ulid) == 26
    assert is_opaque_id(value)


def test_new_id_is_unique_and_sortable_within_prefix() -> None:
    ids = [new_id("prj") for _ in range(200)]
    assert len(set(ids)) == 200


@pytest.mark.parametrize("prefix", ["", "xyz", "PRJ", "prj_", "project"])
def test_new_id_rejects_unknown_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="unknown identifier prefix"):
        new_id(prefix)


@pytest.mark.parametrize(
    "value", ["", "prj", "prj_123", "xyz_01J9Z3Q8M8V3W7G2E1N4R5T6Y7", 42, None, "a@b.example"]
)
def test_is_opaque_id_rejects_non_ids(value: object) -> None:
    assert not is_opaque_id(value)


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert isinstance(SystemClock(), Clock)


def test_frozen_clock_advance_and_set() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FrozenClock(start)
    assert clock.now() == start
    assert clock.now() == start  # does not move by itself
    assert clock.advance(timedelta(days=7)) == start + timedelta(days=7)
    later = datetime(2026, 3, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    assert clock.set(later) == later
    assert clock.now().tzinfo == UTC


def test_frozen_clock_rejects_naive_and_backwards() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 1, 1))  # noqa: DTZ001
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
