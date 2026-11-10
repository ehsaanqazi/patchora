"""Effective runtime settings and integration credentials.

* Non-secret policy settings: environment defaults (:class:`app.config.Settings`) overridden
  by validated rows in ``app_settings`` (edited on the Settings page by admins).
* Credentials: environment variables, or write-only values saved through the Integrations
  page and encrypted with ``APP_SECRET_KEY``. Saved credentials are never returned by the API.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AutonomyMode, SandboxBackend, Settings, get_settings
from app.core.exceptions import InvalidRequestError
from app.db.base import utcnow
from app.db.models import AppSetting
from app.security.input_sanitization import validate_glob_pattern


class RuntimeSettings(BaseModel):
    autonomy_mode: AutonomyMode
    auto_create_pull_requests: bool
    max_agent_retries: int = Field(ge=0, le=5)
    max_test_runtime_seconds: int = Field(ge=10, le=3600)
    max_patch_files: int = Field(ge=1, le=50)
    max_patch_lines: int = Field(ge=1, le=5000)
    protected_paths: list[str]
    sandbox_backend: SandboxBackend
    allow_unsafe_local_execution: bool
    sandbox_allow_dependency_install: bool
    faiss_enabled: bool
    pr_draft: bool
    webhook_auto_analyze: bool
    create_issue_on_human_review: bool
    audit_retention_days: int = Field(ge=7, le=3650)
    run_retention_days: int = Field(ge=7, le=3650)

    @field_validator("protected_paths")
    @classmethod
    def _validate_globs(cls, value: list[str]) -> list[str]:
        if len(value) > 200:
            raise ValueError("too many protected path patterns")
        return [validate_glob_pattern(v) for v in value]



RUNTIME_KEYS = tuple(RuntimeSettings.model_fields.keys())
# Changing these alters what the agent is allowed to do; they are audited as security events.
SECURITY_SENSITIVE_KEYS = {
    "autonomy_mode",
    "auto_create_pull_requests",
    "protected_paths",
    "sandbox_backend",
    "allow_unsafe_local_execution",
    "max_patch_files",
    "max_patch_lines",
    "sandbox_allow_dependency_install",
}


def _defaults(settings: Settings) -> dict[str, Any]:
    return {key: getattr(settings, key) for key in RUNTIME_KEYS}


def get_runtime_settings(session: Session, settings: Settings | None = None) -> RuntimeSettings:
    settings = settings or get_settings()
    values = _defaults(settings)
    for row in session.scalars(select(AppSetting)):
        if row.key in values:
            values[row.key] = row.value_json
    try:
        return RuntimeSettings.model_validate(values)
    except ValidationError:
        # A corrupt override must never weaken policy: fall back to the environment defaults.
        return RuntimeSettings.model_validate(_defaults(settings))


def update_runtime_settings(
    session: Session, changes: dict[str, Any], actor: str, settings: Settings | None = None
) -> tuple[RuntimeSettings, dict[str, tuple[Any, Any]]]:
    settings = settings or get_settings()
    unknown = set(changes) - set(RUNTIME_KEYS)
    if unknown:
        raise InvalidRequestError(f"Unknown settings: {sorted(unknown)}")
    current = get_runtime_settings(session, settings)
    merged = current.model_dump(mode="json") | changes
    try:
        validated = RuntimeSettings.model_validate(merged)
    except ValidationError as exc:
        raise InvalidRequestError("Invalid settings", details={"errors": exc.errors(include_url=False)}) \
            from exc
    if settings.app_env == "production" and validated.sandbox_backend == SandboxBackend.LOCAL:
        raise InvalidRequestError("The local sandbox backend is not permitted in production.")
    if (
        validated.sandbox_backend == SandboxBackend.LOCAL
        and validated.allow_unsafe_local_execution
        and settings.app_env != "development"
    ):
        raise InvalidRequestError("Unsafe local execution can only be enabled in development.")

    dumped = validated.model_dump(mode="json")
    old = current.model_dump(mode="json")
    diff: dict[str, tuple[Any, Any]] = {}
    for key, value in dumped.items():
        if key in changes and old.get(key) != value:
            diff[key] = (old.get(key), value)
            row = session.get(AppSetting, key)
            if row is None:
                session.add(AppSetting(key=key, value_json=value, updated_by=actor))
            else:
                row.value_json = value
                row.updated_by = actor
                row.updated_at = utcnow()
    return validated, diff


