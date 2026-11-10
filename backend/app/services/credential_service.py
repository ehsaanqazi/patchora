"""Integration configuration stored in the database.

Every Groq, Sentry and GitHub field (API keys, tokens, model id, organization, URLs, GitHub App
id/installation/private key, webhook secrets) is configured on the Integrations page and stored in
``integration_credentials``. The application reads them from there at use time, so changes apply
without a restart.

Storage: values are encrypted with ``APP_SECRET_KEY`` when one is configured; otherwise they are
stored unencrypted (prefix ``plain:``) - a deliberate interim choice until key management and
rotation are added. Secret values are never returned by the API.

Environment variables are no longer required. If one is set and the database has no value for
that field, it is used as a fallback (the Integrations page shows the source).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.exceptions import InvalidRequestError
from app.core.security import decrypt_secret, encrypt_secret
from app.db.base import utcnow
from app.db.models import IntegrationCredential
from app.security.input_sanitization import validate_slug


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    secret: bool
    env_attr: str | None  # optional environment fallback (Settings attribute)
    kind: str = "text"  # text | url | slug | digits | model | pem
    required: bool = False
    help: str = ""
    default: str | None = None


FIELDS: dict[str, list[FieldSpec]] = {
    "groq": [
        FieldSpec("api_key", "API key", True, "groq_api_key", required=True,
                  help="Groq console → API Keys."),
        FieldSpec("model", "Model id", False, "groq_model", kind="model", required=True,
                  help="A current chat model that supports JSON mode. 'Test connection' lists the models "
                       "available to your key."),
        FieldSpec("base_url", "Base URL", False, "groq_base_url", kind="url",
                  help="Optional. Leave empty for the Groq default."),
    ],
    "sentry": [
        FieldSpec("auth_token", "Auth token", True, "sentry_auth_token", required=True,
                  help="Internal integration token with event:read, project:read, org:read."),
        FieldSpec("organization", "Organization slug", False, "sentry_org", kind="slug", required=True),
        FieldSpec("project", "Project slug", False, "sentry_project", kind="slug",
                  help="Used to browse and import issues."),
        FieldSpec("webhook_secret", "Webhook client secret", True, "sentry_webhook_secret",
                  help="Signs Sentry-Hook-Signature. Webhooks are disabled until set."),
        FieldSpec("base_url", "Sentry URL", False, "sentry_base_url", kind="url", default="https://sentry.io",
                  help="Change only for self-hosted Sentry."),
    ],
    "github": [
        FieldSpec("app_id", "GitHub App id", False, "github_app_id", kind="digits",
                  help="Preferred: GitHub App (with installation id and private key)."),
        FieldSpec("installation_id", "Installation id", False, "github_installation_id", kind="digits"),
        FieldSpec("private_key", "App private key (PEM)", True, None, kind="pem",
                  help="Paste the full contents of the App's .pem file."),
        FieldSpec("personal_access_token", "Personal access token", True, "github_personal_access_token",
                  help="Alternative to the App for local use: a fine-grained token limited to selected repos."),
        FieldSpec("webhook_secret", "Webhook secret", True, "github_webhook_secret",
                  help="Optional: pull_request status updates."),
        FieldSpec("api_url", "API URL", False, "github_api_url", kind="url", default="https://api.github.com",
                  help="Change only for GitHub Enterprise Server."),
    ],
}

PROVIDERS = tuple(FIELDS)
_MODEL_RE = re.compile(r"^[A-Za-z0-9._/:-]{1,120}$")
_PLAIN = "plain:"
_ENC = "enc:"
MAX_VALUE = 16_000


def field_spec(provider: str, name: str) -> FieldSpec:
    for spec in FIELDS.get(provider, []):
        if spec.name == name:
            return spec
    raise InvalidRequestError(f"Unknown integration field {provider}.{name}")


@dataclass(frozen=True)
class ResolvedCredential:
    value: str | None
    source: str | None  # "saved" | "env" | "default" | None


def _secret_key(settings: Settings) -> str | None:
    return settings.app_secret_key.get_secret_value() if settings.app_secret_key else None


def _decode(stored: str, settings: Settings) -> str | None:
    if stored.startswith(_PLAIN):
        return stored[len(_PLAIN):]
    if stored.startswith(_ENC):
        return decrypt_secret(_secret_key(settings), stored[len(_ENC):])
    return decrypt_secret(_secret_key(settings), stored)  # rows written before prefixes existed


def _env_value(spec: FieldSpec, settings: Settings) -> str | None:
    if spec.name == "private_key":  # legacy: key file referenced by GITHUB_PRIVATE_KEY_PATH
        path = settings.github_private_key_path
        try:
            return path.read_text(encoding="utf-8") if path else None
        except OSError:
            return None
    if spec.env_attr is None:
        return None
    raw = getattr(settings, spec.env_attr, None)
    if raw is None:
        return None
    text = raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw)
    if not text or (spec.default is not None and text == spec.default):
        return None  # untouched built-in defaults are reported as "default", not "env"
    return text


def resolve_credential(session: Session, provider: str, name: str,
                       settings: Settings | None = None) -> ResolvedCredential:
    settings = settings or get_settings()
    spec = field_spec(provider, name)
    row = session.scalar(select(IntegrationCredential).where(IntegrationCredential.provider == provider,
                                                             IntegrationCredential.name == name))
    if row is not None:
        value = _decode(row.encrypted_value, settings)
        if value:
            return ResolvedCredential(value, "saved")
    env = _env_value(spec, settings)
    if env:
        return ResolvedCredential(env, "env")
    if spec.default:
        return ResolvedCredential(spec.default, "default")
    return ResolvedCredential(None, None)


def credential_value(session: Session, provider: str, name: str, settings: Settings | None = None) -> str | None:
    return resolve_credential(session, provider, name, settings).value


def _validate(spec: FieldSpec, value: str) -> str:
    value = value.strip() if spec.kind != "pem" else value.strip() + "\n"
    if len(value) > MAX_VALUE:
        raise InvalidRequestError(f"{spec.label} is too long.")
    if spec.kind != "pem" and any(ord(c) < 32 for c in value):
        raise InvalidRequestError(f"{spec.label} contains control characters.")
    if spec.kind == "url":
        parsed = urlparse(value)
        local = parsed.hostname in {"localhost", "127.0.0.1"}
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or (parsed.scheme == "http" and not local):
            raise InvalidRequestError(f"{spec.label} must be an https:// URL.")
        value = value.rstrip("/")
    elif spec.kind == "slug":
        validate_slug(value, spec.label)
    elif spec.kind == "digits" and not value.isdigit():
        raise InvalidRequestError(f"{spec.label} must be numeric.")
    elif spec.kind == "model" and not _MODEL_RE.match(value):
        raise InvalidRequestError(f"{spec.label} contains invalid characters.")
    elif spec.kind == "pem" and ("-----BEGIN" not in value or "PRIVATE KEY-----" not in value):
        raise InvalidRequestError("The private key must be a PEM block (-----BEGIN ... PRIVATE KEY-----).")
    return value


def save_credential(session: Session, provider: str, name: str, value: str | None,
                    settings: Settings | None = None) -> None:
    """Store (or clear, when value is empty/None) one integration field in the database."""
    settings = settings or get_settings()
    spec = field_spec(provider, name)
    existing = session.scalar(select(IntegrationCredential).where(IntegrationCredential.provider == provider,
                                                                  IntegrationCredential.name == name))
    if value is None or value.strip() == "":
        if existing is not None:
            session.delete(existing)
        return
    clean = _validate(spec, value)
    key = _secret_key(settings)
    stored = _ENC + encrypt_secret(key, clean) if key else _PLAIN + clean
    if existing is None:
        session.add(IntegrationCredential(provider=provider, name=name, encrypted_value=stored))
    else:
        existing.encrypted_value = stored
        existing.updated_at = utcnow()


def all_secret_values(session: Session, settings: Settings | None = None) -> list[str]:
    """Every configured secret (saved or env), used for log redaction and diff leak detection."""
    settings = settings or get_settings()
    values = set(settings.secret_values())
    for provider, specs in FIELDS.items():
        for spec in specs:
            if spec.secret:
                resolved = resolve_credential(session, provider, spec.name, settings)
                if resolved.value and len(resolved.value) >= 8:
                    values.add(resolved.value)
    return sorted(values)


def encryption_enabled(settings: Settings | None = None) -> bool:
    return _secret_key(settings or get_settings()) is not None
