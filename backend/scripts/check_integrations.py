"""Check integration configuration and connectivity without printing secret values."""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.db.migrate import upgrade_to_head
from app.db.session import init_engine, session_scope
from app.providers.factory import ProviderFactory
from app.services.integration_service import integration_cards, test_integration
from app.services.settings_service import get_runtime_settings


async def main() -> None:
    settings = get_settings()
    upgrade_to_head(settings.database_url)
    init_engine(settings.database_url)
    factory = ProviderFactory(settings)
    with session_scope() as session:
        for card in integration_cards(session, settings):
            fields = ", ".join(f"{f.env_var}={'set' if f.configured else 'missing'}" for f in card.fields)
            print(f"[{card.provider}] configured={card.configured} {fields}")
            if card.configured:
                result = await test_integration(card.provider, session, factory)
                print(f"    connection: {'OK' if result.ok else 'FAILED'} - {result.message}")
        sandbox = factory.sandbox(get_runtime_settings(session, settings))
        ok, reason = await sandbox.available()
        print(f"[sandbox] backend={sandbox.name} available={ok} - {reason}")


if __name__ == "__main__":
    asyncio.run(main())
