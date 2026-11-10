"""Sandbox interface and the bounded subprocess primitive shared by the runners."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import IO, Protocol

from app.sandbox.limits import SandboxLimits
from app.sandbox.result import ExecutionResult, sanitize_output


class Sandbox(Protocol):
    name: str
    isolation: str  # container | process | simulated | none
    simulated: bool

    async def available(self) -> tuple[bool, str]: ...

    async def run(self, workspace: Path, argv: list[str], limits: SandboxLimits, *,
                  image: str | None = None) -> ExecutionResult: ...


class _BoundedReader(threading.Thread):
    """Reads a stream keeping a bounded head and tail; counts the rest."""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.head_limit = limit // 4
        self.tail_limit = limit - self.head_limit
        self.head = bytearray()
        self.tail: deque[bytes] = deque()
        self.tail_size = 0
        self.dropped = 0

    def run(self) -> None:
        while True:
            chunk = self.stream.read(4096)
            if not chunk:
                break
            if len(self.head) < self.head_limit:
                take = self.head_limit - len(self.head)
                self.head.extend(chunk[:take])
                chunk = chunk[take:]
            if chunk:
                self.tail.append(chunk)
                self.tail_size += len(chunk)
                while self.tail_size > self.tail_limit and self.tail:
                    removed = self.tail.popleft()
                    self.tail_size -= len(removed)
                    self.dropped += len(removed)

    def text(self) -> tuple[str, bool]:
        tail = b"".join(self.tail)
        if self.dropped:
            data = bytes(self.head) + f"\n...[{self.dropped} bytes truncated]...\n".encode() + tail
        else:
            data = bytes(self.head) + tail
        return sanitize_output(data.decode("utf-8", errors="replace")), self.dropped > 0


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_bounded(
    argv: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    timeout: float,
    max_output_bytes: int,
    backend: str,
    on_timeout: Callable[[], None] | None = None,
) -> ExecutionResult:
    started = time.monotonic()
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd) if cwd else None, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,  # type: ignore[call-overload]
        )
    except (OSError, ValueError) as exc:
        return ExecutionResult(exit_code=None, stdout="", stderr="", duration_ms=0, backend=backend,
                               error=f"failed to start process: {type(exc).__name__}: {exc}")
    assert proc.stdout is not None and proc.stderr is not None
    out_reader = _BoundedReader(proc.stdout, max_output_bytes)
    err_reader = _BoundedReader(proc.stderr, max_output_bytes // 2)
    out_reader.start()
    err_reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if on_timeout is not None:
            on_timeout()
        _kill_tree(proc)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
    out_reader.join(timeout=5)
    err_reader.join(timeout=5)
    stdout, t1 = out_reader.text()
    stderr, t2 = err_reader.text()
    return ExecutionResult(
        exit_code=None if timed_out else proc.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=timed_out,
        truncated=t1 or t2,
        backend=backend,
    )


SECRET_ENV_PREFIXES = ("GROQ_", "GITHUB_", "SENTRY_", "APP_", "AWS_", "AZURE_", "GOOGLE_", "OPENAI_",
                       "ANTHROPIC_", "DATABASE_", "GH_", "NPM_TOKEN", "PYPI_")


def scrubbed_environ() -> dict[str, str]:
    """Host environment minus anything that could carry credentials."""
    return {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith(SECRET_ENV_PREFIXES)
        and not any(word in k.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL"))
    }
