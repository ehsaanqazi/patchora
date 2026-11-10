"""Choosing the active LLM provider: stored as an audited runtime setting and used for every run."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import NotConfiguredError
from app.db.session import session_scope
from app.main import create_app
from app.providers.factory import ProviderFactory
from app.services.anthropic_service import AnthropicLLM
from app.services.bedrock_service import BedrockLLM
from app.services.credential_service import save_credential
from app.services.openai_compatible_service import OpenAICompatibleLLM
from app.services.settings_service import get_runtime_settings

H = {"X-Requested-With": "patchora"}


def test_switch_active_provider_and_build_its_client(settings):
    with TestClient(create_app(settings, start_workers=False, run_migrations=False)) as c:
        cards = {card["provider"]: card for card in c.get("/api/v1/integrations").json()}
        assert {"groq", "anthropic", "openai", "deepseek", "gemini", "bedrock", "sentry", "github"} <= set(cards)
        assert cards["groq"]["active"] is True and cards["anthropic"]["category"] == "llm"
        assert cards["sentry"]["category"] == "issues" and cards["gemini"]["label"] == "Google Gemini"

        saved = c.put("/api/v1/integrations/anthropic/credentials", headers=H,
                      json={"values": {"api_key": "sk-ant-test-secret-value-0123456789abcdef", "model": "claude-opus-5-5"}})
        assert saved.status_code == 200 and saved.json()["configured"] is True
        assert "sk-ant-test-secret" not in c.get("/api/v1/integrations").text

        switched = c.put("/api/v1/settings", headers=H, json={"values": {"llm_provider": "anthropic"}})
        assert switched.status_code == 200, switched.text
        cards = {card["provider"]: card for card in c.get("/api/v1/integrations").json()}
        assert cards["anthropic"]["active"] is True and cards["groq"]["active"] is False
        diag = c.get("/api/v1/diagnostics").json()
        assert diag["llm"] == {"provider": "anthropic", "model": "claude-opus-5-5", "configured": True,
                               "max_input_tokens": settings.llm_max_input_tokens}
        audit = c.get("/api/v1/audit", params={"limit": 50}).json()["items"]
        assert any(e["action"].startswith("settings") for e in audit)

        assert c.put("/api/v1/settings", headers=H, json={"values": {"llm_provider": "nope"}}).status_code == 422

        bad_region = c.put("/api/v1/integrations/bedrock/credentials", headers=H,
                           json={"values": {"region": "not a region"}})
        assert bad_region.status_code == 422

    factory = ProviderFactory(settings)
    with session_scope() as session:
        runtime = get_runtime_settings(session, settings)
        assert isinstance(factory.llm(session, runtime), AnthropicLLM)
        with pytest.raises(NotConfiguredError):  # Groq has no key saved in this test
            factory.llm_for(session, "groq")
        save_credential(session, "deepseek", "api_key", "sk-deepseek-test-value-123456", settings)
        save_credential(session, "deepseek", "model", "deepseek-chat", settings)
        save_credential(session, "bedrock", "region", "us-east-1", settings)
        save_credential(session, "bedrock", "model", "anthropic.claude-opus-5-5", settings)
        session.flush()
        deepseek = factory.llm_for(session, "deepseek")
        assert isinstance(deepseek, OpenAICompatibleLLM) and deepseek.model_name == "deepseek-chat"
        assert isinstance(factory.llm_for(session, "bedrock"), BedrockLLM)
