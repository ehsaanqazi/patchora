"""Detection of credentials and secret-like strings in generated content."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?i)aws_?secret_?access_?key['\"]?\s*[:=]\s*['\"][A-Za-z0-9/+=]{40}['\"]")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("groq_api_key", re.compile(r"\bgsk_[A-Za-z0-9]{40,}\b")),
    ("sentry_token", re.compile(r"\bsntry[su]_[A-Za-z0-9_=+/-]{30,}")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("connection_string_password",
     re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]{6,}@[^\s/]+")),
]

_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|api_?key|access_?token|auth_?token|client_?secret|private_?key)\b"
    r"\s*[:=]\s*['\"]([^'\"\s]{12,})['\"]"
)
_PLACEHOLDERS = ("example", "changeme", "placeholder", "dummy", "test", "fake", "xxxx", "your_", "<", "${")


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    line: str


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {c: value.count(c) for c in set(value)}
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in counts.values())


def scan_text(text: str, known_secrets: list[str] | None = None) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for secret in known_secrets or []:
        if secret and len(secret) >= 8 and secret in text:
            findings.append(SecretFinding("configured_secret_value", "[redacted]"))
    for line in text.splitlines():
        for kind, pattern in _PATTERNS:
            if pattern.search(line):
                findings.append(SecretFinding(kind, line.strip()[:80]))
        for match in _ASSIGNMENT.finditer(line):
            value = match.group(2)
            if any(p in value.lower() for p in _PLACEHOLDERS):
                continue
            if _entropy(value) >= 3.5:
                findings.append(SecretFinding("hardcoded_credential", line.strip()[:80]))
    return findings
