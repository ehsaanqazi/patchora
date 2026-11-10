"""Demo-mode workflow through the real engine (scripted model, simulated sandbox)."""

from __future__ import annotations

from sqlalchemy import select

from app.agent.graph import WorkflowEngine
from app.config import AutonomyMode
from app.core.enums import ApprovalStatus, RunOutcome, RunState, UserRole
from app.db.models import (
    AgentStep,
    AnalysisRun,
    Approval,
    ProposedPatch,
    PullRequest,
    RunEvent,
    TestExecution,
)
from app.db.session import session_scope
from app.providers.factory import ProviderFactory
from app.security.authorization import Principal
from app.services import approval_service, demo_service


def _start(scenario: str, mode: AutonomyMode = AutonomyMode.PREPARE_PATCH) -> int:
    with session_scope() as s:
        run, created = demo_service.start_demo_run(s, scenario, actor="tester", mode=mode)
        assert created
        return run.id


def _reviewer(session) -> Principal:
    from app.dependencies import ensure_local_user

    user = ensure_local_user(session)
    return Principal(user_id=user.id, external_id=user.external_id, display_name="Tester", role=UserRole.REVIEWER)


async def test_zero_division_demo_waits_for_approval_then_publishes(settings):
    factory = ProviderFactory(settings)
    engine = WorkflowEngine(factory)
    run_id = _start("zero-division")
    final = await engine.execute(run_id)
    assert final == RunState.WAITING_FOR_APPROVAL

    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.outcome == RunOutcome.AWAITING_APPROVAL.value
        assert run.source_revision_kind == "release_commit"
        assert run.llm_simulated is True
        assert run.diagnosis_status == "supported_by_tests"
        patch = s.scalar(select(ProposedPatch).where(ProposedPatch.run_id == run_id))
        assert patch.policy_status == "passed"
        assert patch.validation_status == "passed"
        assert set(patch.files_changed_json) == {"shop/pricing.py", "tests/test_pricing.py"}
        assert "+    if not orders:" in patch.patch_content
        tests = s.scalars(select(TestExecution).where(TestExecution.run_id == run_id)).all()
        assert {t.phase for t in tests} >= {"baseline", "patched"}
        assert all(t.simulated for t in tests)
        steps = s.scalars(select(AgentStep).where(AgentStep.run_id == run_id)).all()
        assert [st.sequence_number for st in steps] == list(range(1, len(steps) + 1))
        approval = s.scalar(select(Approval).where(Approval.run_id == run_id))
        assert approval.status == ApprovalStatus.PENDING.value
        approval_service.approve(s, approval.id, _reviewer(s), "looks right")

    final = await engine.execute(run_id)
    assert final == RunState.COMPLETED
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.outcome == RunOutcome.PULL_REQUEST_CREATED.value
        pr = s.scalar(select(PullRequest).where(PullRequest.run_id == run_id))
        assert pr.url.startswith("demo://")
        assert pr.branch_name.startswith("patchora/issue-demo-1001/run-")
        assert pr.is_demo
        events = [e.event_type for e in s.scalars(select(RunEvent).where(RunEvent.run_id == run_id))]
        assert "approval_required" in events and "pull_request_created" in events
        assert run.state_json["final_report"]["simulated_components"] == ["model", "tests", "GitHub publication"]


async def test_external_cause_abstains(settings):
    engine = WorkflowEngine(ProviderFactory(settings))
    run_id = _start("payment-timeout")
    assert await engine.execute(run_id) == RunState.NEEDS_HUMAN_REVIEW
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.outcome == RunOutcome.ABSTAINED_EXTERNAL_CAUSE.value
        assert s.scalar(select(ProposedPatch).where(ProposedPatch.run_id == run_id)) is None


async def test_prompt_injection_is_blocked_by_policy(settings):
    engine = WorkflowEngine(ProviderFactory(settings))
    run_id = _start("prompt-injection")
    assert await engine.execute(run_id) == RunState.NEEDS_HUMAN_REVIEW
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.outcome == RunOutcome.POLICY_BLOCKED.value
        assert s.scalar(select(ProposedPatch).where(ProposedPatch.run_id == run_id)) is None


async def test_analyze_only_mode_never_executes(settings):
    engine = WorkflowEngine(ProviderFactory(settings))
    run_id = _start("missing-profile", AutonomyMode.ANALYZE_ONLY)
    assert await engine.execute(run_id) == RunState.COMPLETED
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.outcome == RunOutcome.ANALYSIS_ONLY.value
        assert s.scalars(select(TestExecution).where(TestExecution.run_id == run_id)).all() == []
        assert s.scalar(select(ProposedPatch).where(ProposedPatch.run_id == run_id)) is not None


async def test_rejection_cancels_run(settings):
    engine = WorkflowEngine(ProviderFactory(settings))
    run_id = _start("missing-profile")
    assert await engine.execute(run_id) == RunState.WAITING_FOR_APPROVAL
    with session_scope() as s:
        approval = s.scalar(select(Approval).where(Approval.run_id == run_id))
        approval_service.reject(s, approval.id, _reviewer(s), "not the right fix")
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.status == RunState.CANCELLED.value
        assert run.outcome == RunOutcome.REJECTED.value
