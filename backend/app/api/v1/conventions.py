"""Request-level conventions for ``/api/v1`` routes: **[idem]** (contract §1.2, SYS-20).

A route marked **[idem]** takes ``idem: Idempotent`` and returns
``await idem.run(lambda: service.command(...), status_code=...)``. The key row, the command's
own units of work, and the stored response commit in one transaction (``uow.ambient()``):

* same key, same request -> the stored status and body, with ``Idempotency-Replayed: true``;
* same key, different request -> ``422 IDEMPOTENCY_KEY_REUSED``;
* no key -> ``422 VALIDATION_FAILED`` naming the header;
* a failed command stores nothing, so retrying it is a fresh attempt.

The request hash covers method, route template, path parameters, and the canonical JSON
body; the body itself is never stored (typed confirmations included).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

import orjson
from fastapi import Depends, Header, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, Response

from app.api.v1.deps import ActiveActor, AppContainer
from app.container import Container
from app.platform.correlation import route_template
from app.platform.errors import FieldIssue, ValidationFailed

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotency-Replayed"


def request_hash(method: str, route: str, path_params: Mapping[str, Any], body: bytes) -> str:
    if body.strip():
        try:
            canonical = orjson.dumps(orjson.loads(body), option=orjson.OPT_SORT_KEYS)
        except orjson.JSONDecodeError:
            canonical = body
    else:
        canonical = b""
    material = orjson.dumps(
        {"m": method.upper(), "r": route, "p": {k: str(v) for k, v in path_params.items()}},
        option=orjson.OPT_SORT_KEYS,
    )
    return hashlib.sha256(material + b"\n" + canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class IdempotentCommand:
    container: Container
    actor_id: str
    route: str
    key: str
    request_hash: str

    async def run(
        self, command: Callable[[], Awaitable[BaseModel | None]], *, status_code: int = 200
    ) -> Response:
        keys = self.container.idempotency
        where = {"actor_id": self.actor_id, "route": self.route, "key": self.key}
        async with self.container.uow.ambient() as tx:
            stored = await keys.begin(tx, request_hash=self.request_hash, **where)
            if stored is not None:
                return _response(stored.status, stored.body, replayed=True)
            result = await command()
            body = result.model_dump(mode="json", by_alias=True) if result is not None else None
            await keys.complete(
                tx, status=status_code, body=body, operation_id=_operation_id(body), **where
            )
        return _response(status_code, body, replayed=False)


def _operation_id(body: Any) -> str | None:
    if isinstance(body, dict):
        operation = body.get("operation")
        if isinstance(operation, dict) and isinstance(operation.get("operationId"), str):
            return str(operation["operationId"])
    return None


def _response(status: int, body: Any, *, replayed: bool) -> Response:
    headers = {REPLAYED_HEADER: "true"} if replayed else None
    if body is None:
        return Response(status_code=status, headers=headers)
    return JSONResponse(content=body, status_code=status, headers=headers)


async def idempotent_command(
    request: Request,
    container: AppContainer,
    actor: ActiveActor,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias=IDEMPOTENCY_HEADER,
            description="UUID v4, generated once per user intent and reused on retry.",
        ),
    ] = None,
) -> IdempotentCommand:
    if idempotency_key is None or not idempotency_key.strip():
        raise ValidationFailed(
            [
                FieldIssue(
                    field=IDEMPOTENCY_HEADER,
                    code="missing",
                    message="The Idempotency-Key header is required.",
                )
            ]
        )
    try:
        key = str(uuid.UUID(idempotency_key.strip()))
    except ValueError:
        raise ValidationFailed(
            [FieldIssue(field=IDEMPOTENCY_HEADER, code="uuid", message="Expected a UUID.")]
        ) from None
    assert actor.user_id is not None  # noqa: S101 - ActiveActor guarantees it
    route = route_template(request.scope)
    return IdempotentCommand(
        container=container,
        actor_id=actor.user_id,
        route=route,
        key=key,
        request_hash=request_hash(request.method, route, request.path_params, await request.body()),
    )


Idempotent = Annotated[IdempotentCommand, Depends(idempotent_command)]
