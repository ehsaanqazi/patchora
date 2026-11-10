"""Webhook signature verification and simple rate limiting.

Sentry integration-platform webhooks: ``Sentry-Hook-Signature`` is the hex HMAC-SHA256 of
the raw request body keyed with the integration's client secret.

GitHub webhooks: ``X-Hub-Signature-256`` is ``sha256=`` + hex HMAC-SHA256 of the raw body
keyed with the webhook secret.

Both comparisons use constant-time equality.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque


def compute_hmac_sha256(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_sentry_signature(secret: str | None, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    expected = compute_hmac_sha256(secret, body)
    return hmac.compare_digest(expected, signature.strip().lower())


def verify_github_signature(secret: str | None, body: bytes, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + compute_hmac_sha256(secret, body)
    return hmac.compare_digest(expected, signature.strip().lower())


def payload_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class SlidingWindowRateLimiter:
    """In-process per-key rate limiter (single-instance deployment)."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # bound memory under key-spraying
                for stale in [k for k, v in self._hits.items() if not v]:
                    del self._hits[stale]
            return True
