"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent.graph import WorkflowEngine
from app.api.routes import (
    approvals,
    dashboard,
    evaluations,
    events,
    health,
    integrations,
    issues,
    pull_requests,
    repositories,
    runs,
    sentry,
)
from app.api.routes import settings as settings_routes
from app.config import Settings, get_settings
from app.core.enums import JobKind
from app.core.errors import error_body, install_error_handlers
from app.core.logging import configure_logging, get_logger, register_secrets
from app.db.migrate import upgrade_to_head
from app.db.session import init_engine, with_write_retry
from app.providers.factory import ProviderFactory
from app.security.webhook_validation import SlidingWindowRateLimiter
from app.services import audit_service, run_service, webhook_service
from app.services.credential_service import all_secret_values
from app.services.settings_service import get_runtime_settings
from app.workers.scheduler import WorkerPool, default_on_exhausted

logger = get_logger(__name__)

API_PREFIX = "/api/v1"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "patchora"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _startup_housekeeping(settings: Settings) -> None:
    def _do(session: Any) -> None:
        runtime = get_runtime_settings(session, settings)
        removed = audit_service.apply_retention(session, runtime.audit_retention_days)
        if removed:
            logger.info("audit retention applied", extra={"removed": removed})
        pruned = run_service.apply_run_retention(session, runtime.run_retention_days)
        if any(pruned.values()):
            logger.info("run retention applied", extra=pruned)
        register_secrets(all_secret_values(session, settings))

    with_write_retry(_do)


def create_app(settings: Settings | None = None, *, start_workers: bool | None = None,
               run_migrations: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    register_secrets(settings.secret_values())
    workers_enabled = settings.worker_enabled if start_workers is None else start_workers

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if run_migrations:
            upgrade_to_head(settings.database_url)
        init_engine(settings.database_url)
        _startup_housekeeping(settings)
        app.state.worker_required = workers_enabled
        if workers_enabled:
            worker_pool = build_worker_pool(app.state.factory, settings)
            await worker_pool.start()
            app.state.worker_pool = worker_pool
        logger.info("Patchora started", extra={"env": settings.app_env, "workers": workers_enabled})
        try:
            yield
        finally:
            running = getattr(app.state, "worker_pool", None)
            if running is not None:
                await running.stop()
                app.state.worker_pool = None

    app = FastAPI(
        title="Patchora",
        version="0.1.0",
        description="Autonomous Sentry-to-GitHub bug investigation, repair, validation and pull requests.",
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.factory = ProviderFactory(settings)
    app.state.webhook_limiter = SlidingWindowRateLimiter(settings.webhook_rate_limit_per_minute)
    app.state.worker_pool = None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Requested-With", "Last-Event-ID", "Authorization"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request.state.request_id = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
        path = request.url.path
        # CSRF defence: state-changing API calls must send a custom header, which browsers only
        # allow cross-origin after a CORS preflight that this API rejects for foreign origins.
        # Webhooks are authenticated by signatures instead; bearer-token clients are not
        # cookie-authenticated and therefore not exposed to CSRF.
        if (
            request.method in UNSAFE_METHODS
            and path.startswith(API_PREFIX)
            and not path.startswith(f"{API_PREFIX}/webhooks/")
            and request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE
            and not request.headers.get("authorization", "").lower().startswith("bearer ")
        ):
            return JSONResponse(status_code=403, content=error_body(
                "csrf_check_failed", f"Missing '{CSRF_HEADER}: {CSRF_VALUE}' header.", request.state.request_id))
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    install_error_handlers(app)
    for module in (health, dashboard, integrations, sentry, repositories, issues, runs, events, approvals,
                   pull_requests, settings_routes, evaluations):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


def build_worker_pool(factory: ProviderFactory, settings: Settings) -> WorkerPool:
    engine = WorkflowEngine(factory)

    async def run_workflow(payload: dict[str, Any]) -> None:
        await engine.execute(int(payload["run_id"]))

    async def process_webhook(payload: dict[str, Any]) -> None:
        await webhook_service.process_delivery(factory, settings, int(payload["delivery_id"]))

    return WorkerPool(
        {JobKind.RUN_WORKFLOW.value: run_workflow, JobKind.PROCESS_WEBHOOK.value: process_webhook},
        concurrency=settings.worker_concurrency, lease_seconds=settings.job_lease_seconds,
        poll_interval=settings.worker_poll_interval_seconds, on_exhausted=default_on_exhausted,
    )

