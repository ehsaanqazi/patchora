"""Pull requests created by Patchora, publication requests and the GitHub webhook."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routes.runs import get_run
from app.api.serializers import pr_out
from app.config import Settings
from app.core.exceptions import AuthenticationRequired, NotFoundError, RateLimitedError
from app.db.models import PullRequest, Repository
from app.dependencies import Pagination, current_principal, db_dep, factory_dep, reviewer, settings_dep
from app.providers.factory import ProviderFactory
from app.schemas.common import Page
from app.schemas.pull_requests import PullRequestOut
from app.security.authorization import Principal
from app.security.webhook_validation import payload_hash, verify_github_signature
from app.services import audit_service, publication_service, webhook_service
from app.services.credential_service import resolve_credential

router = APIRouter(tags=["pull_requests"])


@router.post("/runs/{run_id}/pull-request")
def request_pull_request(run_id: int, request: Request, session: Session = Depends(db_dep),
                         principal: Principal = Depends(reviewer)) -> dict[str, Any]:
    result = publication_service.request_publication(session, get_run(session, run_id), principal.external_id)
    session.commit()
    pool = getattr(request.app.state, "worker_pool", None)
    if pool is not None:
        pool.wake()
    return result


@router.get("/pull-requests", response_model=Page[PullRequestOut])
def list_pull_requests(page: Pagination = Depends(), status: str | None = Query(None, max_length=20),
                       demo: bool | None = None, session: Session = Depends(db_dep),
                       _: Principal = Depends(current_principal)) -> Page[PullRequestOut]:
    query = select(PullRequest)
    if status:
        query = query.where(PullRequest.status == status)
    if demo is not None:
        query = query.where(PullRequest.is_demo.is_(demo))
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(query.order_by(PullRequest.id.desc()).limit(page.limit).offset(page.offset))
    return Page(items=[pr_out(p, session) for p in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/pull-requests/{pull_request_id}", response_model=PullRequestOut)
async def get_pull_request(pull_request_id: int, refresh: bool = False, session: Session = Depends(db_dep),
                           factory: ProviderFactory = Depends(factory_dep),
                           _: Principal = Depends(current_principal)) -> PullRequestOut:
    pr = session.get(PullRequest, pull_request_id)
    if pr is None:
        raise NotFoundError(f"Pull request {pull_request_id} not found")
    if refresh and not pr.is_demo:
        repo = session.get(Repository, pr.repository_id)
        assert repo is not None
        client = factory.github(session, repo)
        try:
            info = await client.get_pull_request(repo.full_name, pr.external_pr_number)
        finally:
            await client.aclose()
        pr.status = "merged" if info.merged else info.state
        pr.draft = info.draft
        session.flush()
    return pr_out(pr, session)


@router.post("/webhooks/github", status_code=202, tags=["webhooks"])
async def github_webhook(request: Request, x_github_event: str | None = Header(None),
                         x_github_delivery: str | None = Header(None),
                         x_hub_signature_256: str | None = Header(None),
                         session: Session = Depends(db_dep),
                         settings: Settings = Depends(settings_dep)) -> JSONResponse:
    client = request.client.host if request.client else "unknown"
    if not request.app.state.webhook_limiter.allow(f"github:{client}"):
        raise RateLimitedError("Too many webhook requests.")
    body = await request.body()
    secret = resolve_credential(session, "github", "webhook_secret", settings).value
    if not secret:
        return JSONResponse(status_code=503, content={"error": {
            "code": "not_configured", "message": "GITHUB_WEBHOOK_SECRET is not configured.", "details": {},
            "request_id": getattr(request.state, "request_id", "")}})
    if not verify_github_signature(secret, body, x_hub_signature_256):
        audit_service.record(session, actor_type="webhook", actor_id=f"github:{client}", action="webhook.rejected",
                             resource_type="webhook", resource_id="github", outcome="failure",
                             details={"reason": "invalid signature"})
        session.commit()
        raise AuthenticationRequired("Invalid webhook signature.")
    payload = webhook_service.parse_json_body(body)
    body_hash = payload_hash(body)
    delivery_id = (x_github_delivery or "").strip()[:128] or f"sha256:{body_hash}"
    _, is_new = webhook_service.record_delivery(session, provider="github", delivery_id=delivery_id,
                                                       event_type=(x_github_event or "unknown")[:60],
                                                       body_hash=body_hash, payload=payload)
    session.commit()
    if is_new and getattr(request.app.state, "worker_pool", None):
        request.app.state.worker_pool.wake()
    return JSONResponse(status_code=202, content={"accepted": True, "duplicate": not is_new})
