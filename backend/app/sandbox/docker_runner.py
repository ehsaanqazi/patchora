"""Docker-based isolated execution (the preferred sandbox).

Each command runs in a fresh, disposable container:

* ``--network none`` (the dependency-setup phase alone may use the default bridge network,
  with no credentials present),
* non-root user, ``--read-only`` root filesystem with a size-limited ``/tmp`` tmpfs,
* ``--cap-drop ALL`` and ``no-new-privileges``,
* CPU, memory (swap disabled) and PID limits, wall-clock timeout (container is killed),
* only the disposable workspace is mounted - no Docker socket, home directory or credentials,
* an explicit, minimal environment (host variables are not inherited).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app.sandbox.interface import run_bounded, scrubbed_environ
from app.sandbox.limits import SandboxLimits
from app.sandbox.result import ExecutionResult

CONTAINER_WORKDIR = "/workspace"
DEPS_DIR = f"{CONTAINER_WORKDIR}/.sandbox-deps"


class DockerSandbox:
    name = "docker"
    isolation = "container"
    simulated = False

    def __init__(self, default_image: str, docker_binary: str = "docker") -> None:
        self.default_image = default_image
        self.docker = docker_binary
        self._availability: tuple[float, bool, str] | None = None

    def _probe(self) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                [self.docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=10, env=scrubbed_environ(), check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Docker CLI not available: {type(exc).__name__}"
        if result.returncode != 0:
            return False, "Docker daemon is not reachable (is Docker running?)"
        return True, f"Docker server {result.stdout.strip()}"

    async def available(self) -> tuple[bool, str]:
        now = time.monotonic()
        if self._availability and now - self._availability[0] < 30:
            return self._availability[1], self._availability[2]
        ok, reason = await asyncio.to_thread(self._probe)
        self._availability = (now, ok, reason)
        return ok, reason

    def build_command(self, workspace: Path, argv: list[str], limits: SandboxLimits, image: str,
                      container_name: str) -> list[str]:
        user = "1000:1000"
        if sys.platform != "win32" and os.getuid() != 0:
            user = f"{os.getuid()}:{os.getgid()}"  # match the workspace owner on Linux hosts
        return [
            self.docker, "run", "--rm", "--name", container_name,
            "--network", "bridge" if limits.network else "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",  # noqa: S108 - inside the container
            "--user", user,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", str(limits.cpus),
            "--memory", f"{limits.memory_mb}m",
            "--memory-swap", f"{limits.memory_mb}m",
            "--pids-limit", str(limits.pids_limit),
            "--ulimit", "nofile=1024:1024",
            "-v", f"{workspace.resolve()}:{CONTAINER_WORKDIR}:rw",
            "-w", CONTAINER_WORKDIR,
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PYTHONUNBUFFERED=1",
            "-e", f"PYTHONPATH={DEPS_DIR}",
            "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "-e", "PIP_NO_CACHE_DIR=1",
            "-e", "npm_config_cache=/tmp/.npm",
            "-e", "CI=true",
            "-e", f"PATH={DEPS_DIR}/bin:/usr/local/bin:/usr/bin:/bin",
            "--entrypoint", "",
            image,
            *argv,
        ]

    async def run(self, workspace: Path, argv: list[str], limits: SandboxLimits, *,
                  image: str | None = None) -> ExecutionResult:
        ok, reason = await self.available()
        if not ok:
            return ExecutionResult(exit_code=None, stdout="", stderr="", duration_ms=0, backend=self.name,
                                   error=reason)
        name = f"patchora-{uuid.uuid4().hex[:12]}"
        command = self.build_command(workspace, argv, limits, image or self.default_image, name)

        def _kill() -> None:
            subprocess.run([self.docker, "kill", name], capture_output=True, timeout=20,
                           env=scrubbed_environ(), check=False)

        result = await asyncio.to_thread(
            run_bounded, command, cwd=None, env=scrubbed_environ(),
            timeout=limits.timeout_seconds, max_output_bytes=limits.max_output_bytes,
            backend=self.name, on_timeout=_kill,
        )
        if result.exit_code == 125:  # docker itself failed (bad image, daemon error)
            result.error = "Docker could not start the container: " + result.stderr[-300:]
        return result
