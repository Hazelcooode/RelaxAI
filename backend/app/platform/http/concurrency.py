"""Optimistic concurrency over HTTP (contract §1.2, SYS-06).

Routes marked **[ver]** take ``version: IfMatch``; a missing header is ``428
PRECONDITION_REQUIRED``. The service compares it with the stored version
(``optimistic_update``), which raises ``409 VERSION_CONFLICT`` with ``current`` — never
``412``. Successful reads and mutations send the new version as ``ETag: "<version>"``.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import Depends, Header, Response

from app.platform.errors import FieldIssue, PreconditionRequired, ValidationFailed

_ENTITY_TAG = re.compile(r'^(?:W/)?"(?P<version>\d{1,18})"$')


def if_match_version(
    if_match: Annotated[
        str | None,
        Header(alias="If-Match", description='Current version, e.g. "3" (from the ETag).'),
    ] = None,
) -> int:
    if if_match is None or not if_match.strip():
        raise PreconditionRequired()
    match = _ENTITY_TAG.match(if_match.strip())
    if match is None:
        raise ValidationFailed(
            [FieldIssue(field="If-Match", code="entity_tag", message='Expected "<version>".')]
        )
    return int(match.group("version"))


IfMatch = Annotated[int, Depends(if_match_version)]


def etag(version: int) -> str:
    return f'"{version}"'


def set_etag(response: Response, version: int) -> None:
    response.headers["ETag"] = etag(version)
