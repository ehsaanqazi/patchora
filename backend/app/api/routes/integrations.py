"""Integration status, credential management (write-only) and connection tests."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import NotFoundError
from app.dependencies import admin, current_principal, db_dep, factory_dep, settings_dep
from app.providers.factory import ProviderFactory
from app.schemas.integrations import ConnectionTestResult, CredentialUpdate, IntegrationOut
from app.security.authorization import Principal
from app.services import audit_service
from app.services.credential_service import PROVIDERS, save_credential
from app.services.groq_service import GroqConfig, GroqLLM
from app.services.integration_service import integration_cards, test_integration

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("", response_model=list[IntegrationOut])
def list_integrations(session: Session = Depends(db_dep), settings: Settings = Depends(settings_dep),
                      _: Principal = Depends(current_principal)) -> list[IntegrationOut]:
    return integration_cards(session, settings)


@router.post("/{provider}/test", response_model=ConnectionTestResult)
async def test_connection(provider: str, session: Session = Depends(db_dep),
                          factory: ProviderFactory = Depends(factory_dep),
                          principal: Principal = Depends(current_principal)) -> ConnectionTestResult:
    if provider not in PROVIDERS:
        raise NotFoundError(f"Unknown integration '{provider}'")
    result = await test_integration(provider, session, factory)
    audit_service.record(session, actor_type="user", actor_id=principal.external_id,
                         action="integration.tested", resource_type="integration", resource_id=provider,
                         outcome="success" if result.ok else "failure", details={"message": result.message})
    return result


@router.put("/{provider}/credentials", response_model=IntegrationOut)
def update_credentials(provider: str, body: CredentialUpdate, session: Session = Depends(db_dep),
                       settings: Settings = Depends(settings_dep),
                       principal: Principal = Depends(admin)) -> IntegrationOut:
    if provider not in PROVIDERS:
        raise NotFoundError(f"Unknown integration '{provider}'")
    for name, value in body.values.items():
        save_credential(session, provider, name, value, settings)
    # Field names only - values are never logged or audited.
    audit_service.record(session, actor_type="user", actor_id=principal.external_id,
                         action="integration.credentials_updated", resource_type="integration",
                         resource_id=provider, details={"fields": sorted(body.values)})
    session.flush()
    return next(c for c in integration_cards(session, settings) if c.provider == provider)


@router.get("/groq/models")
async def groq_models(session: Session = Depends(db_dep), factory: ProviderFactory = Depends(factory_dep),
                      _: Principal = Depends(current_principal)) -> dict[str, Any]:
    from app.services.credential_service import credential_value

    llm = GroqLLM(GroqConfig(api_key=credential_value(session, "groq", "api_key", factory.settings),
                             model=credential_value(session, "groq", "model", factory.settings) or "unset",
                             base_url=credential_value(session, "groq", "base_url", factory.settings)))
    return {"models": await llm.list_models()}
