from __future__ import annotations

import base64

import orjson
import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.platform.errors import MalformedRequest
from app.platform.http.pagination import CursorCodec, Page

codec = CursorCodec(b"unit-test-cursor-key-0123456789")

sort_keys = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.text(max_size=40),
    st.lists(
        st.one_of(st.integers(min_value=0, max_value=10**9), st.text(max_size=10)), max_size=3
    ),
)


@given(k=sort_keys, ident=st.text(min_size=1, max_size=40))
def test_round_trip(k: object, ident: str) -> None:
    cursor = codec.encode(k, ident)  # type: ignore[arg-type]
    decoded = codec.decode(cursor)
    assert decoded.k == k
    assert decoded.id == ident


def _forge(payload: dict[str, object], mac: str) -> str:
    body = base64.urlsafe_b64encode(orjson.dumps(payload)).rstrip(b"=").decode()
    return f"{body}.{mac}"


def test_tampered_payload_is_rejected() -> None:
    cursor = codec.encode("2026-01-01T00:00:00Z", "tsk_01J9Z3Q8M8V3W7G2E1N4R5T6Y7")
    _, _, mac = cursor.partition(".")
    forged = _forge({"k": "2020-01-01T00:00:00Z", "id": "tsk_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"}, mac)
    with pytest.raises(MalformedRequest):
        codec.decode(forged)


def test_cursor_from_another_key_is_rejected() -> None:
    other = CursorCodec(b"another-cursor-key-0123456789ab").encode(1, "x")
    with pytest.raises(MalformedRequest):
        codec.decode(other)


@pytest.mark.parametrize("garbage", ["", ".", "abc", "abc.def", "%%%.***", "a" * 2000])
def test_garbage_is_rejected(garbage: str) -> None:
    with pytest.raises(MalformedRequest):
        codec.decode(garbage)


def test_page_serializes_camel_case() -> None:
    page = Page[int](items=[1, 2], next_cursor="c")
    assert page.model_dump() == {"items": [1, 2], "nextCursor": "c"}


def test_short_key_rejected() -> None:
    with pytest.raises(ValueError, match="at least 16 bytes"):
        CursorCodec(b"short")
