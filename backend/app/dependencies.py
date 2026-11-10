"""FastAPI dependencies: settings, database sessions, providers and authentication.

Authentication modes:

* ``AUTH_MODE=local`` (development only; refused when ``APP_ENV=production``): a single local
  admin user, accepted only from loopback clients.
* ``AUTH_MODE=token``: per-user API tokens (created with ``scripts/create_user.py``; only the
  SHA-256 hash is stored). The browser exchanges a token once for an HttpOnly, SameSite=Strict
  session cookie signed with ``APP_SECRET_KEY``, so the token is not kept in frontend state.

State-changing requests must carry ``X-Requested-With: patchora`` (enforced by middleware
in :mod:`app.main`); combined with the strict CORS policy this blocks cross-site requests.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.enums import UserRole
from app.core.exceptions import AuthenticationRequired, PermissionDenied
from app.core.security import SESSION_COOKIE, hash_api_token, load_session
from app.db.models import User
from app.db.session import get_db, with_write_retry
from app.providers.factory import ProviderFactory
from app.security.authorization import Principal, require_role

LOCAL_USER = "local-admin@patchora.local"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def settings_dep() -> Settings:
    return get_settings()


def db_dep() -> Iterator[Session]:
    yield from get_db()


def factory_dep(request: Request) -> ProviderFactory:
    factory: ProviderFactory = request.app.state.factory
    return factory


def _principal(user: User) -> Principal:
    return Principal(user_id=user.id, external_id=user.external_id, display_name=user.display_name,
                     role=UserRole(user.role))


def ensure_local_user(session: Session) -> User:
    user = session.scalar(select(User).where(User.external_id == LOCAL_USER))
    if user is None:
        user = User(external_id=LOCAL_USER, display_name="Local developer", role=UserRole.ADMIN.value)
        session.add(user)
        session.flush()
    return user


def current_principal(request: Request, session: Session = Depends(db_dep),
                      settings: Settings = Depends(settings_dep)) -> Principal:
    if settings.auth_mode == "local":
        host = request.client.host if request.client else ""
        if host not in LOOPBACK:
            raise PermissionDenied("Local auth mode only accepts requests from this machine.")
        # Own short, committed transaction: never hold a write lock for the whole request.
        return with_write_retry(lambda s: _principal(ensure_local_user(s)))

    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header.split(" ", 1)[1].strip()
        user = session.scalar(select(User).where(User.api_token_hash == hash_api_token(token),
                                                 User.active.is_(True)))
        if user is not None:
            return _principal(user)
        raise AuthenticationRequired("Invalid API token.")
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        secret = settings.app_secret_key.get_secret_value() if settings.app_secret_key else None
        user_id = load_session(secret, cookie)
        if user_id is not None:
            user = session.get(User, user_id)
            if user is not None and user.active:
                return _principal(user)
    raise AuthenticationRequired("Sign in to continue.")


def reviewer(principal: Principal = Depends(current_principal)) -> Principal:
    require_role(principal, UserRole.REVIEWER)
    return principal


def admin(principal: Principal = Depends(current_principal)) -> Principal:
    require_role(principal, UserRole.ADMIN)
    return principal


class Pagination:
    def __init__(self, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000)) -> None:
        self.limit = limit
        self.offset = offset
