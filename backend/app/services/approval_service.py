"""Human approval of patch publication.

Only authenticated human users with the reviewer or admin role can resolve approvals.
The agent has no code path that approves anything. Every decision is audited.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.transitions import validate_transition
from app.core.enums import ApprovalStatus, JobKind, RunOutcome, RunState, UserRole
from app.core.exceptions import ConflictError, NotFoundError
from app.db.base import utcnow
from app.db.models import AnalysisRun, Approval
from app.security.authorization import Principal, require_role
from app.services import audit_service
from app.workers import job_runner


def list_approvals(session: Session, *, status: str | None, limit: int, offset: int) -> tuple[list[Approval], int]:
    query = select(Approval)
    if status:
        query = query.where(Approval.status == status)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(query.order_by(Approval.id.desc()).limit(limit).offset(offset)).all()
    return list(rows), total


def _load(session: Session, approval_id: int) -> tuple[Approval, AnalysisRun]:
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError(f"Approval {approval_id} not found")
    run = session.get(AnalysisRun, approval.run_id)
    if run is None:
        raise NotFoundError("The approval's run no longer exists")
    if approval.status != ApprovalStatus.PENDING.value:
        raise ConflictError(f"Approval {approval_id} is already {approval.status}.")
    if run.status != RunState.WAITING_FOR_APPROVAL.value:
        raise ConflictError(f"Run {run.id} is not waiting for approval (status {run.status}).")
    return approval, run


def approve(session: Session, approval_id: int, principal: Principal, reason: str | None) -> Approval:
    require_role(principal, UserRole.REVIEWER)
    approval, run = _load(session, approval_id)
    validate_transition(run.status, RunState.READY_TO_PUBLISH)
    approval.status = ApprovalStatus.APPROVED.value
    approval.reviewer_id = principal.user_id
    approval.reason = (reason or "")[:2000] or None
    approval.resolved_at = utcnow()
    run.status = RunState.READY_TO_PUBLISH.value
    run.current_step = "recheck_policy_before_publication"
    run.outcome = None
    job_runner.enqueue(session, JobKind.RUN_WORKFLOW, {"run_id": run.id}, max_attempts=4)
    audit_service.record(session, actor_type="user", actor_id=principal.external_id, action="approval.approved",
                         resource_type="approval", resource_id=approval.id,
                         details={"run_id": run.id, "patch_id": approval.patch_id, "reason": approval.reason})
    audit_service.emit_event(session, run.id, "approval_granted",
                             f"Publication approved by {principal.display_name}", {"approval_id": approval.id})
    return approval


def reject(session: Session, approval_id: int, principal: Principal, reason: str | None) -> Approval:
    require_role(principal, UserRole.REVIEWER)
    approval, run = _load(session, approval_id)
    validate_transition(run.status, RunState.CANCELLED)
    approval.status = ApprovalStatus.REJECTED.value
    approval.reviewer_id = principal.user_id
    approval.reason = (reason or "")[:2000] or None
    approval.resolved_at = utcnow()
    run.status = RunState.CANCELLED.value
    run.outcome = RunOutcome.REJECTED.value
    run.current_step = "rejected"
    run.completed_at = utcnow()
    audit_service.record(session, actor_type="user", actor_id=principal.external_id, action="approval.rejected",
                         resource_type="approval", resource_id=approval.id,
                         details={"run_id": run.id, "patch_id": approval.patch_id, "reason": approval.reason})
    audit_service.emit_event(session, run.id, "approval_rejected",
                             f"Patch rejected by {principal.display_name}", {"approval_id": approval.id})
    return approval
