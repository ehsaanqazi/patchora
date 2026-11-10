"""Demo data management. Touches only rows flagged ``is_demo``."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import AutonomyMode
from app.core.enums import JobStatus
from app.core.exceptions import InvalidRequestError, NotFoundError
from app.db.models import (
    AnalysisRun,
    Approval,
    GitHubIssue,
    Job,
    PullRequest,
    Repository,
    RepositoryMapping,
    SentryIssue,
)
from app.demo.scenarios import (
    DEMO_ORG,
    DEMO_PROJECT,
    DEMO_REPO,
    DEMO_STRIP_PREFIX,
    SCENARIOS,
)
from app.services import audit_service, run_service
from app.services.sentry_service import parse_event, parse_issue


def ensure_demo_data(session: Session) -> dict[str, Any]:
    repo = session.scalar(select(Repository).where(Repository.provider == "demo",
                                                   Repository.provider_repository_id == "demo-shop-api"))
    if repo is None:
        owner, name = DEMO_REPO.split("/")
        repo = Repository(provider="demo", provider_repository_id="demo-shop-api", owner=owner, name=name,
                          full_name=DEMO_REPO, default_branch="main", enabled=True, is_demo=True, private=True)
        session.add(repo)
        session.flush()
    mapping = run_service.find_mapping(session, DEMO_ORG, DEMO_PROJECT)
    if mapping is None:
        session.add(RepositoryMapping(sentry_organization=DEMO_ORG, sentry_project=DEMO_PROJECT,
                                      repository_id=repo.id, branch="main", path_prefixes_json=["shop/"],
                                      strip_prefixes_json=[DEMO_STRIP_PREFIX], is_demo=True))
        session.flush()
    issues = []
    for scenario in SCENARIOS.values():
        row = run_service.upsert_issue(session, parse_issue(scenario.issue, DEMO_ORG), is_demo=True,
                                       event=parse_event(scenario.event))
        row.repository_id = repo.id
        issues.append(row.id)
    return {"repository_id": repo.id, "issue_ids": issues}


def start_demo_run(session: Session, scenario_key: str, *, actor: str, mode: AutonomyMode,
                   use_real_llm: bool = False) -> tuple[AnalysisRun, bool]:
    scenario = SCENARIOS.get(scenario_key)
    if scenario is None:
        raise NotFoundError(f"Unknown demo scenario '{scenario_key}'")
    ensure_demo_data(session)
    issue = session.scalar(select(SentryIssue).where(SentryIssue.organization == DEMO_ORG,
                                                     SentryIssue.sentry_issue_id == scenario.issue["id"]))
    if issue is None or not issue.is_demo:
        raise InvalidRequestError("Demo issue missing.")
    return run_service.create_run(
        session, issue, trigger="demo", actor_type="user", actor_id=actor, autonomy_mode=mode,
        extra_state={"demo_scenario": scenario_key, "use_real_llm": use_real_llm},
    )


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def reset_demo_data(session: Session, actor: str) -> dict[str, int]:
    """Remove demo rows only. Real configuration, repositories and runs are never touched."""
    run_ids = list(session.scalars(select(AnalysisRun.id).where(AnalysisRun.is_demo.is_(True))))
    removed_jobs = 0
    for job in session.scalars(select(Job).where(Job.status.in_([JobStatus.QUEUED.value,
                                                                  JobStatus.RUNNING.value,
                                                                  JobStatus.SUCCEEDED.value,
                                                                  JobStatus.FAILED.value,
                                                                  JobStatus.CANCELLED.value]))):
        if job.payload_json.get("run_id") in run_ids:
            session.delete(job)
            removed_jobs += 1
    counts = {
        "pull_requests": _rowcount(session.execute(delete(PullRequest).where(PullRequest.is_demo.is_(True)))),
        "approvals": _rowcount(session.execute(delete(Approval).where(Approval.is_demo.is_(True)))),
        "github_issues": _rowcount(session.execute(delete(GitHubIssue).where(GitHubIssue.is_demo.is_(True)))),
    }
    for run in session.scalars(select(AnalysisRun).where(AnalysisRun.is_demo.is_(True))):
        session.delete(run)  # cascades to steps, events, patches and test executions
    session.flush()
    counts["runs"] = len(run_ids)
    counts["issues"] = _rowcount(session.execute(delete(SentryIssue).where(SentryIssue.is_demo.is_(True))))
    counts["mappings"] = _rowcount(session.execute(delete(RepositoryMapping)
                                                   .where(RepositoryMapping.is_demo.is_(True))))
    counts["repositories"] = _rowcount(session.execute(delete(Repository).where(Repository.is_demo.is_(True))))
    counts["jobs"] = removed_jobs
    audit_service.record(session, actor_type="user", actor_id=actor, action="demo.reset",
                         resource_type="demo", resource_id=None, details=counts)
    return {k: int(v or 0) for k, v in counts.items()}
