"""Application configuration.

Settings are loaded from OS environment variables and an optional ``.env`` file at the
repository root. Integration credentials are optional: the application starts in an
"unconfigured" state and the dashboard explains what is missing.

Runtime-tunable, non-secret settings (autonomy mode, limits, protected paths, ...) can be
overridden through the Settings page; those overrides live in the ``app_settings`` table and
are resolved by :mod:`app.services.settings_service`. Values here are the defaults.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"


class AutonomyMode(StrEnum):
    ANALYZE_ONLY = "analyze_only"  # Mode A
    PREPARE_PATCH = "prepare_patch"  # Mode B (default)
    AUTO_PR = "auto_pr"  # Mode C


class SandboxBackend(StrEnum):
    DOCKER = "docker"
    LOCAL = "local"  # development only, weak isolation
    DISABLED = "disabled"


DEFAULT_PROTECTED_PATHS: list[str] = [
    ".git/**",
    ".github/**",
    ".gitlab-ci.yml",
    ".circleci/**",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "CODEOWNERS",
    "**/CODEOWNERS",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/id_rsa*",
    "**/secrets/**",
    "**/Dockerfile",
    "**/Dockerfile.*",
    "**/docker-compose*.yml",
    "**/docker-compose*.yaml",
    "**/k8s/**",
    "**/helm/**",
    "**/terraform/**",
    "**/*.tf",
    "**/deploy/**",
    "**/.npmrc",
    "**/.pypirc",
    "**/setup.py",
    "**/setup.cfg",
    "**/pyproject.toml",
    "**/package.json",
    "**/package-lock.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/poetry.lock",
    "**/requirements*.txt",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    app_secret_key: SecretStr | None = None
    app_admin_token: SecretStr | None = None
    auth_mode: Literal["local", "token"] = "local"
    frontend_origin: str = "http://localhost:3000"
    backend_port: int = 8000
    log_level: str = "INFO"
    log_json: bool = False

    database_url: str = f"sqlite:///{(REPO_ROOT / 'data' / 'patchora.db').as_posix()}"
    data_dir: Path = REPO_ROOT / "data"

    # --- Groq ---------------------------------------------------------------------------
    groq_api_key: SecretStr | None = None
    groq_model: str | None = None
    groq_base_url: str | None = None
    groq_timeout_seconds: float = 60.0
    groq_max_retries: int = 3
    groq_max_input_tokens: int = 24_000
    groq_max_output_tokens: int = 4_096
    groq_structured_output_mode: Literal["json_object", "json_schema"] = "json_object"
    groq_temperature: float = 0.1
    # Optional pricing for cost estimates (USD per 1M tokens). Leave empty when unknown;
    # the app never fabricates a cost.
    groq_price_input_per_million: float | None = None
    groq_price_output_per_million: float | None = None

    # --- GitHub -------------------------------------------------------------------------
    github_api_url: str = "https://api.github.com"
    github_app_id: str | None = None
    github_private_key_path: Path | None = None
    github_installation_id: str | None = None
    github_webhook_secret: SecretStr | None = None
    github_personal_access_token: SecretStr | None = None
    github_timeout_seconds: float = 30.0
    github_max_file_bytes: int = 300_000
    github_max_tarball_bytes: int = 200_000_000

    # --- Sentry -------------------------------------------------------------------------
    sentry_base_url: str = "https://sentry.io"
    sentry_auth_token: SecretStr | None = None
    sentry_webhook_secret: SecretStr | None = None
    sentry_org: str | None = None
    sentry_project: str | None = None
    sentry_timeout_seconds: float = 30.0

    # --- Agent & policy defaults (overridable at runtime through the Settings page) ----
    autonomy_mode: AutonomyMode = AutonomyMode.PREPARE_PATCH
    auto_create_pull_requests: bool = False
    max_agent_retries: int = Field(default=2, ge=0, le=5)
    max_test_runtime_seconds: int = Field(default=300, ge=10, le=3600)
    max_patch_files: int = Field(default=10, ge=1, le=50)
    max_patch_lines: int = Field(default=500, ge=1, le=5000)
    protected_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_PROTECTED_PATHS))
    pr_draft: bool = True
    webhook_auto_analyze: bool = True
    create_issue_on_human_review: bool = False
    audit_retention_days: int = Field(default=365, ge=7)
    run_retention_days: int = Field(default=180, ge=7)

    # --- Sandbox ------------------------------------------------------------------------
    sandbox_enabled: bool = True
    sandbox_backend: SandboxBackend = SandboxBackend.DOCKER
    allow_unsafe_local_execution: bool = False
    sandbox_python_image: str = "python:3.12-slim"
    sandbox_node_image: str = "node:20-slim"
    sandbox_cpus: float = 1.0
    sandbox_memory_mb: int = 1024
    sandbox_pids_limit: int = 256
    sandbox_max_output_bytes: int = 64_000
    sandbox_allow_dependency_install: bool = True
    sandbox_setup_timeout_seconds: int = 600

    # --- Workers ------------------------------------------------------------------------
    worker_enabled: bool = True
    worker_concurrency: int = Field(default=2, ge=1, le=8)
    worker_poll_interval_seconds: float = 1.0
    job_lease_seconds: int = 120

    # --- FAISS (optional) ---------------------------------------------------------------
    faiss_enabled: bool = False
    faiss_index_path: Path = REPO_ROOT / "data" / "faiss_index"
    embedding_model: str | None = None

    # --- Rate limiting ------------------------------------------------------------------
    webhook_rate_limit_per_minute: int = 120

    @field_validator("protected_paths", mode="before")
    @classmethod
    def _split_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        return value

    @field_validator(
        "groq_model",
        "groq_base_url",
        "github_app_id",
        "github_installation_id",
        "sentry_org",
        "sentry_project",
        "embedding_model",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "groq_api_key",
        "app_secret_key",
        "app_admin_token",
        "github_webhook_secret",
        "github_personal_access_token",
        "sentry_auth_token",
        "sentry_webhook_secret",
        "github_private_key_path",
        mode="before",
    )
    @classmethod
    def _blank_secret_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_security(self) -> Settings:
        if self.app_env == "production":
            if self.auth_mode == "local":
                raise ValueError("AUTH_MODE=local is a single-user development mode and is not allowed "
                                 "when APP_ENV=production. Use AUTH_MODE=token.")
            if self.app_secret_key is None or len(self.app_secret_key.get_secret_value()) < 32:
                raise ValueError("APP_SECRET_KEY must be set to at least 32 characters in production.")
            if self.sandbox_backend == SandboxBackend.LOCAL:
                raise ValueError("SANDBOX_BACKEND=local is not permitted in production.")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def secret_values(self) -> list[str]:
        """Every configured secret value, used to scan diffs and logs for leaks."""
        values: list[str] = []
        for field in (
            self.groq_api_key,
            self.app_secret_key,
            self.app_admin_token,
            self.github_webhook_secret,
            self.github_personal_access_token,
            self.sentry_auth_token,
            self.sentry_webhook_secret,
        ):
            if field is not None:
                raw = field.get_secret_value()
                if len(raw) >= 8:
                    values.append(raw)
        return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # PATCHORA_ENV_FILE overrides the .env location; an empty value disables it (used by tests).
    env_file = os.environ.get("PATCHORA_ENV_FILE", str(REPO_ROOT / ".env"))
    return Settings(_env_file=env_file or None)


def reset_settings_cache() -> None:
    get_settings.cache_clear()
