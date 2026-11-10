"""Create or upgrade the SQLite database to the latest migration."""

from __future__ import annotations

from app.config import get_settings
from app.db.migrate import upgrade_to_head


def main() -> None:
    settings = get_settings()
    upgrade_to_head(settings.database_url)
    print(f"Database ready: {settings.database_url}")


if __name__ == "__main__":
    main()
