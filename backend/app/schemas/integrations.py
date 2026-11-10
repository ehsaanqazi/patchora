"""Integration and settings API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel


class FieldStatus(ApiModel):
    name: str
    label: str = ""
    help: str = ""
    kind: str = "text"
    required: bool = False
    env_var: str
    secret: bool
    configured: bool
    source: str | None = None  # saved | env | default
    value: str | None = None  # only for non-secret fields


class IntegrationOut(ApiModel):
    provider: str
    configured: bool
    mode: str | None = None
    fields: list[FieldStatus]
    last_tested_at: datetime | None = None
    last_success_at: datetime | None = None
    last_ok: bool | None = None
    last_message: str | None = None
    notes: list[str] = Field(default_factory=list)


class ConnectionTestResult(ApiModel):
    provider: str
    ok: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CredentialUpdate(BaseModel):
    values: dict[str, str | None] = Field(description="Field name -> new value (null/empty clears)")


class SettingsOut(ApiModel):
    values: dict[str, Any]
    defaults: dict[str, Any]
    security_sensitive: list[str]
    environment: dict[str, Any]


class SettingsUpdate(BaseModel):
    values: dict[str, Any]
