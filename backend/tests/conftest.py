"""Shared fixtures. Tests never use real credentials or the developer's .env file."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ["PATCHORA_ENV_FILE"] = ""  # never read the developer's .env during tests
for _key in list(os.environ):
    if _key.startswith(("GROQ_", "GITHUB_", "SENTRY_", "APP_")) and _key != "PATCHORA_ENV_FILE":
        del os.environ[_key]

from app.config import Settings, get_settings, reset_settings_cache  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.migrate import upgrade_to_head  # noqa: E402

_TEMPLATE: Path | None = None


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run Alembic migrations once; each test copies the resulting database file."""
    global _TEMPLATE
    path = tmp_path_factory.mktemp("template") / "template.db"
    upgrade_to_head(f"sqlite:///{path.as_posix()}")
    _TEMPLATE = path
    return path


@pytest.fixture
def settings(tmp_path: Path, migrated_template: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    db_path = tmp_path / "test.db"
    shutil.copy(migrated_template, db_path)
    env = {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "DATA_DIR": str(tmp_path / "data"),
        "AUTH_MODE": "local",
        "WORKER_ENABLED": "false",
        "SANDBOX_BACKEND": "docker",
        "LOG_LEVEL": "WARNING",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    current = get_settings()
    db_session.init_engine(current.database_url)
    yield current
    db_session.get_engine().dispose()
    reset_settings_cache()


@pytest.fixture
def session(settings: Settings) -> Iterator[object]:
    with db_session.session_scope() as s:
        yield s
