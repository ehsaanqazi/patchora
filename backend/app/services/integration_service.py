"""Integration status cards and safe connection tests (secrets are never returned)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import AppError, ExternalServiceError, NotConfiguredError
from app.db.base import utcnow
from app.db.models import IntegrationStatus
from app.db.session import with_write_retry
from app.providers.factory import ProviderFactory
from app.schemas.integrations import ConnectionTestResult, FieldStatus, IntegrationOut
from app.services.credential_service import FIELDS, LLM_PROVIDERS, encryption_enabled, resolve_credential
from app.services.settings_service import get_runtime_settings

LABELS = {"groq": "Groq", "anthropic": "Anthropic", "openai": "OpenAI", "deepseek": "DeepSeek",
          "gemini": "Google Gemini", "bedrock": "AWS Bedrock", "sentry": "Sentry", "github": "GitHub"}
CATEGORIES = {"sentry": "issues", "github": "code_host", **{p: "llm" for p in LLM_PROVIDERS}}
# Stable API order: the original three first, then the additional model providers.
ORDER = ("groq", "sentry", "github", *(p for p in LLM_PROVIDERS if p != "groq"))


def _status_row(session: Session, provider: str) -> IntegrationStatus | None:
    return session.get(IntegrationStatus, provider)


def integration_cards(session: Session, settings: Settings) -> list[IntegrationOut]:
    cards: list[IntegrationOut] = []
    storage_note = ("Values are stored in the Patchora database, encrypted with APP_SECRET_KEY."
                    if encryption_enabled(settings) else
                    "Values are stored in the Patchora database (data/patchora.db) without encryption. "
                    "Protect that file; encryption and rotation are planned.")
    active_llm = get_runtime_settings(session, settings).llm_provider.value
    for provider in ORDER:
        specs = FIELDS[provider]
        statuses: list[FieldStatus] = []
        configured_names: set[str] = set()
        for spec in specs:
            resolved = resolve_credential(session, provider, spec.name, settings)
            if resolved.value is not None and resolved.source != "default":
                configured_names.add(spec.name)
            statuses.append(FieldStatus(
                name=spec.name, label=spec.label, help=spec.help, kind=spec.kind, required=spec.required,
                env_var=(spec.env_attr or "").upper(), secret=spec.secret,
                configured=resolved.value is not None, source=resolved.source,
                value=None if spec.secret else resolved.value))
        mode: str | None = None
        if provider == "github":
            app_ready = {"app_id", "installation_id", "private_key"} <= configured_names
            pat = "personal_access_token" in configured_names
            mode = "github_app" if app_ready else ("personal_access_token" if pat else None)
            configured = mode is not None
        else:
            configured = all(spec.name in configured_names for spec in specs if spec.required)
        row = _status_row(session, provider)
        cards.append(IntegrationOut(
            provider=provider, label=LABELS.get(provider, provider), category=CATEGORIES.get(provider, "other"),
            active=provider == active_llm, configured=configured, mode=mode, fields=statuses,
            last_tested_at=row.last_tested_at if row else None, last_success_at=row.last_success_at if row else None,
            last_ok=row.last_ok if row else None, last_message=row.last_message if row else None,
            notes=[storage_note],
        ))
    return cards


def _record(provider: str, ok: bool, message: str) -> None:
    def _do(session: Session) -> None:
        row = session.get(IntegrationStatus, provider)
        if row is None:
            row = IntegrationStatus(provider=provider)
            session.add(row)
        row.last_tested_at = utcnow()
        row.last_ok = ok
        row.last_message = message[:1000]
        if ok:
            row.last_success_at = row.last_tested_at

    with_write_retry(_do)


async def test_integration(provider: str, session: Session, factory: ProviderFactory) -> ConnectionTestResult:
    details: dict[str, Any] = {}
    client: Any = None
    try:
        if provider == "sentry":
            client = factory.sentry(session)
            details = await client.test_connection()
            message = f"Connected to Sentry organization '{details.get('organization')}'."
        elif provider == "github":
            client = factory.github(session)
            details = await client.test_connection()
            message = f"Authenticated to GitHub via {details.get('auth')}."
        elif provider in LLM_PROVIDERS:
            label = LABELS[provider]
            client = factory.llm_for(session, provider)
            details = await client.diagnose()
            if provider == "bedrock":
                message = (f"AWS credentials valid; model '{details['model']}' answered in {details.get('region')} "
                           "(the test sent one tiny request).")
            elif details.get("model_available"):
                message = f"{label} key valid; model '{details['model']}' is available."
            else:
                message = (f"{label} key valid, but model '{details['model']}' is not available to this key. "
                           "Choose one of the listed models.")
            if not details.get("model_available"):
                _record(provider, False, message)
                return ConnectionTestResult(provider=provider, ok=False, message=message, details=details)
        else:
            raise NotConfiguredError(f"Unknown provider {provider}")
    except NotConfiguredError as exc:
        _record(provider, False, exc.message)
        return ConnectionTestResult(provider=provider, ok=False, message=exc.message)
    except ExternalServiceError as exc:
        _record(provider, False, exc.message)
        return ConnectionTestResult(provider=provider, ok=False, message=exc.message,
                                    details={"upstream_status": exc.upstream_status})
    except AppError as exc:
        _record(provider, False, exc.message)
        return ConnectionTestResult(provider=provider, ok=False, message=exc.message)
    finally:
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()
        elif client is not None and hasattr(client, "_client"):
            close = getattr(client._client, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
    _record(provider, True, message)
    return ConnectionTestResult(provider=provider, ok=True, message=message, details=details)
