"""Local subprocess runner - DEVELOPMENT ONLY, WEAK ISOLATION.

This is *not* a security sandbox. It runs commands as the current user with access to the
host filesystem and network. It is only usable when ``SANDBOX_BACKEND=local`` *and*
``ALLOW_UNSAFE_LOCAL_EXECUTION=true`` in a development environment, and it is intended for
repositories you trust (e.g. the bundled synthetic fixtures). Mitigations applied:

* a minimal environment (no inherited credentials),
* the disposable workspace as working directory and HOME/TMP,
* wall-clock timeout with process-tree kill, bounded output.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from app.sandbox.interface import run_bounded
from app.sandbox.limits import SandboxLimits
from app.sandbox.result import ExecutionResult


class LocalSandbox:
    name = "local"
    isolation = "process"
    simulated = False

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    async def available(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, ("Local execution is disabled. It requires ALLOW_UNSAFE_LOCAL_EXECUTION=true "
                           "in development and provides weak isolation.")
        return True, "Local subprocess runner (weak isolation, development only)"

    def _env(self, workspace: Path) -> dict[str, str]:
        tmp = workspace / ".sandbox-tmp"
        tmp.mkdir(exist_ok=True)
        env = {
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
            "HOME": str(tmp),
            "USERPROFILE": str(tmp),
            "TMP": str(tmp),
            "TEMP": str(tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
            "CI": "true",
        }
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        return env

    def _resolve(self, argv: list[str]) -> list[str]:
        if argv[0] in {"python", "python3"}:
            return [sys.executable, *argv[1:]]
        if argv[0] == "pytest":
            return [sys.executable, "-m", "pytest", *argv[1:]]
        resolved = shutil.which(argv[0])
        return [resolved or argv[0], *argv[1:]]

    async def run(self, workspace: Path, argv: list[str], limits: SandboxLimits, *,
                  image: str | None = None) -> ExecutionResult:
        ok, reason = await self.available()
        if not ok:
            return ExecutionResult(exit_code=None, stdout="", stderr="", duration_ms=0, backend=self.name,
                                   error=reason)
        return await asyncio.to_thread(
            run_bounded, self._resolve(argv), cwd=workspace, env=self._env(workspace),
            timeout=limits.timeout_seconds, max_output_bytes=limits.max_output_bytes, backend=self.name,
        )
