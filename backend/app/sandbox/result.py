"""Execution results and output sanitization."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.logging import redact

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_output(text: str) -> str:
    """Strip terminal escapes/control characters and redact credentials.

    Test output is untrusted: it may contain escape sequences, fake status lines or prompt
    injection. It is stored for display only and wrapped as untrusted data if a model sees it.
    """
    return redact(_CONTROL.sub("", _ANSI.sub("", text)))


@dataclass
class ExecutionResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    backend: str = "none"
    simulated: bool = False
    error: str | None = None  # the sandbox itself failed (e.g. docker unavailable)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")
_JEST_TESTS = re.compile(r"Tests:\s+(.*?)\s+total", re.IGNORECASE)


def parse_test_counts(output: str) -> tuple[int | None, int | None]:
    """Best-effort (passed, failed) counts. Informational only; exit codes decide outcomes."""
    tail = output[-4000:]
    jest = _JEST_TESTS.findall(tail)
    if jest:
        text = jest[-1]
        passed_match = re.search(r"(\d+) passed", text)
        failed_match = re.search(r"(\d+) failed", text)
        return (int(passed_match.group(1)) if passed_match else 0,
                int(failed_match.group(1)) if failed_match else 0)
    matches = _PYTEST_COUNTS.findall(tail)
    if not matches:
        return None, None
    passed = failed = 0
    for count, kind in matches[-6:]:
        if kind == "passed":
            passed = int(count)
        elif kind in {"failed", "error", "errors"}:
            failed += int(count)
    return passed, failed
