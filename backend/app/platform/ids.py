"""Opaque, prefixed identifiers: ``"{prefix}_{ULID}"`` (conventions §3.1, contract §1.4)."""

from __future__ import annotations

import re

from ulid import ULID

PREFIXES: frozenset[str] = frozenset(
    {
        # architecture-and-conventions.md §3.1
        "frm", "usr", "prj", "mem", "inv", "src", "ing", "doc", "prj_doc", "ovw", "itm",
        "rev", "cit", "chs", "msg", "ans", "rec", "tsk", "tkh", "pln", "pdr", "plk", "ntf",
        "aud", "job", "op", "und", "rvw", "cnd", "era", "rvl", "fmn",
        # api-contract.md §1.4 additions
        "sug", "idc", "ppl",
        # internal (T02): usage quota reservations
        "rsv",
    }
)  # fmt: skip

_ID_PATTERN = re.compile(r"^(?P<prefix>[a-z]+(?:_[a-z]+)?)_(?P<ulid>[0-9A-HJKMNP-TV-Z]{26})$")


def new_id(prefix: str) -> str:
    """Return a new identifier for ``prefix``; unknown prefixes are a programming error."""
    if prefix not in PREFIXES:
        raise ValueError(f"unknown identifier prefix: {prefix!r}")
    return f"{prefix}_{ULID()}"


def is_opaque_id(value: object) -> bool:
    """True when ``value`` is an identifier minted by :func:`new_id` (used by the log filter)."""
    if not isinstance(value, str):
        return False
    match = _ID_PATTERN.match(value)
    return match is not None and match.group("prefix") in PREFIXES
