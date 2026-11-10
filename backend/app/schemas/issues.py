"""Sentry issue/event models (normalized) and issue API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel
from app.schemas.repositories import MappingOut


class StackFrame(ApiModel):
    filename: str | None = None
    abs_path: str | None = None
    module: str | None = None
    function: str | None = None
    lineno: int | None = None
    colno: int | None = None
    in_app: bool | None = None
    # Source context reported by the SDK: [(line_number, text), ...]. Local variables are
    # deliberately dropped (they frequently contain PII or secrets).
    context: list[tuple[int, str]] = Field(default_factory=list)


class ExceptionInfo(ApiModel):
    type: str | None = None
    value: str | None = None
    module: str | None = None
    mechanism: str | None = None
    frames: list[StackFrame] = Field(default_factory=list)


class NormalizedEvent(ApiModel):
    event_id: str
    title: str | None = None
    message: str | None = None
    platform: str | None = None
    release: str | None = None
    environment: str | None = None
    date_created: datetime | None = None
    exceptions: list[ExceptionInfo] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def primary_exception(self) -> ExceptionInfo | None:
        # Sentry orders chained exceptions oldest first; the last one is what was raised.
        return self.exceptions[-1] if self.exceptions else None


class NormalizedIssue(BaseModel):
    sentry_issue_id: str
    short_id: str | None = None
    organization: str
    project: str
    title: str
    culprit: str | None = None
    exception_type: str | None = None
    level: str | None = None
    status: str | None = None
    permalink: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    event_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReleaseCommit(BaseModel):
    sha: str
    repository: str | None = None
    message: str | None = None


# --- API schemas ----------------------------------------------------------------------------

class SentryIssueOut(ApiModel):
    id: int
    sentry_issue_id: str
    short_id: str | None
    organization: str
    project: str
    title: str
    culprit: str | None
    exception_type: str | None
    level: str | None
    status: str | None
    permalink: str | None
    first_seen: datetime | None
    last_seen: datetime | None
    event_count: int
    latest_event_id: str | None
    release: str | None
    repository_id: int | None
    repository_full_name: str | None = None
    latest_run_id: int | None = None
    latest_run_status: str | None = None
    is_demo: bool


class SentryIssueDetail(SentryIssueOut):
    event: NormalizedEvent | None = None
    event_error: str | None = None
    runs: list[dict[str, Any]] = Field(default_factory=list)
    github_issues: list[dict[str, Any]] = Field(default_factory=list)
    mapping: MappingOut | None = None


class RemoteIssueOut(BaseModel):
    """An issue listed live from Sentry (not necessarily ingested yet)."""

    sentry_issue_id: str
    short_id: str | None
    project: str
    title: str
    culprit: str | None
    exception_type: str | None
    level: str | None
    status: str | None
    permalink: str | None
    first_seen: datetime | None
    last_seen: datetime | None
    event_count: int
    ingested_id: int | None = None


class AnalyzeRequest(BaseModel):
    repository_id: int | None = None
    autonomy_mode: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


class LinkRepositoryRequest(BaseModel):
    repository_id: int | None
