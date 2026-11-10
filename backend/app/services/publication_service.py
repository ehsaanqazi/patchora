"""User-initiated GitHub issue creation and publication retries."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.agent import tools
from app.core.enums import ApprovalStatus, JobKind, RunState
from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.db.models import AnalysisRun, Approval, GitHubIssue, PullRequest, Repository, SentryIssue
from app.db.session import session_scope, with_write_retry
from app.providers.factory import ProviderFactory
from app.security.authorization import ensure_repository_allowed
from app.services import audit_service
from app.workers import job_runner


async def create_github_issue(factory: ProviderFactory, run_id: int, actor: str) -> dict[str, Any]:
    """Create (or find) the GitHub issue for a run's Sentry issue. Deduplicated by a stable marker."""
    with session_scope() as session:
        run = session.get(AnalysisRun, run_id)
        if run is None:
            raise NotFoundError(f"Run {run_id} not found")
        issue = session.get(SentryIssue, run.sentry_issue_id)
        repo = session.get(Repository, run.repository_id) if run.repository_id else None
        assert issue is not None
        ensure_repository_allowed(repo)
        assert repo is not None
        existing = session.scalar(select(GitHubIssue).where(GitHubIssue.sentry_issue_id == issue.id,
                                                            GitHubIssue.repository_id == repo.id))
        if existing is not None:
            return {"created": False, "url": existing.url, "number": existing.external_issue_number}
        deps = factory.for_run(session, run)
        state = dict(run.state_json or {})
        marker = tools.issue_marker(issue.organization, issue.sentry_issue_id)
        issue_data = state.get("issue") or {"title": issue.title, "exception_type": issue.exception_type,
                                            "event_count": issue.event_count}
        status = f"Patchora run #{run.id}: {run.status} ({run.outcome or 'in progress'})"
        body = tools.render_issue_body(issue=issue_data, permalink=issue.permalink, event=state.get("event"),
                                       diagnosis=state.get("diagnosis"), revision_note=state.get("revision_note"),
                                       status=status, marker=marker)
        full_name, repo_id, issue_pk, title = repo.full_name, repo.id, issue.id, issue.title
    try:
        host = deps.require_code_host()
        found = await host.find_issue_by_marker(full_name, marker)
        info = found or await host.create_issue(full_name, title=f"[Sentry] {title[:200]}", body=body,
                                                labels=["patchora"])
    finally:
        await deps.aclose()

    def persist(session: Any) -> None:
        if session.scalar(select(GitHubIssue).where(GitHubIssue.repository_id == repo_id,
                                                    GitHubIssue.external_issue_number == info.number)) is None:
            session.add(GitHubIssue(run_id=run_id, sentry_issue_id=issue_pk, repository_id=repo_id,
                                    external_issue_number=info.number, external_issue_id=info.id,
                                    title=info.title[:500], url=info.url, status=info.state,
                                    is_demo=host.simulated))
        audit_service.record(session, actor_type="user", actor_id=actor,
                             action="github_issue.linked" if found else "github_issue.created",
                             resource_type="analysis_run", resource_id=run_id,
                             details={"url": info.url, "simulated": host.simulated})
        audit_service.emit_event(session, run_id, "github_issue_created",
                                 f"GitHub issue {'linked' if found else 'created'}: #{info.number}",
                                 {"url": info.url})

    with_write_retry(persist)
    return {"created": found is None, "url": info.url, "number": info.number}


def request_publication(session: Any, run: AnalysisRun, actor: str) -> dict[str, Any]:
    """POST /runs/{id}/pull-request: idempotent publication request.

    * Existing PR -> returned unchanged.
    * Waiting for approval -> rejected (approve first, through the approvals endpoint).
    * A previously approved run whose publication failed transiently -> re-enqueued.
    """
    pr = session.scalar(select(PullRequest).where(PullRequest.run_id == run.id))
    if pr is not None:
        return {"status": "exists", "url": pr.url, "number": pr.external_pr_number}
    status = RunState(run.status)
    if status == RunState.WAITING_FOR_APPROVAL:
        raise ConflictError("This run is waiting for approval; approve it on the Approvals page first.")
    if status in {RunState.READY_TO_PUBLISH, RunState.PUBLISHING_BRANCH, RunState.CREATING_PULL_REQUEST}:
        if job_runner.enqueue(session, JobKind.RUN_WORKFLOW, {"run_id": run.id}, max_attempts=4) is not None:
            audit_service.record(session, actor_type="user", actor_id=actor, action="publication.requested",
                                 resource_type="analysis_run", resource_id=run.id)
        return {"status": "queued"}
    approved = session.scalar(select(Approval).where(Approval.run_id == run.id,
                                                     Approval.status == ApprovalStatus.APPROVED.value))
    if status == RunState.FAILED and approved is not None:
        raise InvalidRequestError("Publication previously failed; retry the run to produce a fresh patch.")
    raise ConflictError(f"A pull request cannot be created for a run in state {status.value}; only "
                        "validated, approved patches (or Mode C runs that pass policy) are published.")
