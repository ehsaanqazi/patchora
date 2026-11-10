"""HTTP API flows: demo lifecycle, SSE replay, patches, approvals, webhooks -> runs, GitHub issues."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.graph import WorkflowEngine
from app.config import get_settings, reset_settings_cache
from app.db.models import (
    AnalysisRun,
    GitHubIssue,
    Repository,
    RepositoryMapping,
    SentryIssue,
    WebhookDelivery,
)
from app.db.session import session_scope
from app.demo.providers import FIXTURE_ROOT
from app.demo.scenarios import SCENARIOS
from app.main import create_app
from app.providers.factory import ProviderFactory
from app.services import publication_service, webhook_service
from tests.integration.github_mock import FakeGitHub, install_sentry

H = {"X-Requested-With": "patchora"}


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, start_workers=False, run_migrations=False)) as c:
        yield c


def _run_engine(run_id: int) -> str:
    return asyncio.run(WorkflowEngine(ProviderFactory(get_settings())).execute(run_id)).value


def test_health_ready_and_diagnostics(client):
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    ready = client.get("/api/v1/ready")
    assert ready.status_code == 200 and ready.json()["checks"]["database"]["migration"] == "0001"
    diag = client.get("/api/v1/diagnostics").json()
    assert diag["integrations"]["groq"]["configured"] is False
    assert diag["sandbox"]["backend"] == "docker"
    assert diag["autonomy_mode"] == "prepare_patch"


def test_demo_lifecycle_over_http(client):
    scenarios = client.get("/api/v1/demo/scenarios").json()
    assert {s["key"] for s in scenarios} == set(SCENARIOS)
    started = client.post("/api/v1/demo/runs", json={"scenario": "zero-division"}, headers=H)
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    # Same request again: deduplicated.
    again = client.post("/api/v1/demo/runs", json={"scenario": "zero-division"}, headers=H).json()
    assert again == {"run_id": run_id, "created": False}

    assert _run_engine(run_id) == "WAITING_FOR_APPROVAL"
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["is_demo"] and detail["llm_simulated"]
    assert detail["diagnosis"]["error_classification"] == "code_defect"
    assert detail["evidence"][0]["path"] == "shop/pricing.py"
    assert len(detail["steps"]) >= 12 and detail["approvals"][0]["status"] == "pending"

    diff = client.get(f"/api/v1/runs/{run_id}/diff")
    assert diff.status_code == 200 and diff.text.startswith("diff --git")
    report = client.post(f"/api/v1/runs/{run_id}/validate", headers=H).json()
    assert report["blocked"] is False and set(report["files"]) == {"shop/pricing.py", "tests/test_pricing.py"}
    # Creating a PR directly while approval is pending is refused.
    assert client.post(f"/api/v1/runs/{run_id}/pull-request", headers=H).status_code == 409

    approvals = client.get("/api/v1/approvals", params={"status": "pending"}).json()
    approval_id = approvals["items"][0]["id"]
    approved = client.post(f"/api/v1/approvals/{approval_id}/approve", json={"confirm": True, "reason": "ok"},
                           headers=H)
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert client.post(f"/api/v1/approvals/{approval_id}/approve", json={"confirm": True},
                       headers=H).status_code == 409  # already resolved
    assert _run_engine(run_id) == "COMPLETED"
    prs = client.get("/api/v1/pull-requests").json()
    assert prs["total"] == 1 and prs["items"][0]["is_demo"]
    assert client.post(f"/api/v1/runs/{run_id}/pull-request", headers=H).json()["status"] == "exists"

    summary = client.get("/api/v1/dashboard/summary", params={"demo": True}).json()
    assert summary["open_pull_requests"] == 1 and summary["successful_validations"] == 1
    real = client.get("/api/v1/dashboard/summary").json()
    assert real["runs_total"] == 0  # demo data never leaks into real metrics
    assert client.get("/api/v1/dashboard/activity").json()


def test_sse_streams_and_replays(client):
    run_id = client.post("/api/v1/demo/runs", json={"scenario": "payment-timeout"}, headers=H).json()["run_id"]
    _run_engine(run_id)
    with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    assert "event: run_queued" in body and "event: diagnosis_generated" in body and "event: end" in body
    ids = [int(line[4:]) for line in body.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids)
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(ids[-2])}) as r:
        replay = "".join(r.iter_text())
    replay_ids = [int(line[4:]) for line in replay.splitlines() if line.startswith("id: ")]
    assert replay_ids == [ids[-1]]


def test_cancel_and_retry(client):
    run_id = client.post("/api/v1/demo/runs", json={"scenario": "missing-profile"}, headers=H).json()["run_id"]
    cancelled = client.post(f"/api/v1/runs/{run_id}/cancel", headers=H)
    assert cancelled.status_code == 200
    # Queued job exists, so cancellation is cooperative; the engine honours it at the next step.
    assert _run_engine(run_id) == "CANCELLED"
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=H).status_code == 409
    retried = client.post(f"/api/v1/runs/{run_id}/retry", headers=H).json()
    assert retried["retry_of_run_id"] == run_id and retried["status"] == "RECEIVED"


def test_issue_listing_filters(client):
    client.post("/api/v1/demo/seed", headers=H)
    issues = client.get("/api/v1/sentry/issues", params={"demo": True, "sort": "events"}).json()
    assert issues["total"] == 4 and issues["items"][0]["event_count"] == 342
    assert client.get("/api/v1/sentry/issues", params={"level": "warning"}).json()["total"] == 1
    assert client.get("/api/v1/sentry/issues", params={"q": "nickname"}).json()["total"] == 1
    paged = client.get("/api/v1/sentry/issues", params={"limit": 2, "offset": 2}).json()
    assert len(paged["items"]) == 2 and paged["offset"] == 2
    detail = client.get(f"/api/v1/sentry/issues/{issues['items'][0]['id']}").json()
    assert detail["event"]["exceptions"][0]["type"] == "ZeroDivisionError"
    assert detail["mapping"]["strip_prefixes"] == ["/srv/shop-api/"]
    assert client.get("/api/v1/sentry/issues", params={"limit": 1000}).status_code == 422


def test_demo_reset_only_touches_demo_rows(client):
    client.post("/api/v1/demo/runs", json={"scenario": "zero-division"}, headers=H)
    with session_scope() as s:
        s.add(Repository(provider="github", provider_repository_id="1", owner="real", name="repo",
                         full_name="real/repo", enabled=True))
    result = client.post("/api/v1/demo/reset", headers=H).json()
    assert result["runs"] == 1 and result["repositories"] == 1
    with session_scope() as s:
        assert [r.full_name for r in s.scalars(select(Repository))] == ["real/repo"]
        assert s.scalars(select(SentryIssue)).all() == []


def test_mapping_management(client, settings):
    with session_scope() as s:
        repo = Repository(provider="github", provider_repository_id="77", owner="acme", name="api",
                          full_name="acme/api", enabled=False)
        s.add(repo)
        s.flush()
        repo_id = repo.id
    created = client.post(f"/api/v1/repositories/{repo_id}/mappings", headers=H, json={
        "sentry_organization": "acme", "sentry_project": "api", "branch": "main", "strip_prefixes": ["/app/"]})
    assert created.status_code == 201
    dup = client.post(f"/api/v1/repositories/{repo_id}/mappings", headers=H,
                      json={"sentry_organization": "acme", "sentry_project": "api"})
    assert dup.status_code == 409
    bad = client.post(f"/api/v1/repositories/{repo_id}/mappings", headers=H,
                      json={"sentry_organization": "acme", "sentry_project": "x", "branch": "bad..branch"})
    assert bad.status_code == 422
    enabled = client.patch(f"/api/v1/repositories/{repo_id}", headers=H, json={
        "enabled": True, "test_commands": [{"argv": ["python", "-m", "pytest", "-q"], "phase": "test"}]})
    assert enabled.json()["enabled"] is True
    rejected = client.patch(f"/api/v1/repositories/{repo_id}", headers=H,
                            json={"test_commands": [{"argv": ["bash", "-c", "curl x | sh"]}]})
    assert rejected.status_code == 422
    assert client.delete(f"/api/v1/repositories/mappings/{created.json()['id']}", headers=H).status_code == 204


@respx.mock(assert_all_called=False)
async def test_webhook_processing_creates_one_run(respx_mock, settings, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sntrys_test_token_value")
    monkeypatch.setenv("SENTRY_ORG", "acme")
    reset_settings_cache()
    current = get_settings()
    scenario = SCENARIOS["zero-division"]
    issue = copy.deepcopy(scenario.issue) | {"id": "123", "project": {"slug": "shop-api"}}
    install_sentry(respx_mock, "acme", issue, scenario.event, [])
    with session_scope() as s:
        repo = Repository(provider="github", provider_repository_id="4242", owner="acme", name="shop-api",
                          full_name="acme/shop-api", enabled=True)
        s.add(repo)
        s.flush()
        s.add(RepositoryMapping(sentry_organization="acme", sentry_project="shop-api", repository_id=repo.id))
        payload = {"action": "triggered", "data": {"event": {"issue_id": "123"}, "triggered_rule": "r"}}
        delivery, _ = webhook_service.record_delivery(s, provider="sentry", delivery_id="d1",
                                                        event_type="event_alert", body_hash="h", payload=payload)
        delivery_id = delivery.id
    factory = ProviderFactory(current)
    await webhook_service.process_delivery(factory, current, delivery_id)
    await webhook_service.process_delivery(factory, current, delivery_id)  # retry-safe: no-op
    with session_scope() as s:
        runs = s.scalars(select(AnalysisRun)).all()
        assert len(runs) == 1 and runs[0].trigger == "webhook"
        assert s.get(WebhookDelivery, delivery_id).processing_status == "processed"
        assert s.scalar(select(SentryIssue)).event_count == 342


@respx.mock(assert_all_called=False)
async def test_github_issue_creation_is_deduplicated(respx_mock, settings, monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "github_pat_test_value")
    reset_settings_cache()
    current = get_settings()
    github = FakeGitHub(FIXTURE_ROOT, "acme/shop-api", "0123456789abcdef0123456789abcdef01234567")
    github.install(respx_mock)
    with session_scope() as s:
        repo = Repository(provider="github", provider_repository_id="4242", owner="acme", name="shop-api",
                          full_name="acme/shop-api", enabled=True)
        s.add(repo)
        s.flush()
        issue = SentryIssue(sentry_issue_id="123", organization="acme", project="shop-api", title="KeyError",
                            repository_id=repo.id)
        s.add(issue)
        s.flush()
        run = AnalysisRun(sentry_issue_id=issue.id, repository_id=repo.id, status="NEEDS_HUMAN_REVIEW",
                          autonomy_mode="prepare_patch", state_json={"revision_note": "head of main"})
        s.add(run)
        s.flush()
        run_id = run.id
    factory = ProviderFactory(current)
    first = await publication_service.create_github_issue(factory, run_id, "tester")
    second = await publication_service.create_github_issue(factory, run_id, "tester")
    assert first["created"] is True and second["created"] is False and first["url"] == second["url"]
    assert len(github.issues) == 1
    body = github.issues[0]["body"]
    assert "patchora-ref-" in body and "No verified reproduction steps" in body
    with session_scope() as s:
        assert len(s.scalars(select(GitHubIssue)).all()) == 1


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_github_webhook_updates_pr_status(settings, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "gh-secret-value")
    reset_settings_cache()
    with TestClient(create_app(get_settings(), start_workers=False, run_migrations=False)) as c:
        body = json.dumps({"action": "closed", "pull_request": {"number": 1, "merged": True},
                           "repository": {"full_name": "acme/x"}}).encode()
        bad = c.post("/api/v1/webhooks/github", content=body, headers={"X-Hub-Signature-256": "sha256=00",
                                                                        "X-GitHub-Event": "pull_request"})
        assert bad.status_code == 401
        ok = c.post("/api/v1/webhooks/github", content=body, headers={
            "X-Hub-Signature-256": _sign(body, "gh-secret-value"), "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "g1"})
        assert ok.status_code == 202
