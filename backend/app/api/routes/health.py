"""Health, readiness and diagnostics (no secret values are ever returned)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings
from app.dependencies import current_principal, db_dep, factory_dep, settings_dep
from app.providers.factory import ProviderFactory
from app.security.authorization import Principal
from app.services.credential_service import credential_value, encryption_enabled
from app.services.integration_service import integration_cards
from app.services.search_service import faiss_available
from app.services.settings_service import get_runtime_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request, session: Session = Depends(db_dep)) -> JSONResponse:
    checks: dict[str, Any] = {}
    try:
        session.execute(text("SELECT 1"))
        version = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
        checks["database"] = {"ok": True, "migration": version}
    except Exception as exc:  # reported, not raised
        checks["database"] = {"ok": False, "error": type(exc).__name__}
    worker_required = bool(getattr(request.app.state, "worker_required", False))
    pool = getattr(request.app.state, "worker_pool", None)
    checks["workers"] = {"ok": pool is not None or not worker_required, "running": pool is not None}
    ok = all(c.get("ok") for c in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, "checks": checks})


@router.get("/diagnostics")
async def diagnostics(request: Request, session: Session = Depends(db_dep),
                      settings: Settings = Depends(settings_dep), factory: ProviderFactory = Depends(factory_dep),
                      _: Principal = Depends(current_principal)) -> dict[str, Any]:
    runtime = get_runtime_settings(session, settings)
    sandbox = factory.sandbox(runtime)
    sandbox_ok, sandbox_reason = await sandbox.available()
    faiss_ok, faiss_reason = faiss_available()
    cards = {c.provider: {"configured": c.configured, "mode": c.mode, "last_ok": c.last_ok}
             for c in integration_cards(session, settings)}
    return {
        "app_env": settings.app_env,
        "auth_mode": settings.auth_mode,
        "database": "sqlite" if settings.is_sqlite else "other",
        "integrations": cards,
        "llm": {"provider": runtime.llm_provider.value,
                "model": credential_value(session, runtime.llm_provider.value, "model", settings),
                "configured": cards.get(runtime.llm_provider.value, {}).get("configured", False),
                "max_input_tokens": settings.groq_max_input_tokens if runtime.llm_provider.value == "groq"
                else settings.llm_max_input_tokens},
        "sandbox": {"backend": sandbox.name, "isolation": sandbox.isolation, "available": sandbox_ok,
                    "detail": sandbox_reason,
                    "code_execution_enabled": sandbox_ok and sandbox.isolation in {"container", "process"}},
        "faiss": {"enabled": runtime.faiss_enabled, "installed": faiss_ok, "detail": faiss_reason},
        "autonomy_mode": runtime.autonomy_mode.value,
        "auto_create_pull_requests": settings.auto_create_pull_requests,
        "app_secret_key_configured": settings.app_secret_key is not None,
        "credentials_encrypted": encryption_enabled(settings),
        "workers": {"running": getattr(request.app.state, "worker_pool", None) is not None},
    }
