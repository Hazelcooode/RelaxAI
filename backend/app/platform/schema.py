"""Base model for every camelCase wire shape (API schemas and module DTOs alike)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True
    )


class OmitWhenNone(CamelModel):
    """For contract fields written ``field?: T``: names in ``omit_when_none`` are left out
    of the output when ``None`` instead of being sent as ``null``. Other optional fields
    (``progress: T | null``) still serialize as ``null``."""

    omit_when_none: ClassVar[frozenset[str]] = frozenset()

    # Unannotated on purpose: a return annotation would replace the model's JSON schema.
    @model_serializer(mode="wrap")
    def _omit_absent(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        data = handler(self)
        if isinstance(data, dict):
            for name in self.omit_when_none:
                if getattr(self, name) is None:
                    data.pop(name, None)
                    alias = type(self).model_fields[name].alias
                    if alias is not None:
                        data.pop(alias, None)
        return data
