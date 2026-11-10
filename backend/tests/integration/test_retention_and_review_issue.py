"""Run retention and the optional 'GitHub issue on human review' workflow setting."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.agent.graph import WorkflowEngine
from app.config import AutonomyMode
from app.core.enums import RunState
from app.db.base import utcnow
from app.db.models import AnalysisRun, GitHubIssue, PullRequest, RunEvent
from app.db.session import session_scope
from app.providers.factory import ProviderFactory
from app.services import demo_service, run_service
from app.services.settings_service import update_runtime_settings


def test_run_retention_keeps_recent_active_and_published_runs(settings):
    with session_scope() as s:
        old_done = demo_service.start_demo_run(s, "zero-division", actor="t", mode=AutonomyMode.ANALYZE_ONLY)[0]
        old_done.status, old_done.completed_at = RunState.COMPLETED.value, utcnow() - timedelta(days=400)
        old_pr = demo_service.start_demo_run(s, "missing-profile", actor="t", mode=AutonomyMode.ANALYZE_ONLY)[0]
        old_pr.status, old_pr.completed_at = RunState.COMPLETED.value, utcnow() - timedelta(days=400)
        s.add(PullRequest(run_id=old_pr.id, repository_id=old_pr.repository_id, external_pr_number=1,
                          external_pr_id="1", title="t", branch_name="patchora/x", base_branch="main",
                          url="demo://x", is_demo=True))
        active = demo_service.start_demo_run(s, "payment-timeout", actor="t", mode=AutonomyMode.ANALYZE_ONLY)[0]
        ids = (old_done.id, old_pr.id, active.id)
    with session_scope() as s:
        assert run_service.apply_run_retention(s, 180)["runs"] == 1
    with session_scope() as s:
        assert s.get(AnalysisRun, ids[0]) is None
        assert s.scalars(select(RunEvent).where(RunEvent.run_id == ids[0])).all() == []
        assert s.get(AnalysisRun, ids[1]) is not None and s.get(AnalysisRun, ids[2]) is not None


async def test_issue_created_when_run_needs_human_review(settings):
    with session_scope() as s:
        update_runtime_settings(s, {"create_issue_on_human_review": True}, "admin", settings)
        run_id = demo_service.start_demo_run(s, "payment-timeout", actor="t", mode=AutonomyMode.PREPARE_PATCH)[0].id
    assert await WorkflowEngine(ProviderFactory(settings)).execute(run_id) == RunState.NEEDS_HUMAN_REVIEW
    with session_scope() as s:
        issue = s.scalar(select(GitHubIssue))
        assert issue is not None and issue.run_id == run_id and issue.url.startswith("demo://")
