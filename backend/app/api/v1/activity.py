"""Activity / User Operations (contract §5.4) and ``POST /undo`` (contract §1.6)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field

from app.api.v1.conventions import Idempotent
from app.api.v1.deps import ActiveActor, AppContainer
from app.api.v1.schemas.base import ApiModel, ApiRequest
from app.modules.governance import ActivityGroup, OperationRef, OperationSummary, OperationView
from app.platform.http.pagination import Page, PageParams, page_params

router = APIRouter(tags=["activity"])


@router.get("/operations", response_model=Page[OperationView], operation_id="listOperations")
async def list_operations(
    container: AppContainer,
    actor: ActiveActor,
    group: Annotated[ActivityGroup, Query()],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[OperationView]:
    return await container.operations.list_for(actor, group, params.cursor, limit=params.limit)


@router.get(
    "/operations/summary", response_model=OperationSummary, operation_id="getOperationSummary"
)
async def operation_summary(container: AppContainer, actor: ActiveActor) -> OperationSummary:
    return await container.operations.summary(actor)


@router.get("/operations/{operation_id}", response_model=OperationView, operation_id="getOperation")
async def get_operation(
    operation_id: str, container: AppContainer, actor: ActiveActor
) -> OperationView:
    return await container.operations.get_for(actor, operation_id)


@router.post("/operations/{operation_id}/dismiss", status_code=204, operation_id="dismissOperation")
async def dismiss_operation(
    operation_id: str, container: AppContainer, actor: ActiveActor
) -> Response:
    await container.operations.dismiss(actor, operation_id)
    return Response(status_code=204)


class OperationAccepted(ApiModel):
    operation: OperationRef


@router.post(
    "/operations/{operation_id}/retry",
    response_model=OperationAccepted,
    status_code=202,
    operation_id="retryOperation",
)
async def retry_operation(
    operation_id: str, container: AppContainer, actor: ActiveActor, idem: Idempotent
) -> Response:
    """**[idem]** Only when ``availableActions`` contains ``Retry``."""

    async def command() -> OperationAccepted:
        ref = await container.operations.retry(actor, operation_id, idem.key)
        return OperationAccepted(operation=ref)

    return await idem.run(command, status_code=202)


class UndoRequest(ApiRequest):
    token: str = Field(min_length=1, max_length=64)


class UndoResult(ApiModel):
    result: dict[str, Any] = Field(
        description="The current resource, of the same type the undone command returned."
    )


@router.post("/undo", response_model=UndoResult, operation_id="undo")
async def undo(body: UndoRequest, container: AppContainer, actor: ActiveActor) -> UndoResult:
    """Idempotent; ``409 UNDO_EXPIRED`` with ``current`` once the window has closed."""
    result = await container.undo.undo(actor, body.token)
    return UndoResult(result=result.model_dump(mode="json", by_alias=True))
