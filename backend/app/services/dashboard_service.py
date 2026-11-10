"""Dashboard aggregates computed from real rows (demo rows are counted separately)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import ApprovalStatus, RunOutcome, RunState, ValidationOutcome
from app.db.models import AnalysisRun, Approval, AuditLog, ProposedPatch, PullRequest, SentryIssue


def _count(session: Session, query: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)


def summary(session: Session, *, demo: bool) -> dict[str, Any]:
    runs = select(AnalysisRun).where(AnalysisRun.is_demo.is_(demo))
    patches = select(ProposedPatch).join(AnalysisRun).where(AnalysisRun.is_demo.is_(demo))
    by_outcome = dict(session.execute(
        select(AnalysisRun.outcome, func.count()).where(AnalysisRun.is_demo.is_(demo))
        .group_by(AnalysisRun.outcome)).all())
    return {
        "demo": demo,
        "issues_ingested": _count(session, select(SentryIssue).where(SentryIssue.is_demo.is_(demo))),
        "issues_investigated": int(session.scalar(
            select(func.count(func.distinct(AnalysisRun.sentry_issue_id))).where(AnalysisRun.is_demo.is_(demo))
        ) or 0),
        "runs_total": _count(session, runs),
        "runs_active": _count(session, runs.where(AnalysisRun.status.notin_(
            [RunState.COMPLETED.value, RunState.FAILED.value, RunState.CANCELLED.value,
             RunState.NEEDS_HUMAN_REVIEW.value, RunState.WAITING_FOR_APPROVAL.value]))),
        "runs_failed": _count(session, runs.where(AnalysisRun.status == RunState.FAILED.value)),
        "runs_needing_review": _count(session, runs.where(AnalysisRun.status == RunState.NEEDS_HUMAN_REVIEW.value)),
        "successful_validations": _count(session, patches.where(
            ProposedPatch.validation_status == ValidationOutcome.PASSED.value)),
        "awaiting_approval": _count(session, select(Approval).where(Approval.is_demo.is_(demo),
                                                                    Approval.status == ApprovalStatus.PENDING.value)),
        "open_pull_requests": _count(session, select(PullRequest).where(PullRequest.is_demo.is_(demo),
                                                                        PullRequest.status == "open")),
        "abstentions": sum(by_outcome.get(o.value, 0) for o in (RunOutcome.ABSTAINED_EXTERNAL_CAUSE,
                                                                 RunOutcome.ABSTAINED_INSUFFICIENT_EVIDENCE)),
        "outcomes": {k or "in_progress": v for k, v in by_outcome.items()},
    }


def recent_activity(session: Session, limit: int = 20) -> list[dict[str, Any]]:
    rows = session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
    return [{"id": r.id, "action": r.action, "actor": f"{r.actor_type}:{r.actor_id}", "outcome": r.outcome,
             "resource": f"{r.resource_type}:{r.resource_id}", "created_at": r.created_at,
             "details": r.details_json} for r in rows]
