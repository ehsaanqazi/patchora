"""Cryptographic helpers: API-token hashing, session signing and credential encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.exceptions import NotConfiguredError

SESSION_COOKIE = "patchora_session"
SESSION_MAX_AGE_SECONDS = 12 * 3600


def generate_api_token() -> str:
    return "sfx_" + secrets.token_urlsafe(32)


def hash_api_token(token: str) -> str:
    # Tokens are 256-bit random values, so a fast hash is appropriate (no dictionary attack).
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str | bytes, b: str | bytes) -> bool:
    if isinstance(a, str):
        a = a.encode("utf-8")
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hmac.compare_digest(a, b)


def _require_key(secret_key: str | None) -> str:
    if not secret_key or len(secret_key) < 16:
        raise NotConfiguredError(
            "APP_SECRET_KEY must be set (at least 16 characters; 32+ recommended) for this operation."
        )
    return secret_key


def session_serializer(secret_key: str | None) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_require_key(secret_key), salt="patchora-session")


def sign_session(secret_key: str | None, user_id: int) -> str:
    return session_serializer(secret_key).dumps({"uid": user_id})


def load_session(secret_key: str | None, value: str) -> int | None:
    try:
        data = session_serializer(secret_key).loads(value, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired, NotConfiguredError):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, int) else None


def _fernet(secret_key: str | None) -> Fernet:
    key = _require_key(secret_key)
    # Legacy salt from the project's former name; changing it would make stored credentials unreadable.
    derived = hashlib.sha256(("sentryfix-credentials:" + key).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(secret_key: str | None, plaintext: str) -> str:
    return _fernet(secret_key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(secret_key: str | None, ciphertext: str) -> str | None:
    try:
        return _fernet(secret_key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, NotConfiguredError, ValueError):
        return None


def mask_secret(value: str | None) -> str | None:
    """A non-reversible hint that a secret is present. Never reveals characters."""
    if not value:
        return None
    return "configured"
