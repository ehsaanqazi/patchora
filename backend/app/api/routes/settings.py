"""Runtime settings, authentication, audit log and demo-mode controls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AutonomyMode, Settings
from app.core.exceptions import AuthenticationRequired, InvalidRequestError, NotConfiguredError
from app.core.security import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS, hash_api_token, sign_session
from app.db.models import User
from app.demo.scenarios import SCENARIOS
from app.dependencies import Pagination, admin, current_principal, db_dep, reviewer, settings_dep
from app.schemas.common import Page
from app.schemas.integrations import SettingsOut, SettingsUpdate
from app.security.authorization import Principal
from app.services import audit_service, demo_service
from app.services.settings_service import (
    RUNTIME_KEYS,
    SECURITY_SENSITIVE_KEYS,
    get_runtime_settings,
    update_runtime_settings,
)

router = APIRouter()


# --- Settings -----------------------------------------------------------------------------

def _settings_out(session: Session, settings: Settings) -> SettingsOut:
    runtime = get_runtime_settings(session, settings)
    defaults = {k: getattr(settings, k) for k in RUNTIME_KEYS}
    return SettingsOut(
        values=runtime.model_dump(mode="json"),
        defaults={k: (v.value if hasattr(v, "value") else v) for k, v in defaults.items()},
        security_sensitive=sorted(SECURITY_SENSITIVE_KEYS),
        environment={"app_env": settings.app_env, "auth_mode": settings.auth_mode,
                     "sandbox_enabled": settings.sandbox_enabled,
                     "auto_create_pull_requests_env": settings.auto_create_pull_requests,
                     "faiss_index_path": str(settings.faiss_index_path.name),
                     "embedding_model": settings.embedding_model},
    )


@router.get("/settings", response_model=SettingsOut, tags=["settings"])
def get_settings_route(session: Session = Depends(db_dep), settings: Settings = Depends(settings_dep),
                       _: Principal = Depends(current_principal)) -> SettingsOut:
    return _settings_out(session, settings)


@router.put("/settings", response_model=SettingsOut, tags=["settings"])
def update_settings(body: SettingsUpdate, session: Session = Depends(db_dep),
                    settings: Settings = Depends(settings_dep),
                    principal: Principal = Depends(admin)) -> SettingsOut:
    _, diff = update_runtime_settings(session, body.values, principal.external_id, settings)
    if diff:
        sensitive = sorted(set(diff) & SECURITY_SENSITIVE_KEYS)
        audit_service.record(session, actor_type="user", actor_id=principal.external_id,
                             action="settings.security_changed" if sensitive else "settings.changed",
                             resource_type="settings", resource_id=None,
                             details={k: {"from": old, "to": new} for k, (old, new) in diff.items()})
    session.flush()
    return _settings_out(session, settings)


# --- Authentication -----------------------------------------------------------------------

class LoginRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


@router.get("/auth/me", tags=["auth"])
def me(principal: Principal = Depends(current_principal), settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    return {"id": principal.user_id, "external_id": principal.external_id, "display_name": principal.display_name,
            "role": principal.role.value, "auth_mode": settings.auth_mode}


@router.post("/auth/login", tags=["auth"])
def login(body: LoginRequest, response: Response, session: Session = Depends(db_dep),
          settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    if settings.auth_mode != "token":
        raise InvalidRequestError("Login is only used when AUTH_MODE=token.")
    if settings.app_secret_key is None:
        raise NotConfiguredError("APP_SECRET_KEY must be set for session cookies.")
    user = session.scalar(select(User).where(User.api_token_hash == hash_api_token(body.token),
                                             User.active.is_(True)))
    if user is None:
        audit_service.record(session, actor_type="user", actor_id=None, action="auth.login_failed",
                             resource_type="user", resource_id=None, outcome="failure")
        session.commit()
        raise AuthenticationRequired("Invalid token.")
    response.set_cookie(SESSION_COOKIE, sign_session(settings.app_secret_key.get_secret_value(), user.id),
                        max_age=SESSION_MAX_AGE_SECONDS, httponly=True, samesite="strict",
                        secure=settings.app_env == "production", path="/")
    audit_service.record(session, actor_type="user", actor_id=user.external_id, action="auth.login",
                         resource_type="user", resource_id=user.id)
    return {"display_name": user.display_name, "role": user.role}


@router.post("/auth/logout", tags=["auth"])
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# --- Audit --------------------------------------------------------------------------------

@router.get("/audit", tags=["audit"])
def audit_log(page: Pagination = Depends(), resource_type: str | None = Query(None, max_length=40),
              resource_id: str | None = Query(None, max_length=64), session: Session = Depends(db_dep),
              _: Principal = Depends(current_principal)) -> Page[dict[str, Any]]:
    rows, total = audit_service.list_audit(session, resource_type=resource_type, resource_id=resource_id,
                                           limit=page.limit, offset=page.offset)
    items = [{"id": r.id, "actor_type": r.actor_type, "actor_id": r.actor_id, "action": r.action,
              "resource_type": r.resource_type, "resource_id": r.resource_id, "outcome": r.outcome,
              "details": r.details_json, "created_at": r.created_at} for r in rows]
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


# --- Demo ---------------------------------------------------------------------------------

@router.get("/demo/scenarios", tags=["demo"])
def demo_scenarios(_: Principal = Depends(current_principal)) -> list[dict[str, Any]]:
    return [{"key": s.key, "description": s.description, "issue_title": s.issue["title"],
             "short_id": s.issue["shortId"]} for s in SCENARIOS.values()]


@router.post("/demo/seed", tags=["demo"])
def seed_demo(session: Session = Depends(db_dep), principal: Principal = Depends(reviewer)) -> dict[str, Any]:
    result = demo_service.ensure_demo_data(session)
    audit_service.record(session, actor_type="user", actor_id=principal.external_id, action="demo.seeded",
                         resource_type="demo", resource_id=None)
    return result


class DemoRunRequest(BaseModel):
    scenario: str = Field(max_length=60)
    autonomy_mode: AutonomyMode = AutonomyMode.PREPARE_PATCH
    use_real_llm: bool = False


@router.post("/demo/runs", tags=["demo"], status_code=202)
def start_demo(body: DemoRunRequest, request: Request, session: Session = Depends(db_dep),
               principal: Principal = Depends(reviewer)) -> dict[str, Any]:
    run, created = demo_service.start_demo_run(session, body.scenario, actor=principal.external_id,
                                               mode=body.autonomy_mode, use_real_llm=body.use_real_llm)
    session.commit()
    pool = getattr(request.app.state, "worker_pool", None)
    if pool is not None:
        pool.wake()
    return {"run_id": run.id, "created": created}


@router.post("/demo/reset", tags=["demo"])
def reset_demo(session: Session = Depends(db_dep), principal: Principal = Depends(admin)) -> dict[str, int]:
    return demo_service.reset_demo_data(session, principal.external_id)
