from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import AutonomyMode, Settings
from app.core.enums import UserRole
from app.core.exceptions import InvalidRequestError, PermissionDenied, PolicyViolation
from app.core.security import decrypt_secret, encrypt_secret, load_session, sign_session
from app.db.models import AppSetting, Repository
from app.db.session import session_scope
from app.security.authorization import Principal, can_approve, ensure_repository_allowed, require_role
from app.services.credential_service import resolve_credential, save_credential
from app.services.settings_service import get_runtime_settings, update_runtime_settings


def test_production_requires_secure_configuration():
    with pytest.raises(ValidationError, match="AUTH_MODE=local"):
        Settings(_env_file=None, app_env="production", auth_mode="local")
    with pytest.raises(ValidationError, match="APP_SECRET_KEY"):
        Settings(_env_file=None, app_env="production", auth_mode="token", app_secret_key="short")
    with pytest.raises(ValidationError, match="local is not permitted"):
        Settings(_env_file=None, app_env="production", auth_mode="token", app_secret_key="x" * 40,
                 sandbox_backend="local")


def test_blank_values_and_lists():
    s = Settings(_env_file=None, groq_api_key="", groq_model="  ", protected_paths="a/**, b.txt")
    assert s.groq_api_key is None and s.groq_model is None
    assert s.protected_paths == ["a/**", "b.txt"]
    assert s.autonomy_mode == AutonomyMode.PREPARE_PATCH and s.auto_create_pull_requests is False


def test_runtime_settings_validation(settings):
    with session_scope() as s:
        values, diff = update_runtime_settings(s, {"autonomy_mode": "analyze_only", "max_patch_files": 3}, "admin",
                                               settings)
        assert values.autonomy_mode == AutonomyMode.ANALYZE_ONLY and set(diff) == {"autonomy_mode", "max_patch_files"}
        with pytest.raises(InvalidRequestError):
            update_runtime_settings(s, {"max_patch_files": 0}, "admin", settings)
        with pytest.raises(InvalidRequestError):
            update_runtime_settings(s, {"not_a_setting": 1}, "admin", settings)
        with pytest.raises(InvalidRequestError):  # unsafe local execution only in development
            update_runtime_settings(s, {"sandbox_backend": "local", "allow_unsafe_local_execution": True},
                                    "admin", settings)
    with session_scope() as s:
        assert get_runtime_settings(s, settings).max_patch_files == 3


def test_corrupt_override_falls_back_to_safe_defaults(settings):
    with session_scope() as s:
        s.add(AppSetting(key="max_patch_files", value_json=9999))
    with session_scope() as s:
        assert get_runtime_settings(s, settings).max_patch_files == settings.max_patch_files


def test_credentials_stored_in_database_without_secret_key(settings):
    """No APP_SECRET_KEY: values are still saved to the database (unencrypted, prefixed) and used."""
    with session_scope() as s:
        save_credential(s, "groq", "api_key", "gsk_value_123", settings)
        save_credential(s, "groq", "model", "  some-model  ", settings)
    with session_scope() as s:
        from app.db.models import IntegrationCredential

        rows = {r.name: r.encrypted_value for r in s.query(IntegrationCredential)}
        assert rows["api_key"] == "plain:gsk_value_123"
        assert resolve_credential(s, "groq", "api_key", settings).source == "saved"
        assert resolve_credential(s, "groq", "model", settings).value == "some-model"
        save_credential(s, "groq", "model", "", settings)  # clearing removes the row
    with session_scope() as s:
        assert resolve_credential(s, "groq", "model", settings).value is None


def test_credentials_encrypted_when_secret_key_configured(settings):
    keyed = settings.model_copy(update={"app_secret_key": __import__("pydantic").SecretStr("k" * 40)})
    with session_scope() as s:
        save_credential(s, "groq", "api_key", "gsk_value_123", keyed)
    with session_scope() as s:
        from app.db.models import IntegrationCredential

        row = s.query(IntegrationCredential).one()
        assert row.encrypted_value.startswith("enc:") and "gsk_value_123" not in row.encrypted_value
        assert resolve_credential(s, "groq", "api_key", keyed).value == "gsk_value_123"


@pytest.mark.parametrize(("provider", "name", "value"), [
    ("sentry", "organization", "bad org!"), ("sentry", "base_url", "ftp://x"), ("sentry", "base_url", "http://evil.com"),
    ("github", "app_id", "12a"), ("github", "private_key", "not a pem"), ("groq", "model", "bad model; rm"),
    ("groq", "api_key", "line1\nline2"), ("nope", "x", "y"),
])
def test_invalid_integration_values_rejected(settings, provider, name, value):
    with session_scope() as s, pytest.raises(InvalidRequestError):
        save_credential(s, provider, name, value, settings)


def test_defaults_and_env_fallback(settings, monkeypatch):
    with session_scope() as s:
        base = resolve_credential(s, "sentry", "base_url", settings)
        assert (base.value, base.source) == ("https://sentry.io", "default")
    env = settings.model_copy(update={"sentry_org": "from-env"})
    with session_scope() as s:
        assert resolve_credential(s, "sentry", "organization", env).source == "env"
        save_credential(s, "sentry", "organization", "from-db", env)
    with session_scope() as s:
        assert resolve_credential(s, "sentry", "organization", env).value == "from-db"  # database wins


def test_crypto_helpers():
    token = encrypt_secret("k" * 32, "secret")
    assert decrypt_secret("k" * 32, token) == "secret"
    assert decrypt_secret("other-key-0000000000", token) is None
    cookie = sign_session("k" * 32, 5)
    assert load_session("k" * 32, cookie) == 5
    assert load_session("k" * 32, cookie + "x") is None


def test_roles_and_repository_allowlist():
    viewer = Principal(1, "v", "V", UserRole.VIEWER)
    reviewer = Principal(2, "r", "R", UserRole.REVIEWER)
    agent = Principal(3, "agent", "Agent", UserRole.ADMIN, actor_type="agent")
    assert not can_approve(viewer) and can_approve(reviewer) and not can_approve(agent)
    with pytest.raises(PermissionDenied):
        require_role(viewer, UserRole.REVIEWER)
    with pytest.raises(PermissionDenied):
        require_role(agent, UserRole.VIEWER)  # the agent is never a user
    repo = Repository(full_name="a/b", enabled=False, is_demo=False)
    with pytest.raises(PolicyViolation):
        ensure_repository_allowed(repo)
    with pytest.raises(PolicyViolation):
        ensure_repository_allowed(None)
    repo.enabled = True
    assert ensure_repository_allowed(repo) is repo
