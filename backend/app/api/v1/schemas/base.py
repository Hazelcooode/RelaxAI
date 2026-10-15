"""Base models for every API schema. These models are the OpenAPI source of truth."""

from __future__ import annotations

from pydantic import ConfigDict

from app.platform.schema import CamelModel


class ApiModel(CamelModel):
    """camelCase on the wire, snake_case in Python."""


class ApiRequest(ApiModel):
    """Request bodies reject unknown fields so typos fail loudly (422)."""

    model_config = ConfigDict(extra="forbid")
