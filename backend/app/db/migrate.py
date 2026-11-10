"""Programmatic Alembic migrations (used at startup, by scripts and by tests)."""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from app.config import REPO_ROOT, get_settings


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "app" / "db" / "migrations"))
    cfg.attributes["database_url"] = database_url or get_settings().database_url
    return cfg


def upgrade_to_head(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), "head")
