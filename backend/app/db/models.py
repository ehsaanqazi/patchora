"""SQLAlchemy ORM models.

Design notes:

* Every row that originates from demo mode carries ``is_demo=True`` so demo data is never
  silently mixed with real data and can be removed without touching anything else.
* Large or free-form content (patches, test output, model summaries) is bounded before it
  is written; see :mod:`app.services.audit_service` and the services that write each table.
* ``audit_logs`` is append-only through the application interface; an SQLite trigger also
  blocks UPDATEs at the database level (see the initial migration).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, utcnow


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    api_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("provider", "provider_repository_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # github | demo
    provider_repository_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    full_name: Mapped[str] = mapped_column(String(201), nullable=False, index=True)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    installation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    private: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Allowlist flag: the agent may only read/write repositories with enabled=True.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Trusted, admin-configured validation commands (argv lists). Never model-provided.
    test_commands_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    mappings: Mapped[list[RepositoryMapping]] = relationship(back_populates="repository")


class RepositoryMapping(TimestampMixin, Base):
    """Sentry organization/project -> repository -> branch -> optional path prefixes."""

    __tablename__ = "repository_mappings"
    __table_args__ = (UniqueConstraint("sentry_organization", "sentry_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sentry_organization: Mapped[str] = mapped_column(String(100), nullable=False)
    sentry_project: Mapped[str] = mapped_column(String(100), nullable=False)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Prefixes in the repository where application code lives (e.g. "src/").
    path_prefixes_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Prefixes to strip from stack-trace paths (e.g. "/app/", "/srv/www/").
    strip_prefixes_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    repository: Mapped[Repository] = relationship(back_populates="mappings")


class SentryIssue(TimestampMixin, Base):
    __tablename__ = "sentry_issues"
    __table_args__ = (
        UniqueConstraint("organization", "sentry_issue_id"),
        Index("ix_sentry_issues_last_seen", "last_seen"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sentry_issue_id: Mapped[str] = mapped_column(String(64), nullable=False)
    short_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    organization: Mapped[str] = mapped_column(String(100), nullable=False)
    project: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    culprit: Mapped[str | None] = mapped_column(String(500), nullable=True)
    exception_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    level: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    permalink: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    first_seen: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latest_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release: Mapped[str | None] = mapped_column(String(250), nullable=True)
    repository_id: Mapped[int | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    raw_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    repository: Mapped[Repository | None] = relationship()


class AnalysisRun(TimestampMixin, Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (Index("ix_analysis_runs_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sentry_issue_id: Mapped[int] = mapped_column(
        ForeignKey("sentry_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repository_id: Mapped[int | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="RECEIVED")
    current_step: Mapped[str | None] = mapped_column(String(80), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    autonomy_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    root_cause_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_status: Mapped[str | None] = mapped_column(String(30), nullable=True)  # provisional|...
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    llm_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_revision_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    base_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    repair_attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retry_of_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    # Durable agent working state (evidence, diagnosis, plan, ...). Bounded, no secrets.
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)

    sentry_issue: Mapped[SentryIssue] = relationship()
    repository: Mapped[Repository | None] = relationship()
    steps: Mapped[list[AgentStep]] = relationship(
        back_populates="run", order_by="AgentStep.sequence_number", cascade="all, delete-orphan"
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "sequence_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(60), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    input_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    run: Mapped[AnalysisRun] = relationship(back_populates="steps")


class RunEvent(Base):
    """Persisted progress events. The autoincrement id is the SSE event id for replay."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class ProposedPatch(TimestampMixin, Base):
    __tablename__ = "proposed_patches"
    __table_args__ = (UniqueConstraint("run_id", "attempt_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    patch_content: Mapped[str] = mapped_column(Text, nullable=False)
    diff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    files_changed_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    lines_added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lines_removed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    policy_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    policy_findings_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TestExecution(Base):
    __tablename__ = "test_executions"
    __test__ = False  # not a pytest test class

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patch_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposed_patches.id", ondelete="CASCADE"), nullable=True, index=True
    )
    phase: Mapped[str] = mapped_column(String(20), nullable=False)  # setup|baseline|patched|static
    backend: Mapped[str] = mapped_column(String(20), nullable=False)  # docker|local|simulated|none
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    command_summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_failed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    truncated_output: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class GitHubIssue(Base):
    __tablename__ = "github_issues"
    __table_args__ = (UniqueConstraint("repository_id", "external_issue_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sentry_issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("sentry_issues.id", ondelete="SET NULL"), nullable=True, index=True
    )
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    external_issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    external_issue_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class PullRequest(TimestampMixin, Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repository_id", "external_pr_number"),
        UniqueConstraint("repository_id", "branch_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    external_pr_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    head_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    draft: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    validation_outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("run_id", "patch_id", "approval_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patch_id: Mapped[int] = mapped_column(
        ForeignKey("proposed_patches.id", ondelete="CASCADE"), nullable=False
    )
    approval_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    reviewer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("provider", "delivery_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    processing_status: Mapped[str] = mapped_column(String(20), nullable=False, default="received")
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_resource", "resource_type", "resource_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)  # user|agent|system|webhook
    actor_id: Mapped[str | None] = mapped_column(String(320), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False, index=True)


class Job(Base):
    """Durable SQLite-backed work queue (single application instance)."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow,
                                                 nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AppSetting(Base):
    """Runtime overrides for non-secret settings (autonomy mode, limits, ...)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow,
                                                 nullable=False)


class IntegrationCredential(Base):
    """Write-only credentials saved through the UI, encrypted with APP_SECRET_KEY."""

    __tablename__ = "integration_credentials"
    __table_args__ = (UniqueConstraint("provider", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow,
                                                 nullable=False)


class IntegrationStatus(Base):
    __tablename__ = "integration_status"

    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_version: Mapped[str] = mapped_column(String(40), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    llm_mode: Mapped[str] = mapped_column(String(20), nullable=False)  # scripted|groq
    config_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    results_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
