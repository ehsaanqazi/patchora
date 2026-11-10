"""Structured logging with secret redaction.

Every log record passes through :class:`RedactingFilter`, which removes configured secret
values, bearer tokens and well-known credential formats before the record is emitted.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_TOKEN_PATTERNS = [
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(bearer|token|basic)\s+[^\s'\",]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsntry[su]_[A-Za-z0-9_=+/-]{20,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),  # Anthropic / OpenAI / DeepSeek keys
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google (Gemini) API keys
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]

_extra_secrets: list[str] = []


def register_secrets(values: list[str]) -> None:
    """Register exact secret values that must never appear in logs."""
    for value in values:
        if value and value not in _extra_secrets:
            _extra_secrets.append(value)


def redact(text: str) -> str:
    for secret in _extra_secrets:
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    for pattern in _TOKEN_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)} [REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        return True


_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        return redact(base)


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_patchora", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler._patchora = True  # type: ignore[attr-defined]
    handler.addFilter(RedactingFilter())
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(KeyValueFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx logs full URLs at INFO; keep them at WARNING to avoid noisy/sensitive output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
