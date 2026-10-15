"""Base models for every API schema. These models are the OpenAPI source of truth."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True
    )


class ApiRequest(ApiModel):
    """Request bodies reject unknown fields so typos fail loudly (422)."""

    model_config = ConfigDict(extra="forbid")
