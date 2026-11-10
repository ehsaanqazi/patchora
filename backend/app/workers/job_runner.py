"""Durable SQLite-backed job queue.

* ``enqueue`` is idempotent per key and refuses a second active workflow job for the same run.
* ``claim`` atomically moves one due job to ``running`` with a lease using a single
  ``UPDATE ... WHERE id = (SELECT ...) RETURNING`` statement (SQLite >= 3.35).
* Leases are extended by a heartbeat while a job runs; expired leases are reclaimed so a
  crashed worker's job is retried. Because this queue serves a single application instance,
  all ``running`` jobs found at startup are stale and are requeued immediately.
* The interface (enqueue/claim/complete/fail) is small so a production queue can replace it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import JobKind, JobStatus
from app.db.base import utcnow
from app.db.models import Job
from app.workers.retry_policy import job_backoff_seconds

ACTIVE = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


def _sql_ts(dt: datetime) -> str:
    """Format exactly as SQLAlchemy stores DateTime values in SQLite (naive UTC)."""
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def enqueue(
    session: Session,
    kind: JobKind,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    delay_seconds: float = 0,
) -> Job | None:
    """Add a job. Returns None when an equivalent job already exists (dedup)."""
    if idempotency_key is not None:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            return None
    if kind == JobKind.RUN_WORKFLOW:
        run_id = payload.get("run_id")
        for job in session.scalars(select(Job).where(Job.kind == kind.value, Job.status.in_(ACTIVE))):
            if job.payload_json.get("run_id") == run_id:
                return None
    job = Job(kind=kind.value, payload_json=payload, idempotency_key=idempotency_key,
              max_attempts=max_attempts, run_after=utcnow() + timedelta(seconds=delay_seconds))
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return job


def claim(session: Session, worker_id: str, lease_seconds: int) -> Job | None:
    now = utcnow()
    row = session.execute(
        text(
            "UPDATE jobs SET status = :running, lease_owner = :owner, lease_expires_at = :lease, "
            "attempts = attempts + 1, updated_at = :now "
            "WHERE id = (SELECT id FROM jobs WHERE status = :queued AND run_after <= :now "
            "ORDER BY run_after, id LIMIT 1) AND status = :queued RETURNING id"
        ),
        {"running": JobStatus.RUNNING.value, "queued": JobStatus.QUEUED.value, "owner": worker_id,
         "lease": _sql_ts(now + timedelta(seconds=lease_seconds)), "now": _sql_ts(now)},
    ).first()
    if row is None:
        return None
    session.flush()
    job = session.get(Job, row[0], populate_existing=True)
    return job


def heartbeat(session: Session, job_id: int, worker_id: str, lease_seconds: int) -> bool:
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_owner == worker_id, Job.status == JobStatus.RUNNING.value)
        .values(lease_expires_at=utcnow() + timedelta(seconds=lease_seconds))
    )
    return bool(getattr(result, "rowcount", 0))


def complete(session: Session, job_id: int, worker_id: str) -> None:
    session.execute(
        update(Job).where(Job.id == job_id, Job.lease_owner == worker_id)
        .values(status=JobStatus.SUCCEEDED.value, lease_owner=None, lease_expires_at=None,
                completed_at=utcnow())
    )


def fail(session: Session, job_id: int, worker_id: str, error: str, *, retryable: bool) -> bool:
    """Record a failure. Returns True if the job was rescheduled."""
    job = session.get(Job, job_id)
    if job is None or job.lease_owner != worker_id:
        return False
    job.last_error = error[:2000]
    job.lease_owner = None
    job.lease_expires_at = None
    if retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED.value
        job.run_after = utcnow() + timedelta(seconds=job_backoff_seconds(job.attempts))
        return True
    job.status = JobStatus.FAILED.value
    job.completed_at = utcnow()
    return False


def cancel_jobs_for_run(session: Session, run_id: int) -> int:
    count = 0
    for job in session.scalars(select(Job).where(Job.status == JobStatus.QUEUED.value)):
        if job.payload_json.get("run_id") == run_id:
            job.status = JobStatus.CANCELLED.value
            job.completed_at = utcnow()
            count += 1
    return count


def reclaim_expired(session: Session) -> int:
    result = session.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING.value, Job.lease_expires_at < utcnow())
        .values(status=JobStatus.QUEUED.value, lease_owner=None, lease_expires_at=None,
                last_error="lease expired; job recovered")
    )
    return int(getattr(result, "rowcount", 0) or 0)


def requeue_all_running(session: Session) -> int:
    """Startup recovery for a single-instance deployment."""
    result = session.execute(
        update(Job).where(Job.status == JobStatus.RUNNING.value)
        .values(status=JobStatus.QUEUED.value, lease_owner=None, lease_expires_at=None,
                last_error="process restarted; job recovered")
    )
    return int(getattr(result, "rowcount", 0) or 0)


def has_active_job(session: Session, run_id: int) -> bool:
    for job in session.scalars(select(Job).where(Job.kind == JobKind.RUN_WORKFLOW.value,
                                                 Job.status.in_(ACTIVE))):
        if job.payload_json.get("run_id") == run_id:
            return True
    return False
