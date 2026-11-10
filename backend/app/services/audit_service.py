"""Append-only audit trail and persisted run events.

The application interface exposes only ``record`` and read queries; there is no update path.
Details are redacted and size-bounded before they are written.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger, redact
from app.db.base import utcnow
from app.db.models import AuditLog, RunEvent

logger = get_logger(__name__)

MAX_DETAILS_BYTES = 8_000


def _bounded(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    text = redact(json.dumps(details, default=str))
    if len(text) > MAX_DETAILS_BYTES:
        return {"truncated": True, "preview": text[:MAX_DETAILS_BYTES]}
    loaded: dict[str, Any] = json.loads(text)
    return loaded


def record(
    session: Session,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | int | None,
    outcome: str = "success",
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        outcome=outcome,
        details_json=_bounded(details),
    )
    session.add(entry)
    logger.info("audit", extra={"action": action, "resource": f"{resource_type}:{resource_id}",
                                "outcome": outcome, "actor": f"{actor_type}:{actor_id}"})
    return entry


def list_audit(
    session: Session,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    query = select(AuditLog)
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)
    if resource_id:
        query = query.where(AuditLog.resource_id == resource_id)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(query.order_by(AuditLog.id.desc()).limit(limit).offset(offset)).all()
    return list(rows), total


def apply_retention(session: Session, retention_days: int) -> int:
    """Delete audit rows older than the retention window (the only permitted removal)."""
    cutoff = utcnow() - timedelta(days=retention_days)
    result = session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)


# --- Run events (SSE source of truth) -----------------------------------------------------

def emit_event(
    session: Session, run_id: int, event_type: str, message: str, data: dict[str, Any] | None = None
) -> RunEvent:
    event = RunEvent(run_id=run_id, event_type=event_type, message=redact(message)[:1000],
                     data_json=_bounded(data))
    session.add(event)
    return event


def events_after(session: Session, run_id: int, after_id: int, limit: int = 200) -> list[RunEvent]:
    return list(
        session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.id > after_id)
            .order_by(RunEvent.id)
            .limit(limit)
        )
    )
