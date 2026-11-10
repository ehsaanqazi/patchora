"""End-to-end: Sentry + GitHub over mocked HTTP, scripted model, REAL pytest execution.

The synthetic fixture repository has a deliberate ZeroDivisionError. The patch adds a
regression test; the engine must observe the test failing on the base revision and passing
with the fix, then (Mode C) publish a branch and pull request through the GitHub client.
"""

from __future__ import annotations

import copy
import json

import pytest
import respx
from sqlalchemy import select

from app.agent.graph import WorkflowEngine
from app.config import AutonomyMode
from app.core.enums import RunOutcome, RunState
from app.db.models import AnalysisRun, PullRequest, Repository, RepositoryMapping, TestExecution
from app.db.session import session_scope
from app.demo.providers import FIXTURE_ROOT
from app.demo.scenarios import SCENARIOS
from app.providers.factory import ProviderFactory
from app.providers.llm import ScriptedLLM
from app.sandbox.local_runner import LocalSandbox
from app.services import run_service
from app.services.sentry_service import parse_issue
from tests.integration.github_mock import FakeGitHub, install_sentry

SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "acme/shop-api"
ORG = "acme"


@pytest.fixture
def real_settings(settings, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sntrys_test_token_value")
    monkeypatch.setenv("SENTRY_ORG", ORG)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "github_pat_test_value_not_real")
    monkeypatch.setenv("AUTO_CREATE_PULL_REQUESTS", "true")
    from app.config import get_settings, reset_settings_cache

    reset_settings_cache()
    return get_settings()


def _seed(scenario_key: str, mode: AutonomyMode) -> tuple[int, dict]:
    scenario = SCENARIOS[scenario_key]
    issue = copy.deepcopy(scenario.issue)
    issue["id"] = "123"
    issue["project"] = {"slug": "shop-api"}
    event = copy.deepcopy(scenario.event)
    with session_scope() as s:
        repo = Repository(provider="github", provider_repository_id="4242", owner="acme", name="shop-api",
                          full_name=REPO, default_branch="main", enabled=True)
        s.add(repo)
        s.flush()
        s.add(RepositoryMapping(sentry_organization=ORG, sentry_project="shop-api", repository_id=repo.id,
                                strip_prefixes_json=["/srv/shop-api/"], path_prefixes_json=["shop/"]))
        s.flush()
        row = run_service.upsert_issue(s, parse_issue(issue, ORG))
        run, _ = run_service.create_run(s, row, trigger="manual", actor_type="user", actor_id="t", autonomy_mode=mode)
        return run.id, {"issue": issue, "event": event}


def _factory(settings, scenario_key: str) -> ProviderFactory:
    factory = ProviderFactory(settings)
    responses = {k: list(v) for k, v in SCENARIOS[scenario_key].responses.items()}
    factory.overrides["llm"] = lambda s, r: ScriptedLLM(responses=copy.deepcopy(responses))
    factory.overrides["sandbox"] = lambda s, r: LocalSandbox(enabled=True)
    return factory


