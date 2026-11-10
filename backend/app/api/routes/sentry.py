"""Sentry issues (ingested and live) and the Sentry webhook."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.serializers import github_issue_dict, issue_out, mapping_out, run_out
from app.config import AutonomyMode, Settings
from app.core.exceptions import AppError, AuthenticationRequired, NotFoundError, RateLimitedError
from app.db.models import AnalysisRun, GitHubIssue, Repository, SentryIssue
from app.demo.providers import DemoIssueSource
from app.dependencies import Pagination, current_principal, db_dep, factory_dep, reviewer, settings_dep
from app.providers.factory import ProviderFactory
from app.schemas.common import Page
from app.schemas.issues import (
    AnalyzeRequest,
    LinkRepositoryRequest,
    RemoteIssueOut,
    SentryIssueDetail,
    SentryIssueOut,
)
from app.schemas.runs import RunOut
from app.security.authorization import Principal
from app.security.input_sanitization import validate_sentry_issue_id
from app.security.webhook_validation import payload_hash, verify_sentry_signature
from app.services import audit_service, run_service, webhook_service
from app.services.credential_service import resolve_credential
from app.services.settings_service import get_runtime_settings

router = APIRouter(tags=["sentry"])

SORTS: dict[str, Any] = {"last_seen": SentryIssue.last_seen.desc(), "first_seen": SentryIssue.first_seen.desc(),
         "events": SentryIssue.event_count.desc(), "created": SentryIssue.id.desc()}


@router.get("/sentry/issues", response_model=Page[SentryIssueOut])
def list_issues(
    page: Pagination = Depends(),
    project: str | None = None,
    level: str | None = None,
    status: str | None = None,
    q: str | None = Query(None, max_length=200),
    demo: bool | None = None,
    sort: str = Query("last_seen", pattern="^(last_seen|first_seen|events|created)$"),
    session: Session = Depends(db_dep),
    _: Principal = Depends(current_principal),
) -> Page[SentryIssueOut]:
    query = select(SentryIssue)
    if project:
        query = query.where(SentryIssue.project == project)
    if level:
        query = query.where(SentryIssue.level == level)
    if status:
        query = query.where(SentryIssue.status == status)
    if demo is not None:
        query = query.where(SentryIssue.is_demo.is_(demo))
    if q:
        like = f"%{q}%"
        query = query.where(or_(SentryIssue.title.ilike(like), SentryIssue.culprit.ilike(like),
                                SentryIssue.short_id.ilike(like)))
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(query.order_by(SORTS[sort], SentryIssue.id.desc()).limit(page.limit).offset(page.offset))
    return Page(items=[issue_out(session, r) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/sentry/remote-issues")
async def list_remote_issues(
    project: str | None = None, query: str | None = Query(None, max_length=200), cursor: str | None = None,
    session: Session = Depends(db_dep), factory: ProviderFactory = Depends(factory_dep),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Live issue list from Sentry (requires configuration)."""
    project = project or resolve_credential(session, "sentry", "project", factory.settings).value
    if not project:
        raise NotFoundError("No Sentry project specified (set SENTRY_PROJECT or pass ?project=).")
    source = factory.sentry(session)
    try:
        issues, next_cursor = await source.list_issues(project, query=query, cursor=cursor)
    finally:
        await source.aclose()
    items = []
    for issue in issues:
        ingested = session.scalar(select(SentryIssue.id).where(SentryIssue.organization == issue.organization,
                                                               SentryIssue.sentry_issue_id == issue.sentry_issue_id))
        items.append(RemoteIssueOut(**issue.model_dump(exclude={"organization", "metadata"}), ingested_id=ingested))
    return {"items": [i.model_dump(mode="json") for i in items], "next_cursor": next_cursor}


@router.post("/sentry/issues/import", response_model=SentryIssueOut)
async def import_issue(sentry_issue_id: str = Query(..., max_length=64), session: Session = Depends(db_dep),
                       factory: ProviderFactory = Depends(factory_dep),
                       principal: Principal = Depends(reviewer)) -> SentryIssueOut:
    validate_sentry_issue_id(sentry_issue_id)
    source = factory.sentry(session)
    try:
        issue = await source.get_issue(sentry_issue_id)
        try:
            event = await source.get_latest_event(sentry_issue_id)
        except AppError:
            event = None
    finally:
        await source.aclose()
    row = run_service.upsert_issue(session, issue, event=event)
    audit_service.record(session, actor_type="user", actor_id=principal.external_id, action="sentry_issue.imported",
                         resource_type="sentry_issue", resource_id=row.id)
    return issue_out(session, row)


@router.get("/sentry/issues/{issue_id}", response_model=SentryIssueDetail)
async def get_issue(issue_id: int, live: bool = False, session: Session = Depends(db_dep),
                    factory: ProviderFactory = Depends(factory_dep),
                    _: Principal = Depends(current_principal)) -> SentryIssueDetail:
    issue = session.get(SentryIssue, issue_id)
    if issue is None:
        raise NotFoundError(f"Issue {issue_id} not found")
    runs = session.scalars(select(AnalysisRun).where(AnalysisRun.sentry_issue_id == issue.id)
                           .order_by(AnalysisRun.id.desc()).limit(20)).all()
    event: Any = None
    event_error: str | None = None
    latest_state = runs[0].state_json if runs else {}
    if issue.is_demo:
        event = await DemoIssueSource().get_latest_event(issue.sentry_issue_id)
    elif latest_state.get("event") and not live:
        event = latest_state["event"]
    else:
        try:
            source = factory.sentry(session)
            try:
                event = await source.get_latest_event(issue.sentry_issue_id)
            finally:
                await source.aclose()
        except AppError as exc:
            event_error = exc.message
    mapping = run_service.find_mapping(session, issue.organization, issue.project)
    gh = session.scalars(select(GitHubIssue).where(GitHubIssue.sentry_issue_id == issue.id)).all()
    base = issue_out(session, issue).model_dump()
    return SentryIssueDetail(**base, event=event, event_error=event_error,
                             runs=[run_out(r).model_dump(mode="json") for r in runs],
                             github_issues=[github_issue_dict(g) for g in gh],
                             mapping=mapping_out(mapping) if mapping else None)


