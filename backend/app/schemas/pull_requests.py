"""Pull request API schemas."""

from __future__ import annotations

from datetime import datetime

from app.schemas.common import ApiModel


class PullRequestOut(ApiModel):
    id: int
    run_id: int
    repository_id: int
    repository_full_name: str | None = None
    external_pr_number: int
    title: str
    branch_name: str
    base_branch: str
    head_commit_sha: str | None
    url: str
    status: str
    draft: bool
    validation_outcome: str | None
    sentry_issue_title: str | None = None
    sentry_issue_id: int | None = None
    is_demo: bool
    created_at: datetime
    updated_at: datetime
