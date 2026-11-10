"""Evaluation results.

Evaluations run in a separate process (``python -m app.evaluation.evaluator``) against an
isolated temporary database, so they never mix with real runs. This API lists their stored
results and can start a scripted evaluation in the background.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import BACKEND_ROOT, Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import EvaluationRun
from app.dependencies import Pagination, admin, current_principal, db_dep, settings_dep
from app.schemas.common import Page
from app.security.authorization import Principal
from app.services import audit_service

router = APIRouter(prefix="/evaluations", tags=["evaluations"])

_running: dict[str, subprocess.Popen[bytes]] = {}


def _out(row: EvaluationRun, include_results: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {"id": row.id, "dataset_version": row.dataset_version, "model_name": row.model_name,
                            "llm_mode": row.llm_mode, "config_version": row.config_version, "status": row.status,
                            "metrics": row.metrics_json, "started_at": row.started_at,
                            "completed_at": row.completed_at}
    if include_results:
        data["results"] = row.results_json
    return data


@router.get("", response_model=Page[dict[str, Any]])
def list_evaluations(page: Pagination = Depends(), session: Session = Depends(db_dep),
                     _: Principal = Depends(current_principal)) -> Page[dict[str, Any]]:
    total = session.scalar(select(func.count(EvaluationRun.id))) or 0
    rows = session.scalars(select(EvaluationRun).order_by(EvaluationRun.id.desc()).limit(page.limit)
                           .offset(page.offset))
    return Page(items=[_out(r) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/{evaluation_id}")
def get_evaluation(evaluation_id: int, session: Session = Depends(db_dep),
                   _: Principal = Depends(current_principal)) -> dict[str, Any]:
    row = session.get(EvaluationRun, evaluation_id)
    if row is None:
        raise NotFoundError(f"Evaluation {evaluation_id} not found")
    return _out(row, include_results=True)


@router.post("", status_code=202)
def start_evaluation(session: Session = Depends(db_dep), settings: Settings = Depends(settings_dep),
                     principal: Principal = Depends(admin)) -> dict[str, Any]:
    """Start the scripted (deterministic) pipeline evaluation in a background process."""
    current = _running.get("scripted")
    if current is not None and current.poll() is None:
        raise ConflictError("An evaluation is already running.")
    _running["scripted"] = subprocess.Popen(
        [sys.executable, "-m", "app.evaluation.evaluator", "--llm", "scripted", "--sandbox", "auto"],
        cwd=str(BACKEND_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    audit_service.record(session, actor_type="user", actor_id=principal.external_id, action="evaluation.started",
                         resource_type="evaluation", resource_id=None, details={"llm": "scripted"})
    return {"started": True}
