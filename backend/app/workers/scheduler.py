"""In-process worker pool for the SQLite job queue (single application instance).

* A bounded number of asyncio workers claim jobs atomically.
* A heartbeat extends each running job's lease; stale leases are reclaimed periodically.
* On startup, jobs left ``running`` by a previous process are requeued and mid-workflow runs
  without a job are re-enqueued (restart recovery).
* Shutdown stops claiming, waits briefly for in-flight jobs, then cancels them. Their leases
  expire and the work resumes on the next start; handlers are idempotent or check external
  state before repeating writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.enums import JobKind
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.db.session import with_write_retry
from app.workers import job_runner
from app.workers.retry_policy import is_retryable

logger = get_logger(__name__)

JobHandler = Callable[[dict[str, Any]], Awaitable[None]]


class WorkerPool:
    def __init__(self, handlers: dict[str, JobHandler], *, concurrency: int, lease_seconds: int,
                 poll_interval: float, on_exhausted: Callable[[str, dict[str, Any], str], None] | None = None) -> None:
        self.handlers = handlers
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self.on_exhausted = on_exhausted
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self.active_jobs: set[int] = set()

    def wake(self) -> None:
        self._wake.set()

    def recover(self) -> dict[str, Any]:
        from app.services.run_service import recover_orphaned_runs

        requeued = with_write_retry(job_runner.requeue_all_running)
        recovered = with_write_retry(recover_orphaned_runs)
        if requeued or recovered:
            logger.info("restart recovery", extra={"jobs_requeued": requeued, "runs_recovered": len(recovered)})
        return {"jobs_requeued": requeued, "runs_recovered": recovered}

    async def start(self) -> None:
        self.recover()
        self._stopping.clear()
        for index in range(self.concurrency):
            self._tasks.append(asyncio.create_task(self._loop(index), name=f"patchora-worker-{index}"))
        self._tasks.append(asyncio.create_task(self._reaper(), name="patchora-reaper"))

    async def stop(self, grace_seconds: float = 10.0) -> None:
        self._stopping.set()
        self._wake.set()
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    async def _reaper(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(Exception):
                reclaimed = await asyncio.to_thread(with_write_retry, job_runner.reclaim_expired)
                if reclaimed:
                    logger.warning("reclaimed expired job leases", extra={"count": reclaimed})
                    self._wake.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=max(5.0, self.lease_seconds / 3))

    async def _heartbeat(self, job_id: int) -> None:
        while True:
            await asyncio.sleep(max(1.0, self.lease_seconds / 3))
            with contextlib.suppress(Exception):
                await asyncio.to_thread(with_write_retry, lambda s: job_runner.heartbeat(
                    s, job_id, self.worker_id, self.lease_seconds))

    async def _loop(self, index: int) -> None:
        while not self._stopping.is_set():
            try:
                job = await asyncio.to_thread(
                    with_write_retry, lambda s: job_runner.claim(s, self.worker_id, self.lease_seconds))
            except Exception:
                logger.exception("job claim failed")
                job = None
            if job is None:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                continue
            await self.run_job(job.id, job.kind, dict(job.payload_json))

    async def run_job(self, job_id: int, kind: str, payload: dict[str, Any]) -> None:
        handler = self.handlers.get(kind)
        self.active_jobs.add(job_id)
        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        try:
            if handler is None:
                raise ValueError(f"No handler for job kind {kind}")
            await handler(payload)
        except asyncio.CancelledError:
            raise  # shutdown: the lease expires and the job is recovered on restart
        except Exception as exc:
            retryable = is_retryable(exc)
            message = exc.message if isinstance(exc, ExternalServiceError) else f"{type(exc).__name__}: {exc}"
            rescheduled = await asyncio.to_thread(
                with_write_retry,
                lambda s: job_runner.fail(s, job_id, self.worker_id, message, retryable=retryable))
            if not rescheduled and self.on_exhausted is not None:
                await asyncio.to_thread(self.on_exhausted, kind, payload, message)
            if not retryable:
                logger.exception("job failed", extra={"job_id": job_id, "kind": kind})
            else:
                logger.warning("job will be retried" if rescheduled else "job retries exhausted",
                               extra={"job_id": job_id, "kind": kind})
        else:
            await asyncio.to_thread(with_write_retry, lambda s: job_runner.complete(s, job_id, self.worker_id))
        finally:
            heartbeat.cancel()
            self.active_jobs.discard(job_id)


def default_on_exhausted(kind: str, payload: dict[str, Any], error: str) -> None:
    if kind == JobKind.RUN_WORKFLOW.value and "run_id" in payload:
        from app.agent.graph import mark_run_failed

        mark_run_failed(int(payload["run_id"]), error)
