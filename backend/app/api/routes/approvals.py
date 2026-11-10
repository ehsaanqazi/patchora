"""Approval queue. Resolving an approval requires the reviewer role and explicit confirmation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.serializers import approval_out
from app.core.exceptions import InvalidRequestError
from app.dependencies import Pagination, current_principal, db_dep, reviewer
from app.schemas.approvals import ApprovalDecision, ApprovalOut
from app.schemas.common import Page
from app.security.authorization import Principal
from app.services import approval_service

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=Page[ApprovalOut])
def list_approvals(page: Pagination = Depends(), status: str | None = Query(None, max_length=20),
                   session: Session = Depends(db_dep), _: Principal = Depends(current_principal)) -> Page[ApprovalOut]:
    rows, total = approval_service.list_approvals(session, status=status, limit=page.limit, offset=page.offset)
    return Page(items=[approval_out(a, session) for a in rows], total=total, limit=page.limit, offset=page.offset)


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
def approve(approval_id: int, body: ApprovalDecision, request: Request, session: Session = Depends(db_dep),
            principal: Principal = Depends(reviewer)) -> ApprovalOut:
    if not body.confirm:
        raise InvalidRequestError("Approval requires explicit confirmation (confirm=true).")
    approval = approval_service.approve(session, approval_id, principal, body.reason)
    session.commit()
    pool = getattr(request.app.state, "worker_pool", None)
    if pool is not None:
        pool.wake()
    return approval_out(approval, session)


@router.post("/{approval_id}/reject", response_model=ApprovalOut)
def reject(approval_id: int, body: ApprovalDecision, session: Session = Depends(db_dep),
           principal: Principal = Depends(reviewer)) -> ApprovalOut:
    if not body.confirm:
        raise InvalidRequestError("Rejection requires explicit confirmation (confirm=true).")
    approval = approval_service.reject(session, approval_id, principal, body.reason)
    session.flush()
    return approval_out(approval, session)
