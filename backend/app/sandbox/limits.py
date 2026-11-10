"""Execution limits and trusted command validation."""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLOWED_EXECUTABLES = frozenset({
    "python", "python3", "pytest", "pip", "npm", "npx", "node", "yarn", "pnpm", "go", "cargo",
    "ruff", "mypy",
})
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./:=@,+%-]{1,200}$")


def validate_command_argv(argv: list[str]) -> list[str]:
    """Validate an admin-configured command. Commands are argv lists executed without a shell."""
    if not argv or len(argv) > 30:
        raise ValueError("command must have between 1 and 30 arguments")
    if argv[0] not in ALLOWED_EXECUTABLES:
        raise ValueError(f"executable '{argv[0]}' is not on the allowlist {sorted(ALLOWED_EXECUTABLES)}")
    for arg in argv:
        if not _SAFE_ARG.match(arg):
            raise ValueError(f"argument {arg!r} contains disallowed characters")
        if arg.startswith("/") or ".." in arg.split("/"):
            raise ValueError(f"argument {arg!r} must be a workspace-relative path")
    return argv


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: int = 300
    cpus: float = 1.0
    memory_mb: int = 1024
    pids_limit: int = 256
    max_output_bytes: int = 64_000
    network: bool = False  # only the dependency-setup phase may enable network