@router.post("/sentry/issues/{issue_id}/analyze", response_model=RunOut, status_code=202)
def analyze_issue(issue_id: int, request: Request, body: AnalyzeRequest | None = None,
                  session: Session = Depends(db_dep), settings: Settings = Depends(settings_dep),
                  principal: Principal = Depends(reviewer)) -> RunOut:
    body = body or AnalyzeRequest()
    issue = session.get(SentryIssue, issue_id)
    if issue is None:
        raise NotFoundError(f"Issue {issue_id} not found")
    runtime = get_runtime_settings(session, settings)
    mode = AutonomyMode(body.autonomy_mode) if body.autonomy_mode else runtime.autonomy_mode
    if body.repository_id is not None:
        repo = session.get(Repository, body.repository_id)
        if repo is None:
            raise NotFoundError(f"Repository {body.repository_id} not found")
    extra = {"demo_scenario": _demo_scenario(issue)} if issue.is_demo else None
    run, _created = run_service.create_run(
        session, issue, trigger="manual", actor_type="user", actor_id=principal.external_id, autonomy_mode=mode,
        repository_id=body.repository_id, idempotency_key=body.idempotency_key, extra_state=extra)
    session.commit()
    if getattr(request.app.state, "worker_pool", None):
        request.app.state.worker_pool.wake()
    session.refresh(run)
    return run_out(run)


def _demo_scenario(issue: SentryIssue) -> str | None:
    from app.demo.providers import scenario_for_issue

    scenario = scenario_for_issue(issue.sentry_issue_id)
    return scenario.key if scenario else None


@router.post("/sentry/issues/{issue_id}/repository", response_model=SentryIssueOut)
def link_repository(issue_id: int, body: LinkRepositoryRequest, session: Session = Depends(db_dep),
                    principal: Principal = Depends(reviewer)) -> SentryIssueOut:
    issue = session.get(SentryIssue, issue_id)
    if issue is None:
        raise NotFoundError(f"Issue {issue_id} not found")
    if body.repository_id is not None:
        repo = session.get(Repository, body.repository_id)
        if repo is None:
            raise NotFoundError(f"Repository {body.repository_id} not found")
        if repo.is_demo != issue.is_demo:
            raise NotFoundError("Demo and real data cannot be linked to each other.")
    issue.repository_id = body.repository_id
    audit_service.record(session, actor_type="user", actor_id=principal.external_id,
                         action="sentry_issue.repository_linked" if body.repository_id else
                         "sentry_issue.repository_unlinked", resource_type="sentry_issue", resource_id=issue.id,
                         details={"repository_id": body.repository_id})
    session.flush()
    session.refresh(issue)
    return issue_out(session, issue)


# --- Webhook ---------------------------------------------------------------------------------

@router.post("/webhooks/sentry", status_code=202, tags=["webhooks"])
async def sentry_webhook(
    request: Request,
    sentry_hook_resource: str | None = Header(None),
    sentry_hook_signature: str | None = Header(None),
    request_id: str | None = Header(None),
    session: Session = Depends(db_dep),
    settings: Settings = Depends(settings_dep),
) -> JSONResponse:
    limiter = request.app.state.webhook_limiter
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(f"sentry:{client}"):
        raise RateLimitedError("Too many webhook requests.")
    body = await request.body()
    if len(body) > 1_000_000:
        raise AuthenticationRequired("Payload too large.")
    secret = resolve_credential(session, "sentry", "webhook_secret", settings).value
    if not secret:
        return JSONResponse(status_code=503, content={"error": {
            "code": "not_configured", "message": "SENTRY_WEBHOOK_SECRET is not configured; webhooks are disabled.",
            "details": {}, "request_id": getattr(request.state, "request_id", "")}})
    if not verify_sentry_signature(secret, body, sentry_hook_signature):
        audit_service.record(session, actor_type="webhook", actor_id=f"sentry:{client}",
                             action="webhook.rejected", resource_type="webhook", resource_id="sentry",
                             outcome="failure", details={"reason": "invalid signature"})
        session.commit()
        raise AuthenticationRequired("Invalid webhook signature.")
    resource = (sentry_hook_resource or "").strip()
    payload = webhook_service.parse_json_body(body)
    webhook_service.validate_sentry_payload(resource, payload)
    body_hash = payload_hash(body)
    delivery_id = (request_id or "").strip()[:128] or f"sha256:{body_hash}"
    delivery, is_new = webhook_service.record_delivery(session, provider="sentry", delivery_id=delivery_id,
                                                       event_type=resource, body_hash=body_hash, payload=payload)
    session.commit()
    if is_new and getattr(request.app.state, "worker_pool", None):
        request.app.state.worker_pool.wake()
    return JSONResponse(status_code=202, content={"accepted": True, "duplicate": not is_new,
                                                  "delivery_id": delivery.delivery_id})
