"""Shared HTTP plumbing for external integrations.

Maps transport and HTTP errors to :class:`ExternalServiceError` with a ``retryable`` flag,
parses ``Retry-After`` and rate-limit headers, and applies the bounded retry policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core.exceptions import ExternalAuthError, ExternalNotFound, ExternalServiceError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.workers.retry_policy import RetryPolicy, retry_async

logger = get_logger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - utcnow()).total_seconds())
            except (TypeError, ValueError):
                return None
    reset = response.headers.get("x-ratelimit-reset")
    remaining = response.headers.get("x-ratelimit-remaining")
    if reset and remaining == "0":
        try:
            return max(0.0, float(reset) - utcnow().timestamp())
        except ValueError:
            return None
    return None


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            if key in body:
                return str(body[key])[:300]
    return str(body)[:300]


def raise_for_response(service: str, response: httpx.Response) -> None:
    if response.is_success:
        return
    status = response.status_code
    message = f"{service} API returned {status}: {_error_message(response)}"
    if status in (401, 403):
        # GitHub returns 403 for secondary rate limits; treat those as retryable.
        if status == 403 and (
            response.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in message.lower()
        ):
            raise ExternalServiceError(message, service=service, retryable=True, upstream_status=status,
                                       retry_after=_retry_after(response))
        raise ExternalAuthError(message, service=service, upstream_status=status)
    if status == 404:
        raise ExternalNotFound(message, service=service, upstream_status=status)
    raise ExternalServiceError(
        message,
        service=service,
        retryable=status in RETRYABLE_STATUS,
        upstream_status=status,
        retry_after=_retry_after(response) if status in (429, 503) else None,
    )


async def request(
    client: httpx.AsyncClient,
    service: str,
    method: str,
    url: str,
    *,
    policy: RetryPolicy | None = None,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json: Any = None,
    expected_errors: tuple[int, ...] = (),
) -> httpx.Response:
    """Perform a request with bounded retries. Statuses in ``expected_errors`` are returned."""
    policy = policy or RetryPolicy()

    async def _do() -> httpx.Response:
        try:
            response = await client.request(method, url, headers=headers, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise ExternalServiceError(f"{service} request timed out", service=service,
                                       retryable=True) from exc
        except httpx.TransportError as exc:
            raise ExternalServiceError(f"{service} connection error: {type(exc).__name__}",
                                       service=service, retryable=True) from exc
        if response.status_code in expected_errors:
            return response
        raise_for_response(service, response)
        return response

    def _log(attempt: int, exc: BaseException, delay: float) -> None:
        logger.warning("retrying external request",
                       extra={"service": service, "attempt": attempt, "delay": round(delay, 2),
                              "error": type(exc).__name__})

    return await retry_async(_do, policy, on_retry=_log)


def parse_link_header(value: str | None) -> dict[str, dict[str, str]]:
    """Parse an RFC 8288 Link header into {rel: {"url": ..., **params}}."""
    links: dict[str, dict[str, str]] = {}
    if not value:
        return links
    for part in value.split(","):
        segments = part.strip().split(";")
        if not segments or not segments[0].strip().startswith("<"):
            continue
        url = segments[0].strip()[1:-1]
        params: dict[str, str] = {"url": url}
        for seg in segments[1:]:
            if "=" in seg:
                key, val = seg.strip().split("=", 1)
                params[key.strip()] = val.strip().strip('"')
        rel = params.get("rel")
        if rel:
            links[rel] = params
    return links
