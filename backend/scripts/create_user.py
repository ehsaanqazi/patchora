"""Create a user and print a one-time API token (only its SHA-256 hash is stored).

    python backend/scripts/create_user.py alice@example.com "Alice" reviewer
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.config import get_settings
from app.core.enums import UserRole
from app.core.security import generate_api_token, hash_api_token
from app.db.migrate import upgrade_to_head
from app.db.models import User
from app.db.session import init_engine, session_scope
from app.services import audit_service


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("external_id")
    parser.add_argument("display_name")
    parser.add_argument("role", choices=[r.value for r in UserRole])
    args = parser.parse_args()
    settings = get_settings()
    upgrade_to_head(settings.database_url)
    init_engine(settings.database_url)
    token = generate_api_token()
    with session_scope() as session:
        user = session.scalar(select(User).where(User.external_id == args.external_id))
        if user is None:
            user = User(external_id=args.external_id, display_name=args.display_name, role=args.role)
            session.add(user)
        user.role = args.role
        user.display_name = args.display_name
        user.api_token_hash = hash_api_token(token)
        session.flush()
        audit_service.record(session, actor_type="system", actor_id="cli", action="user.token_issued",
                             resource_type="user", resource_id=user.id, details={"role": args.role})
    print(f"User {args.external_id} ({args.role}). API token (shown once, store it securely):\n{token}")


if __name__ == "__main__":
    main()
