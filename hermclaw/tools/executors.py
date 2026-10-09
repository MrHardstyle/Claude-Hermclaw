"""Command executors for the tool engine (P17 17.5; :class:`~hermclaw.core.interfaces.CommandExecutor`).

- :class:`LocalSandboxExecutor` runs the command through the execution sandbox
  (``worker.execution.sandbox.make_sandbox(policy).run(CommandRequest, workspace_dir)``, rootless Podman/Docker).
  The sandbox module is imported lazily. When it is not installed, the executor falls back to
  :class:`SubprocessExecutor` – **only** outside production (``settings.env != "production"``); production refuses.
- :class:`SubprocessExecutor` is the dev/test fallback: the command runs with ``bash -c`` in the workspace directory,
  in its own process group (killed as a whole on timeout), with an environment built from the sandbox env allow-list
  and bounded output capture. It provides **no** filesystem or network isolation and refuses to run in production.

``RemoteSandboxExecutor`` (execution worker .222 via WorkerClient + workspace sync) is added by the integrator.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from hermclaw.contracts.worker import CommandRequest, CommandResult
from hermclaw.core.config import SandboxPolicy
from hermclaw.core.errors import PolicyViolation
from hermclaw.core.interfaces import ExecutionRequest, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.settings import Settings, get_settings

log = get_logger(__name__)

SANDBOX_MODULE = "worker.execution.sandbox"
DEFAULT_MAX_OUTPUT_BYTES = 200_000


class SandboxUnavailable(PolicyViolation):
    """No isolated execution is possible for this environment (e.g. production without the sandbox)."""

    code = "SANDBOX_UNAVAILABLE"


class SandboxRunner(Protocol):
    async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult: ...


class _CappedBuffer:
    """Keeps the head and the tail of a stream (errors and test summaries are at the end)."""

    def __init__(self, limit: int) -> None:
        self.head_limit = max(0, limit // 4)
        self.tail_limit = max(0, limit - self.head_limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.head_limit - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)

    def text(self) -> str:
        head = self.head.decode("utf-8", errors="replace")
        tail = self.tail.decode("utf-8", errors="replace")
        if self.truncated:
            return f"{head}\n…[{self.total - len(self.head) - len(self.tail)} bytes omitted]…\n{tail}"
        return head + tail


class _CaptureProtocol(asyncio.SubprocessProtocol):
    """Streams stdout/stderr into capped buffers; signals process exit separately from pipe closure.

    (``Process.wait()`` only returns once every pipe is closed, which background children can delay forever.)
    """

    def __init__(self, out: _CappedBuffer, err: _CappedBuffer) -> None:
        self.out = out
        self.err = err
        self.exited = asyncio.Event()
        self.closed = asyncio.Event()

    def pipe_data_received(self, fd: int, data: bytes | str) -> None:
        chunk = data.encode() if isinstance(data, str) else data
        (self.out if fd == 1 else self.err).feed(chunk)

    def process_exited(self) -> None:
        self.exited.set()

    def connection_lost(self, exc: Exception | None) -> None:
        self.closed.set()


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


class SubprocessExecutor:
    """Unisolated local execution for development and tests only (refuses ``env == 'production'``)."""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        settings: Settings | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        shell: str | None = None,
    ) -> None:
        self.policy = policy or SandboxPolicy()
        self._settings = settings
        self.max_output_bytes = max_output_bytes
        self.shell = shell or shutil.which("bash") or "/bin/sh"

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    def _ensure_allowed(self) -> None:
        if self.settings.env == "production":
            raise SandboxUnavailable("unisolated subprocess execution is not permitted in production; the execution sandbox is required")

    def build_env(self, extra: dict[str, str]) -> dict[str, str]:
        env = {k: os.environ[k] for k in self.policy.env_allowlist if k in os.environ}
        env.setdefault("PATH", os.defpath)
        env.update(extra)
        return env

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        self._ensure_allowed()
        request_id = uuid.uuid4().hex
        cwd = Path(workspace.path)
        if not await asyncio.to_thread(cwd.is_dir):
            raise SandboxUnavailable(f"workspace directory does not exist: {cwd}")
        if req.network:
            log.debug("subprocess executor cannot restrict network access", extra={"request_id": request_id})
        out, err = _CappedBuffer(self.max_output_bytes), _CappedBuffer(self.max_output_bytes)
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        transport, proto = await loop.subprocess_exec(
            lambda: _CaptureProtocol(out, err),
            self.shell,
            "-c",
            req.command,
            cwd=str(cwd),
            env=self.build_env(dict(req.env)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            await asyncio.wait_for(proto.exited.wait(), timeout=req.timeout_seconds)
        except TimeoutError:
            timed_out = True
        finally:
            # nothing started by the command survives it (background children are part of the process group)
            _kill_group(transport.get_pid())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proto.exited.wait(), timeout=5)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proto.closed.wait(), timeout=2)
            transport.close()
        returncode = transport.get_returncode()
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandResult(
            request_id=request_id,
            exit_code=None if timed_out else returncode,
            timed_out=timed_out,
            stdout=out.text(),
            stderr=err.text(),
            stdout_truncated=out.truncated,
            stderr_truncated=err.truncated,
            duration_ms=duration_ms,
            sandbox="local",
        )


def sandbox_module_available() -> bool:
    try:
        return importlib.util.find_spec(SANDBOX_MODULE) is not None
    except (ImportError, ValueError):
        return False


class LocalSandboxExecutor:
    """Executes commands in the in-process execution sandbox (falls back to a subprocess outside production)."""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        settings: Settings | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        runner: SandboxRunner | None = None,
    ) -> None:
        self.policy = policy or SandboxPolicy()
        self._settings = settings
        self.max_output_bytes = max_output_bytes
        self._runner: SandboxRunner | None = runner
        self._fallback: SubprocessExecutor | None = None

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    def _resolve(self) -> SandboxRunner | SubprocessExecutor:
        if self._runner is not None:
            return self._runner
        if self._fallback is not None:
            return self._fallback
        if sandbox_module_available():
            module: Any = importlib.import_module(SANDBOX_MODULE)  # errors inside the module must surface
            self._runner = module.make_sandbox(self.policy, environment=self.settings.env, max_output_bytes=self.max_output_bytes)
            return self._runner  # type: ignore[return-value]
        if self.settings.env == "production":
            raise SandboxUnavailable(f"execution sandbox ({SANDBOX_MODULE}) is not installed; production refuses unisolated commands")
        log.warning("execution sandbox not installed; using unisolated subprocess fallback", extra={"env": self.settings.env})
        self._fallback = SubprocessExecutor(self.policy, settings=self._settings, max_output_bytes=self.max_output_bytes)
        return self._fallback

    @property
    def mode(self) -> str:
        """``sandbox`` or ``subprocess`` (resolves the backend)."""
        return "subprocess" if isinstance(self._resolve(), SubprocessExecutor) else "sandbox"

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        backend = self._resolve()
        if isinstance(backend, SubprocessExecutor):
            return await backend.run(workspace, req)
        creq = CommandRequest(
            request_id=uuid.uuid4().hex,
            job_id=str(req.job_id or workspace.job_id),
            step_id=str(req.step_id or ""),
            workspace=str(workspace.id),
            command=req.command,
            image=req.image,
            timeout_seconds=max(1, min(req.timeout_seconds, 7200)),
            network=req.network,
            env=dict(req.env),
        )
        return await backend.run(creq, Path(workspace.path))
