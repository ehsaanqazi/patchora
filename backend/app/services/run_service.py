"""Run lifecycle: issue ingestion, run creation/deduplication, cancellation, retry, recovery."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agent.transitions import is_cancellable, validate_transition
from app.config import AutonomyMode
from app.core.enums import (
    PARKED_STATES,
    TERMINAL_STATES,
    ApprovalStatus,
    JobKind,
    RunOutcome,
    RunState,
)
from app.core.exceptions import ConflictError, InvalidRequestError
from app.db.base import utcnow
from app.db.models import AnalysisRun, Approval, PullRequest, RepositoryMapping, SentryIssue, WebhookDelivery
from app.schemas.issues import NormalizedEvent, NormalizedIssue
from app.services import audit_service
from app.workers import job_runner

ACTIVE_STATES = [s.value for s in RunState if s not in TERMINAL_STATES]


def upsert_issue(session: Session, issue: NormalizedIssue, *, is_demo: bool = False,
                 event: NormalizedEvent | None = None) -> SentryIssue:
    row = session.scalar(select(SentryIssue).where(
        SentryIssue.organization == issue.organization,
        SentryIssue.sentry_issue_id == issue.sentry_issue_id,
    ))
    if row is None:
        row = SentryIssue(sentry_issue_id=issue.sentry_issue_id, organization=issue.organization,
                          project=issue.project, title=issue.title, is_demo=is_demo)
        session.add(row)
    elif row.is_demo != is_demo:
        raise ConflictError("Refusing to mix demo and real data for the same Sentry issue id.")
    row.short_id = issue.short_id
    row.project = issue.project
    row.title = issue.title
    row.culprit = issue.culprit
    row.exception_type = issue.exception_type
    row.level = issue.level
    row.status = issue.status
    row.permalink = issue.permalink
    row.first_seen = issue.first_seen
    row.last_seen = issue.last_seen
    row.event_count = issue.event_count
    row.raw_metadata_json = issue.metadata
    if event is not None:
        row.latest_event_id = event.event_id or row.latest_event_id
        row.release = event.release or row.release
    if row.repository_id is None:
        mapping = find_mapping(session, issue.organization, issue.project)
        if mapping is not None:
            row.repository_id = mapping.repository_id
    session.flush()
    return row


def find_mapping(session: Session, organization: str, project: str) -> RepositoryMapping | None:
    return session.scalar(select(RepositoryMapping).where(
        RepositoryMapping.sentry_organization == organization,
        RepositoryMapping.sentry_project == project,
    ))


def active_run_for_issue(session: Session, issue_id: int) -> AnalysisRun | None:
    return session.scalar(
        select(AnalysisRun).where(AnalysisRun.sentry_issue_id == issue_id,
                                  AnalysisRun.status.in_(ACTIVE_STATES))
        .order_by(AnalysisRun.id.desc())
    )


def create_run(
    session: Session,
    issue: SentryIssue,
    *,
    trigger: str,
    actor_type: str,
    actor_id: str | None,
    autonomy_mode: AutonomyMode,
    repository_id: int | None = None,
    idempotency_key: str | None = None,
    extra_state: dict[str, Any] | None = None,
    retry_of: int | None = None,
) -> tuple[AnalysisRun, bool]:
    """Create and enqueue a run. Returns (run, created). Duplicate requests return the existing run."""
    if idempotency_key:
        existing = session.scalar(select(AnalysisRun).where(AnalysisRun.idempotency_key == idempotency_key))
        if existing is not None:
            return existing, False
    active = active_run_for_issue(session, issue.id)
    if active is not None:
        return active, False
    run = AnalysisRun(
        sentry_issue_id=issue.id,
        repository_id=repository_id or issue.repository_id,
        status=RunState.RECEIVED.value,
        current_step="queued",
        autonomy_mode=autonomy_mode.value,
        trigger=trigger,
        idempotency_key=idempotency_key,
        is_demo=issue.is_demo,
        state_json=extra_state or {},
        created_by=actor_id,
        retry_of_run_id=retry_of,
    )
    session.add(run)
    session.flush()
    job_runner.enqueue(session, JobKind.RUN_WORKFLOW, {"run_id": run.id}, max_attempts=4)
    audit_service.record(session, actor_type=actor_type, actor_id=actor_id, action="run.created",
                         resource_type="analysis_run", resource_id=run.id,
                         details={"issue": issue.sentry_issue_id, "trigger": trigger,
                                  "mode": autonomy_mode.value, "demo": issue.is_demo})
    audit_service.emit_event(session, run.id, "run_queued", "Analysis queued",
                             {"trigger": trigger, "mode": autonomy_mode.value})
    return run, True


def cancel_run(session: Session, run: AnalysisRun, *, actor_id: str, reason: str = "") -> AnalysisRun:
    state = RunState(run.status)
    if state in TERMINAL_STATES:
        raise ConflictError(f"Run {run.id} has already finished ({state.value}).")
    if not is_cancellable(state):
        raise ConflictError(f"Run {run.id} cannot be cancelled while {state.value}.")
    if state in PARKED_STATES or not job_runner.has_active_job(session, run.id):
        # Nothing is executing: cancel immediately.
        validate_transition(state, RunState.CANCELLED)
        run.status = RunState.CANCELLED.value
        run.outcome = RunOutcome.CANCELLED.value
        run.completed_at = utcnow()
        run.current_step = "cancelled"
        for approval in session.scalars(select(Approval).where(Approval.run_id == run.id,
                                                               Approval.status == ApprovalStatus.PENDING.value)):
            approval.status = ApprovalStatus.EXPIRED.value
            approval.resolved_at = utcnow()
        job_runner.cancel_jobs_for_run(session, run.id)
        audit_service.emit_event(session, run.id, "run_cancelled", "Run cancelled", {"reason": reason[:200]})
    else:
        # A worker owns the run: request cooperative cancellation at the next step boundary.
        run.cancel_requested = True
        audit_service.emit_event(session, run.id, "cancel_requested", "Cancellation requested")
    audit_service.record(session, actor_type="user", actor_id=actor_id, action="run.cancel",
                         resource_type="analysis_run", resource_id=run.id, details={"reason": reason[:200]})
    return run


def retry_run(session: Session, run: AnalysisRun, *, actor_id: str) -> AnalysisRun:
    if RunState(run.status) not in TERMINAL_STATES:
        raise ConflictError("Only finished runs can be retried.")
    issue = session.get(SentryIssue, run.sentry_issue_id)
    if issue is None:
        raise InvalidRequestError("The run's Sentry issue no longer exists.")
    state = {k: v for k, v in (run.state_json or {}).items() if k in {"demo_scenario", "use_real_llm"}}
    new_run, created = create_run(
        session, issue, trigger="retry", actor_type="user", actor_id=actor_id,
        autonomy_mode=AutonomyMode(run.autonomy_mode), repository_id=run.repository_id,
        extra_state=state, retry_of=run.id,
    )
    if not created:
        raise ConflictError(f"Run {new_run.id} is already active for this issue.")
    return new_run


def recover_orphaned_runs(session: Session) -> list[int]:
    """Re-enqueue runs that are mid-workflow but have no active job (e.g. crash before enqueue)."""
    recovered: list[int] = []
    for run in session.scalars(select(AnalysisRun).where(AnalysisRun.status.in_(ACTIVE_STATES))):
        if RunState(run.status) in PARKED_STATES:
            continue
        if not job_runner.has_active_job(session, run.id):
            job_runner.enqueue(session, JobKind.RUN_WORKFLOW, {"run_id": run.id}, max_attempts=4)
            audit_service.emit_event(session, run.id, "run_recovered",
                                     f"Workflow recovered after restart at {run.status}")
            recovered.append(run.id)
    return recovered


def apply_run_retention(session: Session, retention_days: int) -> dict[str, int]:
    """Delete finished runs (and their steps, events, patches, tests) older than the retention
    window, plus processed webhook deliveries. Runs that produced a pull request are kept as the
    record of what was published; audit entries follow their own retention."""
    cutoff = utcnow() - timedelta(days=retention_days)
    finished = [s.value for s in TERMINAL_STATES]
    with_pr = select(PullRequest.run_id)
    runs = session.scalars(select(AnalysisRun).where(
        AnalysisRun.status.in_(finished), AnalysisRun.completed_at < cutoff, AnalysisRun.id.notin_(with_pr)))
    removed_runs = 0
    for run in runs:
        session.delete(run)
        removed_runs += 1
    result = session.execute(delete(WebhookDelivery).where(
        WebhookDelivery.received_at < cutoff, WebhookDelivery.processing_status != "received"))
    return {"runs": removed_runs, "webhook_deliveries": int(getattr(result, "rowcount", 0) or 0)}
