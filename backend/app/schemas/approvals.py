"""Approval API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel


class ApprovalOut(ApiModel):
    id: int
    run_id: int
    patch_id: int
    approval_type: str
    status: str
    reviewer_id: int | None
    reviewer_name: str | None = None
    reason: str | None
    risk_summary: dict[str, Any]
    is_demo: bool
    created_at: datetime
    resolved_at: datetime | None
    run_status: str | None = None
    issue_title: str | None = None
    repository_full_name: str | None = None
    files_changed: list[Any] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    confirm: bool = Field(description="Must be true: explicit confirmation of a consequential action")
