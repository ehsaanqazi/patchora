"""GitHub issue creation for a run (deduplicated by a stable Sentry-derived marker)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.core.exceptions import NotFoundError
from app.db.models import AnalysisRun
from app.db.session import session_scope
from app.dependencies import factory_dep, reviewer
from app.providers.factory import ProviderFactory
from app.security.authorization import Principal
from app.services import publication_service

router = APIRouter(tags=["github"])


@router.post("/runs/{run_id}/github-issue")
async def create_github_issue(run_id: int, factory: ProviderFactory = Depends(factory_dep),
                              principal: Principal = Depends(reviewer)) -> dict[str, Any]:
    with session_scope() as session:
        if session.get(AnalysisRun, run_id) is None:
            raise NotFoundError(f"Run {run_id} not found")
    return await publication_service.create_github_issue(factory, run_id, principal.external_id)
