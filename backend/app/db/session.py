"""Engine and session management.

SQLite is configured for a single application instance:

* ``foreign_keys=ON`` on every connection,
* WAL journal mode so readers do not block the single writer,
* a ``busy_timeout`` so short write contention waits instead of failing immediately.

Transactions are kept short. Callers must never hold a session open across an LLM call,
GitHub/Sentry request or sandbox execution (see :mod:`app.agent.graph`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def build_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        path = url.split("sqlite:///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 5},
            pool_pre_ping=True,
        )
        event.listen(engine, "connect", _sqlite_pragmas)
        return engine
    return create_engine(url, pool_pre_ping=True)


def init_engine(url: str | None = None) -> Engine:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = build_engine(url or get_settings().database_url)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """A short transaction: commit on success, roll back on error."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and (
        "database is locked" in str(exc).lower() or "busy" in str(exc).lower()
    )


def with_write_retry[T](fn: Callable[[Session], T], attempts: int = 5) -> T:
    """Run ``fn`` in its own transaction, retrying on SQLite busy/locked errors."""
    delay = 0.05
    for attempt in range(attempts):
        try:
            with session_scope() as session:
                return fn(session)
        except OperationalError as exc:
            if not is_locked_error(exc) or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)
    raise RuntimeError("unreachable")  # pragma: no cover


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
