"""Domain exception hierarchy.

Services raise these; :mod:`app.core.errors` converts them to consistent HTTP responses.
External-service errors carry a ``retryable`` flag so the retry policy can distinguish
transient failures from permanent ones.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InvalidRequestError(AppError):
    status_code = 422
    code = "invalid_request"


class AuthenticationRequired(AppError):
    status_code = 401
    code = "authentication_required"


class PermissionDenied(AppError):
    status_code = 403
    code = "permission_denied"


class NotConfiguredError(AppError):
    """An integration is missing required configuration."""

    status_code = 503
    code = "not_configured"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class PolicyViolation(AppError):
    """A security or autonomy policy blocked an action."""

    status_code = 403
    code = "policy_violation"


class ExternalServiceError(AppError):
    status_code = 502
    code = "external_service_error"

    def __init__(
        self,
        message: str,
        *,
        service: str,
        retryable: bool = False,
        upstream_status: int | None = None,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.service = service
        self.retryable = retryable
        self.upstream_status = upstream_status
        self.retry_after = retry_after


class ExternalAuthError(ExternalServiceError):
    code = "external_auth_error"


class ExternalNotFound(ExternalServiceError):
    code = "external_not_found"


class LLMOutputError(AppError):
    """The model returned output that failed schema validation after bounded retries."""

    status_code = 502
    code = "llm_output_invalid"


class CancelledByUser(AppError):
    status_code = 409
    code = "cancelled"
