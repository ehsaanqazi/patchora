"""Bounded retries with exponential backoff and full jitter.

Only errors classified as retryable are retried. Permanent errors (authentication,
validation, not-found, policy) fail immediately.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.exceptions import ExternalServiceError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 20.0

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Delay before retry number ``attempt`` (1-based)."""
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self.max_delay)
        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return random.uniform(0, ceiling)  # full jitter


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ExternalServiceError):
        return exc.retryable
    return isinstance(exc, (TimeoutError, ConnectionError))


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except Exception as exc:
            if attempt >= policy.max_attempts or not is_retryable(exc):
                raise
            retry_after = exc.retry_after if isinstance(exc, ExternalServiceError) else None
            delay = policy.delay_for(attempt, retry_after)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await sleep(delay)


def job_backoff_seconds(attempt: int, base: float = 5.0, cap: float = 300.0) -> float:
    """Backoff for re-scheduling a failed background job."""
    return min(cap, base * (2 ** max(0, attempt - 1))) * random.uniform(0.75, 1.25)
