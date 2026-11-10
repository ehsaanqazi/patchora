"""Server-Sent Events for live run progress.

Events are read from the persisted ``run_events`` table, so a reconnecting client resumes
with ``Last-Event-ID`` (or ``?after=``) and receives everything it missed. The stream ends
with an ``end`` event once the run is finished or parked and no events remain.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.serializers import event_out
from app.core.enums import PARKED_STATES, RunState
from app.core.exceptions import NotFoundError
from app.db.models import AnalysisRun
from app.db.session import session_scope
from app.dependencies import current_principal
from app.security.authorization import Principal
from app.services.audit_service import events_after

router = APIRouter(tags=["runs"])

POLL_SECONDS = 1.0
KEEPALIVE_SECONDS = 15.0


def _fetch(run_id: int, after: int) -> tuple[list[dict[str, object]], str | None]:
    with session_scope() as session:
        run = session.get(AnalysisRun, run_id)
        if run is None:
            return [], None
        events = [event_out(e).model_dump(mode="json") for e in events_after(session, run_id, after)]
        return events, run.status


def _format(event: dict[str, object]) -> str:
    return f"id: {event['id']}\nevent: {event['event_type']}\ndata: {json.dumps(event)}\n\n"


@router.get("/runs/{run_id}/events")
async def stream_events(run_id: int, request: Request, after: int = Query(0, ge=0),
                        last_event_id: str | None = Header(None),
                        _: Principal = Depends(current_principal)) -> StreamingResponse:
    cursor = after
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    _initial, status = await run_in_threadpool(_fetch, run_id, 0)
    if status is None:
        raise NotFoundError(f"Run {run_id} not found")

    async def generator() -> AsyncIterator[str]:
        nonlocal cursor
        yield "retry: 3000\n\n"
        idle = 0.0
        while True:
            if await request.is_disconnected():
                return
            events, state = await run_in_threadpool(_fetch, run_id, cursor)
            for event in events:
                cursor = int(event["id"])  # type: ignore[call-overload]
                yield _format(event)
            if not events and state is not None and RunState(state) in PARKED_STATES:
                yield f"event: end\ndata: {json.dumps({'status': state})}\n\n"
                return
            await asyncio.sleep(POLL_SECONDS)
            idle = 0.0 if events else idle + POLL_SECONDS
            if idle >= KEEPALIVE_SECONDS:
                idle = 0.0
                yield ": keepalive\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
