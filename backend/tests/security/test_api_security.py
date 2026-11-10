"""HTTP-level security: webhooks, CSRF, authorization, secret handling, cross-repo access."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings, reset_settings_cache
from app.core.enums import JobKind, UserRole
from app.core.security import hash_api_token
from app.db.models import AuditLog, Job, Repository, User, WebhookDelivery
from app.db.session import session_scope
from app.main import create_app

H = {"X-Requested-With": "patchora"}
SECRET = "whsec-test-secret-value"


def client_for(settings) -> TestClient:
    return TestClient(create_app(settings, start_workers=False, run_migrations=False))


@pytest.fixture
def client(settings):
    with client_for(settings) as c:
        yield c


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def webhook_client(settings, monkeypatch):
    monkeypatch.setenv("SENTRY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SENTRY_ORG", "acme")
    reset_settings_cache()
    with client_for(get_settings()) as c:
        yield c


ISSUE_PAYLOAD = {"action": "created", "data": {"issue": {"id": "555", "title": "KeyError: 'x'",
                                                        "project": {"slug": "web"}, "count": "1"}}}


def _post_webhook(c: TestClient, payload: dict, *, signature: str | None = None, request_id: str = "req-1",
                  resource: str = "issue"):
    body = json.dumps(payload).encode()
    return c.post("/api/v1/webhooks/sentry", content=body, headers={
        "Content-Type": "application/json", "Sentry-Hook-Resource": resource,
        "Sentry-Hook-Signature": signature if signature is not None else _sign(body), "Request-ID": request_id})


def test_webhook_rejects_invalid_signature(webhook_client):
    response = _post_webhook(webhook_client, ISSUE_PAYLOAD, signature="0" * 64)
    assert response.status_code == 401
    with session_scope() as s:
        assert s.scalars(select(WebhookDelivery)).all() == []
        assert s.scalar(select(AuditLog).where(AuditLog.action == "webhook.rejected")) is not None


def test_webhook_rejects_tampered_body(webhook_client):
    body = json.dumps(ISSUE_PAYLOAD).encode()
    signature = _sign(body)
    tampered = body.replace(b"555", b"556")
    response = webhook_client.post("/api/v1/webhooks/sentry", content=tampered, headers={
        "Sentry-Hook-Resource": "issue", "Sentry-Hook-Signature": signature, "Request-ID": "r"})
    assert response.status_code == 401


def test_webhook_disabled_without_secret(client):
    assert _post_webhook(client, ISSUE_PAYLOAD).status_code == 503


def test_webhook_duplicate_delivery_processed_once(webhook_client):
    first = _post_webhook(webhook_client, ISSUE_PAYLOAD, request_id="dup-1")
    second = _post_webhook(webhook_client, ISSUE_PAYLOAD, request_id="dup-1")
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["duplicate"] is False and second.json()["duplicate"] is True
    with session_scope() as s:
        assert len(s.scalars(select(WebhookDelivery)).all()) == 1
        jobs = s.scalars(select(Job).where(Job.kind == JobKind.PROCESS_WEBHOOK.value)).all()
        assert len(jobs) == 1


def test_webhook_malformed_payload(webhook_client):
    assert _post_webhook(webhook_client, {"no": "data"}).status_code == 422
    assert _post_webhook(webhook_client, ISSUE_PAYLOAD, resource="unknown-thing").status_code == 422
    bad = b"{not json"
    response = webhook_client.post("/api/v1/webhooks/sentry", content=bad, headers={
        "Sentry-Hook-Resource": "issue", "Sentry-Hook-Signature": _sign(bad)})
    assert response.status_code == 422


def test_webhook_rate_limited(webhook_client):
    webhook_client.app.state.webhook_limiter.limit = 2
    statuses = [_post_webhook(webhook_client, ISSUE_PAYLOAD, request_id=f"r{i}").status_code for i in range(4)]
    assert statuses[:2] == [202, 202] and statuses[-1] == 429


def test_csrf_header_required_for_state_changes(client):
    assert client.post("/api/v1/demo/seed").status_code == 403
    assert client.post("/api/v1/demo/seed", headers=H).status_code == 200


def test_token_auth_mode(settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("APP_SECRET_KEY", "k" * 40)
    reset_settings_cache()
    with session_scope() as s:
        s.add(User(external_id="viewer@x", display_name="Viewer", role=UserRole.VIEWER.value,
                   api_token_hash=hash_api_token("sfx_viewer_token_1234567890")))
        s.add(User(external_id="rev@x", display_name="Reviewer", role=UserRole.REVIEWER.value,
                   api_token_hash=hash_api_token("sfx_reviewer_token_123456789")))
    with client_for(get_settings()) as c:
        assert c.get("/api/v1/runs").status_code == 401
        assert c.get("/api/v1/runs", headers={"Authorization": "Bearer wrong-token-xyz"}).status_code == 401
        viewer = {"Authorization": "Bearer sfx_viewer_token_1234567890"}
        assert c.get("/api/v1/runs", headers=viewer).status_code == 200
        # A viewer cannot approve, start runs or change settings.
        assert c.post("/api/v1/approvals/1/approve", json={"confirm": True}, headers=viewer).status_code == 403
        assert c.post("/api/v1/demo/runs", json={"scenario": "zero-division"}, headers=viewer).status_code == 403
        assert c.put("/api/v1/settings", json={"values": {"autonomy_mode": "auto_pr"}},
                     headers=viewer).status_code == 403
        # Cookie session login: token exchanged for an HttpOnly cookie.
        login = c.post("/api/v1/auth/login", json={"token": "sfx_reviewer_token_123456789"}, headers=H)
        assert login.status_code == 200
        cookie = login.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert c.get("/api/v1/auth/me").json()["role"] == "reviewer"


def test_unauthorized_and_unconfirmed_approvals(client):
    client.post("/api/v1/demo/seed", headers=H)
    assert client.post("/api/v1/approvals/999/approve", json={"confirm": True}, headers=H).status_code == 404
    assert client.post("/api/v1/approvals/999/approve", json={"confirm": False}, headers=H).status_code == 422


def test_local_mode_rejects_remote_clients(settings):
    app = create_app(settings, start_workers=False, run_migrations=False)
    with TestClient(app, client=("203.0.113.9", 5000)) as c:
        assert c.get("/api/v1/runs").status_code == 403


def test_secrets_never_returned(settings, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_supersecretvalue_1234567890")
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sntrys_supersecret_token_abcdef")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "github_pat_supersecret_value")
    reset_settings_cache()
    with client_for(get_settings()) as c:
        for path in ("/api/v1/integrations", "/api/v1/diagnostics", "/api/v1/settings"):
            text = c.get(path).text
            assert "supersecret" not in text, path
        cards = {card["provider"]: card for card in c.get("/api/v1/integrations").json()}
        groq_key = next(f for f in cards["groq"]["fields"] if f["name"] == "api_key")
        assert groq_key["configured"] is True and groq_key["value"] is None


def test_cross_repository_access_blocked(client):
    client.post("/api/v1/demo/seed", headers=H)
    with session_scope() as s:
        demo = s.scalar(select(Repository).where(Repository.is_demo.is_(True)))
        demo.enabled = False  # remove from allowlist
    run = client.post("/api/v1/demo/runs", json={"scenario": "zero-division"}, headers=H).json()
    import asyncio

    from app.agent.graph import WorkflowEngine
    from app.providers.factory import ProviderFactory

    final = asyncio.run(WorkflowEngine(ProviderFactory(get_settings())).execute(run["run_id"]))
    assert final.value == "NEEDS_HUMAN_REVIEW"
    detail = client.get(f"/api/v1/runs/{run['run_id']}").json()
    assert detail["outcome"] == "policy_blocked" and "allowlist" in detail["error_message"]


def test_settings_changes_are_audited(client):
    response = client.put("/api/v1/settings", json={"values": {"max_patch_files": 4}}, headers=H)
    assert response.status_code == 200 and response.json()["values"]["max_patch_files"] == 4
    assert client.put("/api/v1/settings", json={"values": {"max_patch_files": 100}}, headers=H).status_code == 422
    with session_scope() as s:
        entry = s.scalar(select(AuditLog).where(AuditLog.action == "settings.security_changed"))
        assert entry is not None and entry.details_json["max_patch_files"] == {"from": 10, "to": 4}


def test_audit_log_is_append_only(client):
    client.post("/api/v1/demo/seed", headers=H)
    from sqlalchemy.exc import IntegrityError, OperationalError

    with pytest.raises((IntegrityError, OperationalError)), session_scope() as s:
        entry = s.scalar(select(AuditLog))
        entry.outcome = "tampered"
        s.flush()
