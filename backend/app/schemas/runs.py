"""Analysis run API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel


class RunCreate(BaseModel):
    sentry_issue_id: int | None = Field(default=None, description="Internal id of an ingested Sentry issue")
    demo_scenario: str | None = Field(default=None, max_length=60)
    autonomy_mode: str | None = None
    repository_id: int | None = None
    use_real_llm: bool = False
    idempotency_key: str | None = Field(default=None, max_length=200)


class RunOut(ApiModel):
    id: int
    sentry_issue_id: int
    sentry_issue_title: str | None = None
    sentry_short_id: str | None = None
    repository_id: int | None
    repository_full_name: str | None = None
    status: str
    current_step: str | None
    outcome: str | None
    autonomy_mode: str
    trigger: str
    root_cause_summary: str | None
    diagnosis_status: str | None
    confidence_score: float | None
    model_name: str | None
    llm_simulated: bool
    source_revision: str | None
    source_revision_kind: str | None
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    retry_count: int
    repair_attempt: int
    total_duration_ms: int | None
    prompt_tokens: int
    completion_tokens: int
    estimated_cost: float | None
    cancel_requested: bool
    retry_of_run_id: int | None
    is_demo: bool
    created_at: datetime
    updated_at: datetime


class StepOut(ApiModel):
    id: int
    sequence_number: int
    step_type: str
    state: str
    status: str
    input_summary: str | None
    output_summary: str | None
    duration_ms: int | None
    retry_count: int
    error_message: str | None
    metadata: dict[str, Any]
    started_at: datetime
    completed_at: datetime | None


class TestExecutionOut(ApiModel):
    __test__ = False
    id: int
    patch_id: int | None
    phase: str
    backend: str
    status: str
    command_summary: str
    exit_code: int | None
    tests_passed: int | None
    tests_failed: int | None
    stdout_summary: str | None
    stderr_summary: str | None
    duration_ms: int | None
    timed_out: bool
    truncated_output: bool
    simulated: bool
    started_at: datetime
    completed_at: datetime | None


class PatchOut(ApiModel):
    id: int
    run_id: int
    attempt_number: int
    base_commit_sha: str | None
    branch_name: str | None
    patch_content: str
    diff_summary: str | None
    plan: dict[str, Any]
    files_changed: list[Any]
    lines_added: int
    lines_removed: int
    validation_status: str
    policy_status: str
    policy_findings: list[Any]
    risk_level: str | None
    superseded: bool
    created_at: datetime


class EventOut(ApiModel):
    id: int
    event_type: str
    message: str
    data: dict[str, Any]
    created_at: datetime


class RunDetail(RunOut):
    steps: list[StepOut] = Field(default_factory=list)
    patches: list[PatchOut] = Field(default_factory=list)
    tests: list[TestExecutionOut] = Field(default_factory=list)
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    pull_request: dict[str, Any] | None = None
    github_issues: list[dict[str, Any]] = Field(default_factory=list)
    diagnosis: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    frames: list[dict[str, Any]] = Field(default_factory=list)
    investigation_notes: list[str] = Field(default_factory=list)
    validation: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)
    revision_note: str | None = None
    final_report: dict[str, Any] | None = None
    abstain_reasons: list[str] = Field(default_factory=list)
    demo_scenario: str | None = None


class PolicyReportOut(ApiModel):
    blocked: bool
    findings: list[dict[str, Any]]
    files: list[str]
    lines_added: int
    lines_removed: int
    risk_level: str
