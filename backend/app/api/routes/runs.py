"""Analysis runs, patches and on-demand policy validation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.serializers import patch_out, run_detail, run_out
from app.config import AutonomyMode, Settings
from app.core.exceptions import InvalidRequestError, NotFoundError
from app.db.models import AnalysisRun, ProposedPatch, SentryIssue
from app.dependencies import Pagination, current_principal, db_dep, reviewer, settings_dep
from app.schemas.common import Page
from app.schemas.runs import PatchOut, PolicyReportOut, RunCreate, RunDetail, RunOut
from app.security.authorization import Principal
from app.services import demo_service, run_service
from app.services.credential_service import all_secret_values
from app.services.patch_service import PatchPolicy, assess_risk, parse_unified_diff, validate_patch
from app.services.settings_service import get_runtime_settings

router = APIRouter(prefix="/runs", tags=["runs"])


def get_run(session: Session, run_id: int) -> AnalysisRun:
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise NotFoundError(f"Run {run_id} not found")
    return run


def _wake(request: Request) -> None:
    pool = getattr(request.app.state, "worker_pool", None)
    if pool is not None:
        pool.wake()


@router.post("", response_model=RunOut, status_code=202)
def create_run(body: RunCreate, request: Request, session: Session = Depends(db_dep),
               settings: Settings = Depends(settings_dep), principal: Principal = Depends(reviewer)) -> RunOut:
    runtime = get_runtime_settings(session, settings)
    try:
        mode = AutonomyMode(body.autonomy_mode) if body.autonomy_mode else runtime.autonomy_mode
    except ValueError as exc:
        raise InvalidRequestError(f"Unknown autonomy mode '{body.autonomy_mode}'.") from exc
    if body.demo_scenario:
        run, _ = demo_service.start_demo_run(session, body.demo_scenario, actor=principal.external_id, mode=mode,
                                             use_real_llm=body.use_real_llm)
    else:
        if body.sentry_issue_id is None:
            raise InvalidRequestError("Provide sentry_issue_id or demo_scenario.")
        issue = session.get(SentryIssue, body.sentry_issue_id)
        if issue is None:
            raise NotFoundError(f"Issue {body.sentry_issue_id} not found")
        run, _ = run_service.create_run(session, issue, trigger="manual", actor_type="user",
                                        actor_id=principal.external_id, autonomy_mode=mode,
                                        repository_id=body.repository_id, idempotency_key=body.idempotency_key)
    session.commit()
    _wake(request)
    session.refresh(run)
    return run_out(run)


@router.get("", response_model=Page[RunOut])
def list_runs(page: Pagination = Depends(), status: str | None = None, outcome: str | None = None,
              demo: bool | None = None, issue_id: int | None = None,
              session: Session = Depends(db_dep), _: Principal = Depends(current_principal)) -> Page[RunOut]:
    query = select(AnalysisRun)
    if status:
        query = query.where(AnalysisRun.status == status)
    if outcome:
        query = query.where(AnalysisRun.outcome == outcome)
    if demo is not None:
        query = query.where(AnalysisRun.is_demo.is_(demo))
    if issue_id is not None:
        query = query.where(AnalysisRun.sentry_issue_id == issue_id)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(query.order_by(AnalysisRun.id.desc()).limit(page.limit).offset(page.offset))
    return Page(items=[run_out(r) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/{run_id}", response_model=RunDetail)
def get_run_detail(run_id: int, session: Session = Depends(db_dep),
                   _: Principal = Depends(current_principal)) -> RunDetail:
    return run_detail(session, get_run(session, run_id))


@router.post("/{run_id}/retry", response_model=RunOut, status_code=202)
def retry(run_id: int, request: Request, session: Session = Depends(db_dep),
          principal: Principal = Depends(reviewer)) -> RunOut:
    new_run = run_service.retry_run(session, get_run(session, run_id), actor_id=principal.external_id)
    session.commit()
    _wake(request)
    session.refresh(new_run)
    return run_out(new_run)


@router.post("/{run_id}/cancel", response_model=RunOut)
def cancel(run_id: int, session: Session = Depends(db_dep), principal: Principal = Depends(reviewer)) -> RunOut:
    run = run_service.cancel_run(session, get_run(session, run_id), actor_id=principal.external_id)
    session.flush()
    session.refresh(run)
    return run_out(run)


def _latest_patch(session: Session, run_id: int) -> ProposedPatch:
    patch = session.scalar(select(ProposedPatch).where(ProposedPatch.run_id == run_id)
                           .order_by(ProposedPatch.attempt_number.desc()).limit(1))
    if patch is None:
        raise NotFoundError(f"Run {run_id} has no patch")
    return patch


@router.get("/{run_id}/patch", response_model=PatchOut)
def get_patch(run_id: int, session: Session = Depends(db_dep), _: Principal = Depends(current_principal)) -> PatchOut:
    get_run(session, run_id)
    return patch_out(_latest_patch(session, run_id))


@router.get("/{run_id}/diff", response_class=PlainTextResponse)
def get_diff(run_id: int, attempt: int | None = Query(None, ge=0), session: Session = Depends(db_dep),
             _: Principal = Depends(current_principal)) -> PlainTextResponse:
    get_run(session, run_id)
    patch: ProposedPatch | None
    if attempt is None:
        patch = _latest_patch(session, run_id)
    else:
        patch = session.scalar(select(ProposedPatch).where(ProposedPatch.run_id == run_id,
                                                           ProposedPatch.attempt_number == attempt))
    if patch is None:
        raise NotFoundError("Patch attempt not found")
    return PlainTextResponse(patch.patch_content, media_type="text/x-diff")


@router.post("/{run_id}/validate", response_model=PolicyReportOut)
def revalidate(run_id: int, session: Session = Depends(db_dep), settings: Settings = Depends(settings_dep),
               _: Principal = Depends(current_principal)) -> PolicyReportOut:
    """Re-run static policy validation of the latest patch against the current settings.

    This never executes code; test execution only happens inside the workflow sandbox.
    """
    run = get_run(session, run_id)
    patch = _latest_patch(session, run_id)
    runtime = get_runtime_settings(session, settings)
    files = []
    try:
        files = [fp.path for fp in parse_unified_diff(patch.patch_content)]
    except Exception:  # malformed patches are reported by validate_patch below
        files = []
    # The repository tree is not refetched here (existence/symlink checks ran in the workflow);
    # path, protected-file, size, mode and secret checks use the current settings.
    policy = PatchPolicy(max_files=runtime.max_patch_files, max_lines=runtime.max_patch_lines,
                         protected_paths=runtime.protected_paths, known_secrets=all_secret_values(session, settings))
    report = validate_patch(patch.patch_content, policy)
    risk = assess_risk(report, (run.state_json.get("plan") or {}).get("risk_level"))
    return PolicyReportOut(blocked=report.blocked, findings=[f.as_dict() for f in report.findings],
                           files=files, lines_added=report.lines_added, lines_removed=report.lines_removed,
                           risk_level=risk)