@respx.mock(assert_all_called=False)
async def test_full_pipeline_with_real_test_execution(respx_mock, real_settings):
    run_id, data = _seed("zero-division", AutonomyMode.AUTO_PR)
    github = FakeGitHub(FIXTURE_ROOT, REPO, SHA)
    github.install(respx_mock)
    install_sentry(respx_mock, ORG, data["issue"], data["event"], [{"id": SHA, "repository": {"name": REPO}}])

    final = await WorkflowEngine(_factory(real_settings, "zero-division")).execute(run_id)

    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert final == RunState.COMPLETED, (run.status, run.error_message, run.state_json.get("notes"))
        assert run.outcome == RunOutcome.PULL_REQUEST_CREATED.value
        assert run.source_revision == SHA and run.source_revision_kind == "release_commit"
        assert run.diagnosis_status == "supported_by_tests"
        executions = s.scalars(select(TestExecution).where(TestExecution.run_id == run_id)
                               .order_by(TestExecution.id)).all()
        tests = {t.phase: t for t in executions}
        patched = [t for t in executions if t.phase == "patched"]
        # Targeted run (tests/test_pricing.py) first, then the full suite.
        assert [t.tests_passed for t in patched] == [4, 6]
        assert "tests/test_pricing.py" in patched[0].command_summary
        # Real execution: the new regression test fails on the base revision and passes with the fix.
        assert tests["baseline"].status == "failed" and tests["baseline"].simulated is False
        assert "ZeroDivisionError" in (tests["baseline"].stdout_summary or "")
        assert tests["patched"].status == "passed"
        assert "setup" not in tests  # the local runner never installs dependencies
        pr = s.scalar(select(PullRequest).where(PullRequest.run_id == run_id))
        assert pr.url == f"https://github.com/{REPO}/pull/1"
        assert pr.is_demo is False

    # The published commit was created on a new patchora/ branch from the pinned base commit.
    branch = next(r for r in github.refs if r.startswith("heads/patchora/"))
    commit = github.commits[github.refs[branch]]
    assert commit["parents"] == [SHA]
    assert f"Patchora-Run: {run_id}" in commit["message"]
    assert github.refs["heads/main"] == SHA  # default branch untouched
    assert github.ref_updates == []  # no ref was ever updated / force-pushed
    published = "".join(github.blobs.values())
    assert "if not orders:" in published
    body = github.pulls[0]["body"]
    assert "AI assistance" in body and "Supported by tests" in body
    assert github.pulls[0]["draft"] is True
    assert github.pulls[0]["title"].startswith("[ Patchora ] Fix:")


@respx.mock(assert_all_called=False)
async def test_publication_is_idempotent_after_crash(respx_mock, real_settings):
    """If the branch was created but the process died before recording it, resume reuses it."""
    run_id, data = _seed("zero-division", AutonomyMode.AUTO_PR)
    github = FakeGitHub(FIXTURE_ROOT, REPO, SHA)
    github.install(respx_mock)
    install_sentry(respx_mock, ORG, data["issue"], data["event"], [{"id": SHA, "repository": {"name": REPO}}])
    factory = _factory(real_settings, "zero-division")
    engine = WorkflowEngine(factory)

    from app.agent import nodes

    original = nodes.creating_pull_request
    calls = {"n": 0}

    async def crash_once(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt("simulated crash")  # escapes like a process kill would
        return await original(ctx)

    nodes.HANDLERS[RunState.CREATING_PULL_REQUEST] = crash_once
    try:
        with pytest.raises(KeyboardInterrupt):
            await engine.execute(run_id)
        with session_scope() as s:
            assert s.get(AnalysisRun, run_id).status == RunState.CREATING_PULL_REQUEST.value
        branches_before = [r for r in github.refs if r.startswith("heads/patchora/")]
        final = await engine.execute(run_id)
    finally:
        nodes.HANDLERS[RunState.CREATING_PULL_REQUEST] = original
    assert final == RunState.COMPLETED
    assert [r for r in github.refs if r.startswith("heads/patchora/")] == branches_before
    assert len(github.pulls) == 1


@respx.mock(assert_all_called=False)
async def test_repository_access_failure_stops_for_review(respx_mock, real_settings):
    run_id, data = _seed("zero-division", AutonomyMode.PREPARE_PATCH)
    github = FakeGitHub(FIXTURE_ROOT, REPO, SHA)
    github.fail_tree = True
    github.install(respx_mock)
    install_sentry(respx_mock, ORG, data["issue"], data["event"], [{"id": SHA, "repository": {"name": REPO}}])
    final = await WorkflowEngine(_factory(real_settings, "zero-division")).execute(run_id)
    assert final == RunState.NEEDS_HUMAN_REVIEW
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert "github" in (run.error_message or "").lower()
    assert json.dumps(github.pulls) == "[]"
