"""All integration settings are configured through the API, stored in the DB and used from there."""

from __future__ import annotations

import hashlib
import hmac
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.db.session import session_scope
from app.main import create_app
from app.providers.factory import ProviderFactory
from app.services.github_service import GitHubAppAuth
from app.services.settings_service import get_runtime_settings

H = {"X-Requested-With": "patchora"}


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def test_configure_everything_from_integrations(settings):
    pem = _pem()
    with TestClient(create_app(settings, start_workers=False, run_migrations=False)) as c:
        cards = {card["provider"]: card for card in c.get("/api/v1/integrations").json()}
        assert not any(card["configured"] for card in cards.values())
        assert {f["name"] for f in cards["github"]["fields"]} >= {"app_id", "installation_id", "private_key",
                                                                   "personal_access_token", "api_url"}

        groq = c.put("/api/v1/integrations/groq/credentials", headers=H,
                     json={"values": {"api_key": "gsk_db_secret_value_12345", "model": "db-model"}})
        assert groq.status_code == 200 and groq.json()["configured"] is True
        sentry = c.put("/api/v1/integrations/sentry/credentials", headers=H, json={"values": {
            "auth_token": "sntrys_db_token_value", "organization": "acme", "project": "web",
            "webhook_secret": "db-webhook-secret", "base_url": "https://sentry.example.com/"}})
        assert sentry.status_code == 200 and sentry.json()["configured"] is True
        github = c.put("/api/v1/integrations/github/credentials", headers=H, json={"values": {
            "app_id": "12345", "installation_id": "678", "private_key": pem}})
        assert github.status_code == 200 and github.json()["mode"] == "github_app"
        bad = c.put("/api/v1/integrations/github/credentials", headers=H, json={"values": {"app_id": "abc"}})
        assert bad.status_code == 422

        # Secrets are never returned; non-secret values are shown with their source.
        text = c.get("/api/v1/integrations").text
        assert "gsk_db_secret_value" not in text and "sntrys_db_token" not in text and "PRIVATE KEY" not in text
        fields = {f["name"]: f for f in c.get("/api/v1/integrations").json()[1]["fields"]}
        assert fields["organization"]["value"] == "acme" and fields["organization"]["source"] == "saved"
        assert fields["base_url"]["value"] == "https://sentry.example.com"

        # Webhook verification uses the secret from the database.
        body = json.dumps({"action": "created", "data": {"issue": {"id": "1", "title": "x"}}}).encode()
        sig = hmac.new(b"db-webhook-secret", body, hashlib.sha256).hexdigest()
        ok = c.post("/api/v1/webhooks/sentry", content=body, headers={
            "Sentry-Hook-Resource": "issue", "Sentry-Hook-Signature": sig, "Request-ID": "db-1"})
        assert ok.status_code == 202

    factory = ProviderFactory(settings)
    with session_scope() as s:
        llm = factory.groq(s, get_runtime_settings(s, settings))
        assert llm.model_name == "db-model" and llm.config.api_key == "gsk_db_secret_value_12345"
        sentry_client = factory.sentry(s)
        assert sentry_client.organization == "acme" and sentry_client._base == "https://sentry.example.com"
        auth = factory.github_auth(s)
        assert isinstance(auth, GitHubAppAuth) and auth.installation_id == "678"
        assert auth.app_jwt()  # the stored PEM is a usable RS256 key
