"""ORM -> API schema conversion (keeps route handlers thin)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentStep,
    AnalysisRun,
    Approval,
    GitHubIssue,
    ProposedPatch,
    PullRequest,
    Repository,
    RepositoryMapping,
    RunEvent,
    SentryIssue,
    TestExecution,
    User,
)
from app.schemas.approvals import ApprovalOut
from app.schemas.issues import SentryIssueOut
from app.schemas.pull_requests import PullRequestOut
from app.schemas.repositories import MappingOut, RepositoryOut
from app.schemas.runs import EventOut, PatchOut, RunDetail, RunOut, StepOut, TestExecutionOut


def issue_out(session: Session, issue: SentryIssue) -> SentryIssueOut:
    latest = session.scalar(select(AnalysisRun).where(AnalysisRun.sentry_issue_id == issue.id)
                            .order_by(AnalysisRun.id.desc()).limit(1))
    return SentryIssueOut(
        id=issue.id, sentry_issue_id=issue.sentry_issue_id, short_id=issue.short_id,
        organization=issue.organization, project=issue.project, title=issue.title, culprit=issue.culprit,
        exception_type=issue.exception_type, level=issue.level, status=issue.status, permalink=issue.permalink,
        first_seen=issue.first_seen, last_seen=issue.last_seen, event_count=issue.event_count,
        latest_event_id=issue.latest_event_id, release=issue.release, repository_id=issue.repository_id,
        repository_full_name=issue.repository.full_name if issue.repository else None,
        latest_run_id=latest.id if latest else None, latest_run_status=latest.status if latest else None,
        is_demo=issue.is_demo,
    )


def mapping_out(m: RepositoryMapping) -> MappingOut:
    return MappingOut(id=m.id, sentry_organization=m.sentry_organization, sentry_project=m.sentry_project,
                      repository_id=m.repository_id, branch=m.branch, path_prefixes=list(m.path_prefixes_json),
                      strip_prefixes=list(m.strip_prefixes_json))


def repository_out(repo: Repository) -> RepositoryOut:
    return RepositoryOut(
        id=repo.id, provider=repo.provider, provider_repository_id=repo.provider_repository_id, owner=repo.owner,
        name=repo.name, full_name=repo.full_name, default_branch=repo.default_branch,
        installation_id=repo.installation_id, private=repo.private, enabled=repo.enabled, is_demo=repo.is_demo,
        test_commands=repo.test_commands_json, last_synced_at=repo.last_synced_at, created_at=repo.created_at,
        mappings=[mapping_out(m) for m in repo.mappings],
    )


def run_out(run: AnalysisRun) -> RunOut:
    return RunOut(
        id=run.id, sentry_issue_id=run.sentry_issue_id,
        sentry_issue_title=run.sentry_issue.title if run.sentry_issue else None,
        sentry_short_id=run.sentry_issue.short_id if run.sentry_issue else None,
        repository_id=run.repository_id,
        repository_full_name=run.repository.full_name if run.repository else None,
        status=run.status, current_step=run.current_step, outcome=run.outcome, autonomy_mode=run.autonomy_mode,
        trigger=run.trigger, root_cause_summary=run.root_cause_summary, diagnosis_status=run.diagnosis_status,
        confidence_score=run.confidence_score, model_name=run.model_name, llm_simulated=run.llm_simulated,
        source_revision=run.source_revision, source_revision_kind=run.source_revision_kind,
        started_at=run.started_at, completed_at=run.completed_at, error_message=run.error_message,
        retry_count=run.retry_count, repair_attempt=run.repair_attempt, total_duration_ms=run.total_duration_ms,
        prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens,
        estimated_cost=run.estimated_cost, cancel_requested=run.cancel_requested,
        retry_of_run_id=run.retry_of_run_id, is_demo=run.is_demo, created_at=run.created_at,
        updated_at=run.updated_at,
    )


def step_out(step: AgentStep) -> StepOut:
    return StepOut(id=step.id, sequence_number=step.sequence_number, step_type=step.step_type, state=step.state,
                   status=step.status, input_summary=step.input_summary, output_summary=step.output_summary,
                   duration_ms=step.duration_ms, retry_count=step.retry_count, error_message=step.error_message,
                   metadata=step.metadata_json, started_at=step.started_at, completed_at=step.completed_at)


def patch_out(p: ProposedPatch) -> PatchOut:
    return PatchOut(id=p.id, run_id=p.run_id, attempt_number=p.attempt_number, base_commit_sha=p.base_commit_sha,
                    branch_name=p.branch_name, patch_content=p.patch_content, diff_summary=p.diff_summary,
                    plan=p.plan_json, files_changed=p.files_changed_json, lines_added=p.lines_added,
                    lines_removed=p.lines_removed, validation_status=p.validation_status,
                    policy_status=p.policy_status, policy_findings=p.policy_findings_json, risk_level=p.risk_level,
                    superseded=p.superseded, created_at=p.created_at)


def test_out(t: TestExecution) -> TestExecutionOut:
    return TestExecutionOut(id=t.id, patch_id=t.patch_id, phase=t.phase, backend=t.backend, status=t.status,
                            command_summary=t.command_summary, exit_code=t.exit_code, tests_passed=t.tests_passed,
                            tests_failed=t.tests_failed, stdout_summary=t.stdout_summary,
                            stderr_summary=t.stderr_summary, duration_ms=t.duration_ms, timed_out=t.timed_out,
                            truncated_output=t.truncated_output, simulated=t.simulated, started_at=t.started_at,
                            completed_at=t.completed_at)


def event_out(e: RunEvent) -> EventOut:
    return EventOut(id=e.id, event_type=e.event_type, message=e.message, data=e.data_json, created_at=e.created_at)


def pr_out(pr: PullRequest, session: Session) -> PullRequestOut:
    repo = session.get(Repository, pr.repository_id)
    run = session.get(AnalysisRun, pr.run_id)
    issue = session.get(SentryIssue, run.sentry_issue_id) if run else None
    return PullRequestOut(
        id=pr.id, run_id=pr.run_id, repository_id=pr.repository_id,
        repository_full_name=repo.full_name if repo else None, external_pr_number=pr.external_pr_number,
        title=pr.title, branch_name=pr.branch_name, base_branch=pr.base_branch, head_commit_sha=pr.head_commit_sha,
        url=pr.url, status=pr.status, draft=pr.draft, validation_outcome=pr.validation_outcome,
        sentry_issue_title=issue.title if issue else None, sentry_issue_id=issue.id if issue else None,
        is_demo=pr.is_demo, created_at=pr.created_at, updated_at=pr.updated_at,
    )


def approval_out(a: Approval, session: Session) -> ApprovalOut:
    run = session.get(AnalysisRun, a.run_id)
    reviewer = session.get(User, a.reviewer_id) if a.reviewer_id else None
    patch = session.get(ProposedPatch, a.patch_id)
    return ApprovalOut(
        id=a.id, run_id=a.run_id, patch_id=a.patch_id, approval_type=a.approval_type, status=a.status,
        reviewer_id=a.reviewer_id, reviewer_name=reviewer.display_name if reviewer else None, reason=a.reason,
        risk_summary=a.risk_summary_json, is_demo=a.is_demo, created_at=a.created_at, resolved_at=a.resolved_at,
        run_status=run.status if run else None,
        issue_title=run.sentry_issue.title if run and run.sentry_issue else None,
        repository_full_name=run.repository.full_name if run and run.repository else None,
        files_changed=list(patch.files_changed_json) if patch else [],
    )


def github_issue_dict(g: GitHubIssue) -> dict[str, Any]:
    return {"id": g.id, "number": g.external_issue_number, "title": g.title, "url": g.url, "status": g.status,
            "run_id": g.run_id, "is_demo": g.is_demo, "created_at": g.created_at}


def run_detail(session: Session, run: AnalysisRun) -> RunDetail:
    state: dict[str, Any] = run.state_json or {}
    investigation = state.get("investigation") or {}
    patches = session.scalars(select(ProposedPatch).where(ProposedPatch.run_id == run.id)
                              .order_by(ProposedPatch.attempt_number)).all()
    tests = session.scalars(select(TestExecution).where(TestExecution.run_id == run.id)
                            .order_by(TestExecution.id)).all()
    approvals = session.scalars(select(Approval).where(Approval.run_id == run.id)).all()
    pr = session.scalar(select(PullRequest).where(PullRequest.run_id == run.id))
    gh = session.scalars(select(GitHubIssue).where(GitHubIssue.sentry_issue_id == run.sentry_issue_id)).all()
    base = run_out(run).model_dump()
    return RunDetail(
        **base,
        steps=[step_out(s) for s in run.steps],
        patches=[patch_out(p) for p in patches],
        tests=[test_out(t) for t in tests],
        approvals=[approval_out(a, session).model_dump() for a in approvals],
        pull_request=pr_out(pr, session).model_dump() if pr else None,
        github_issues=[github_issue_dict(g) for g in gh],
        diagnosis=state.get("diagnosis"),
        plan=state.get("plan"),
        evidence=investigation.get("evidence", []),
        frames=investigation.get("frames", []),
        investigation_notes=investigation.get("notes", []),
        validation=state.get("validation"),
        notes=state.get("notes", []),
        revision_note=state.get("revision_note"),
        final_report=state.get("final_report"),
        abstain_reasons=state.get("abstain_reasons", []),
        demo_scenario=state.get("demo_scenario"),
    )
