"""Simulated sandbox for demo mode. Executes nothing; results are labeled as simulated."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.sandbox.limits import SandboxLimits
from app.sandbox.result import ExecutionResult

Responder = Callable[[Path, list[str]], ExecutionResult]


class SimulatedSandbox:
    name = "simulated"
    isolation = "simulated"
    simulated = True

    def __init__(self, responder: Responder | None = None) -> None:
        self._responder = responder

    async def available(self) -> tuple[bool, str]:
        return True, "Simulated execution (demo mode - no code is executed)"

    async def run(self, workspace: Path, argv: list[str], limits: SandboxLimits, *,
                  image: str | None = None) -> ExecutionResult:
        if self._responder is not None:
            result = self._responder(workspace, argv)
        else:
            result = ExecutionResult(exit_code=0, stdout="[simulated] 1 passed", stderr="", duration_ms=0)
        result.backend = self.name
        result.simulated = True
        return result
