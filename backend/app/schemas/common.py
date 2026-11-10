"""Common API schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """Base for response schemas: defaulted fields are still always present in responses, so the
    generated TypeScript types mark them as required."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Page[T](ApiModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Message(BaseModel):
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
