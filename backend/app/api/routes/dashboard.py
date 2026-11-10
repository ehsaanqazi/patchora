"""Dashboard aggregates."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.dependencies import current_principal, db_dep
from app.security.authorization import Principal
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
def summary(demo: bool = Query(False, description="Summarize demo data instead of real data"),
            session: Session = Depends(db_dep), _: Principal = Depends(current_principal)) -> dict[str, Any]:
    return dashboard_service.summary(session, demo=demo)


@router.get("/activity")
def activity(limit: int = Query(20, ge=1, le=100), session: Session = Depends(db_dep),
             _: Principal = Depends(current_principal)) -> list[dict[str, Any]]:
    return dashboard_service.recent_activity(session, limit)
