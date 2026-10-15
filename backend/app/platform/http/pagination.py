"""Cursor pagination (contract §1.3): ``Page<T> = { items, nextCursor }``.

Cursors are opaque to clients: ``base64url(json{k, id}) "." base64url(hmac)``. The HMAC key
comes from settings, so a client cannot forge or edit a cursor to jump into rows it has not
been served; any tampering is ``400 MALFORMED_REQUEST``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated, Generic, TypeVar

import orjson
from fastapi import Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.platform.errors import MalformedRequest

type SortKey = str | int | float | bool | list[str | int | float | bool | None] | None

T = TypeVar("T")

MAX_CURSOR_LENGTH = 1024
_MAC_BYTES = 16


class Page(BaseModel, Generic[T]):  # noqa: UP046 - pydantic generic naming
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True
    )

    items: list[T]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class Cursor:
    k: SortKey
    id: str


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class CursorCodec:
    def __init__(self, key: bytes) -> None:
        if len(key) < 16:
            raise ValueError("cursor signing key must be at least 16 bytes")
        self._key = key

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()[:_MAC_BYTES]

    def encode(self, k: SortKey, id: str) -> str:
        payload = orjson.dumps({"k": k, "id": id}, option=orjson.OPT_SORT_KEYS)
        return f"{_b64encode(payload)}.{_b64encode(self._mac(payload))}"

    def decode(self, cursor: str) -> Cursor:
        try:
            if len(cursor) > MAX_CURSOR_LENGTH:
                raise ValueError("too long")
            body, _, mac = cursor.partition(".")
            payload = _b64decode(body)
            if not hmac.compare_digest(_b64decode(mac), self._mac(payload)):
                raise ValueError("bad signature")
            data = orjson.loads(payload)
            if not isinstance(data, dict) or set(data) != {"k", "id"}:
                raise ValueError("bad shape")
            if not isinstance(data["id"], str):
                raise ValueError("bad id")
        except (ValueError, binascii.Error, orjson.JSONDecodeError):
            raise MalformedRequest() from None
        return Cursor(k=data["k"], id=data["id"])


@dataclass(frozen=True, slots=True)
class PageParams:
    cursor: str | None
    limit: int


def page_params(
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> PageParams:
    """FastAPI dependency for ``?cursor=&limit=`` (default 25, max 100)."""
    return PageParams(cursor=cursor, limit=limit)
