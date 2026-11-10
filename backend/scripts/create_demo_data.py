"""Seed or reset demo data (synthetic, clearly labeled; never touches real rows).

    python backend/scripts/create_demo_data.py          # seed demo repository, mapping and issues
    python backend/scripts/create_demo_data.py --reset  # remove demo rows only
"""

from __future__ import annotations

import argparse

from app.config import get_settings
from app.db.migrate import upgrade_to_head
from app.db.session import init_engine, session_scope
from app.services import demo_service


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Remove demo data only")
    args = parser.parse_args()
    settings = get_settings()
    upgrade_to_head(settings.database_url)
    init_engine(settings.database_url)
    with session_scope() as session:
        if args.reset:
            print("Removed demo rows:", demo_service.reset_demo_data(session, "cli"))
        else:
            print("Demo data ready:", demo_service.ensure_demo_data(session))


if __name__ == "__main__":
    main()
