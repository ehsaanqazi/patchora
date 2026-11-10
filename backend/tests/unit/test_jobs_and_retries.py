from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.enums import JobKind, JobStatus
from app.core.exceptions import ExternalAuthError, ExternalServiceError
from app.db.base import utcnow
from app.db.models import Job
from app.db.session import session_scope
from app.workers import job_runner
from app.workers.retry_policy import RetryPolicy, retry_async


async def _nosleep(_: float) -> None:
    return None


async def test_retry_only_retryable():
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ExternalServiceError("503", service="x", retryable=True)
        return "ok"

    assert await retry_async(flaky, RetryPolicy(max_attempts=3), sleep=_nosleep) == "ok"

    async def permanent() -> str:
        attempts["n"] += 1
        raise ExternalAuthError("401", service="x")

    attempts["n"] = 0
    with pytest.raises(ExternalAuthError):
        await retry_async(permanent, RetryPolicy(max_attempts=5), sleep=_nosleep)
    assert attempts["n"] == 1


async def test_retry_bounded():
    async def always() -> None:
        raise ExternalServiceError("timeout", service="x", retryable=True)

    with pytest.raises(ExternalServiceError):
        await retry_async(always, RetryPolicy(max_attempts=2), sleep=_nosleep)


def test_backoff_respects_retry_after_and_cap():
    policy = RetryPolicy(base_delay=1, max_delay=10)
    assert policy.delay_for(1, retry_after=3) == 3
    assert policy.delay_for(1, retry_after=100) == 10
    assert all(0 <= policy.delay_for(5) <= 10 for _ in range(50))


def test_enqueue_dedup_and_idempotency(settings):
    with session_scope() as s:
        assert job_runner.enqueue(s, JobKind.RUN_WORKFLOW, {"run_id": 1}) is not None
        assert job_runner.enqueue(s, JobKind.RUN_WORKFLOW, {"run_id": 1}) is None  # one active job per run
        assert job_runner.enqueue(s, JobKind.PROCESS_WEBHOOK, {"delivery_id": 5}, idempotency_key="k") is not None
        assert job_runner.enqueue(s, JobKind.PROCESS_WEBHOOK, {"delivery_id": 5}, idempotency_key="k") is None


def test_claim_is_exclusive_and_leases_expire(settings):
    with session_scope() as s:
        job_runner.enqueue(s, JobKind.RUN_WORKFLOW, {"run_id": 7})
    with session_scope() as s:
        first = job_runner.claim(s, "w1", 60)
        assert first is not None and first.status == JobStatus.RUNNING.value and first.attempts == 1
    with session_scope() as s:
        assert job_runner.claim(s, "w2", 60) is None
    with session_scope() as s:
        job = s.get(Job, first.id)
        job.lease_expires_at = utcnow() - timedelta(seconds=1)
    with session_scope() as s:
        assert job_runner.reclaim_expired(s) == 1
    with session_scope() as s:
        again = job_runner.claim(s, "w2", 60)
        assert again is not None and again.id == first.id and again.attempts == 2
        assert not job_runner.heartbeat(s, again.id, "w1", 60)  # old owner cannot extend
        assert job_runner.heartbeat(s, again.id, "w2", 60)


def test_failure_reschedules_until_exhausted(settings):
    with session_scope() as s:
        job = job_runner.enqueue(s, JobKind.RUN_WORKFLOW, {"run_id": 9}, max_attempts=2)
        job_id = job.id
    for expected in (True, False):
        with session_scope() as s:
            row = s.get(Job, job_id)
            row.run_after = utcnow() - timedelta(seconds=1)
        with session_scope() as s:
            claimed = job_runner.claim(s, "w", 60)
            assert claimed is not None
        with session_scope() as s:
            assert job_runner.fail(s, job_id, "w", "boom", retryable=True) is expected
    with session_scope() as s:
        assert s.get(Job, job_id).status == JobStatus.FAILED.value


def test_permanent_failure_not_rescheduled(settings):
    with session_scope() as s:
        job_id = job_runner.enqueue(s, JobKind.RUN_WORKFLOW, {"run_id": 10}).id
    with session_scope() as s:
        job_runner.claim(s, "w", 60)
    with session_scope() as s:
        assert job_runner.fail(s, job_id, "w", "bad", retryable=False) is False


def test_restart_requeues_running_jobs(settings):
    with session_scope() as s:
        job_runner.enqueue(s, JobKind.RUN_WORKFLOW, {"run_id": 11})
    with session_scope() as s:
        job_runner.claim(s, "dead-process", 600)
    with session_scope() as s:
        assert job_runner.requeue_all_running(s) == 1
    with session_scope() as s:
        assert job_runner.claim(s, "new-process", 60) is not None
