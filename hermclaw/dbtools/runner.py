"""External commands of the database tools (pg_dump, pg_restore, psql, migration commands).

Credentials are passed through the environment (``PGPASSWORD``, ``DATABASE_URL`` …), never through argv. Every
process gets a minimal environment (no inherited secrets of the orchestrator), a timeout that kills the whole process
group, and bounded output capture.

Migration commands of a *project* (``alembic upgrade``, ``manage.py migrate`` …) run project code. They go through a
:class:`MigrationCommandRunner`: :class:`ExecutorMigrationRunner` runs them via the sandbox ``CommandExecutor``
(production); :class:`LocalMigrationRunner` runs them as a local subprocess (development/tests only – refused when
``HERMCLAW_ENV=production``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from hermclaw.core.interfaces import CommandExecutor, ExecutionRequest, WorkspaceHandle
from hermclaw.core.settings import get_settings
from hermclaw.dbtools.errors import DB_SANDBOX_UNAVAILABLE, DbToolError

DEFAULT_OUTPUT_BYTES = 64_000
_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR")


@dataclass
class ProcessResult:
    argv: list[str]  # as executed (never contains credentials)
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str | None = None  # could not start (binary missing …)
    runner: str = "local"  # local | sandbox
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None

    @property
    def display(self) -> str:
        return shlex.join(self.argv)

    def summary(self, limit: int = 2000) -> str:
        text = (self.stderr.strip() or self.stdout.strip() or self.error or "")[-limit:]
        return text


def base_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for child processes: PATH/locale only, plus ``extra``."""
    env = {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PGCONNECT_TIMEOUT"] = env.get("PGCONNECT_TIMEOUT", "10")
    if extra:
        env.update(extra)
    return env


async def run_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float,  # noqa: ASYNC109 – kills the process group on expiry
    stdin_data: bytes | None = None,
    max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
) -> ProcessResult:
    """Run ``argv`` without a shell; kills the process group on timeout or cancellation."""
    args = [str(a) for a in argv]
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=base_env(env),
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        return ProcessResult(args, None, error=f"cannot start {Path(args[0]).name}: {exc.strerror or exc}", duration_ms=0)

    out_buf = bytearray()
    err_buf = bytearray()
    out_trunc = err_trunc = False

    async def pump(stream: asyncio.StreamReader | None, buf: bytearray) -> bool:
        truncated = False
        if stream is None:
            return False
        while chunk := await stream.read(65536):
            room = max_output_bytes - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
        return truncated

    async def feed() -> None:
        if proc.stdin is None or stdin_data is None:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.close()

    timed_out = False
    try:
        async with asyncio.timeout(timeout):
            _, out_trunc, err_trunc = await asyncio.gather(feed(), pump(proc.stdout, out_buf), pump(proc.stderr, err_buf))
            await proc.wait()
    except TimeoutError:
        timed_out = True
        _kill_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)
    except asyncio.CancelledError:
        _kill_group(proc)
        raise
    finally:
        if proc.returncode is None:
            _kill_group(proc)
    return ProcessResult(
        args,
        None if timed_out else proc.returncode,
        stdout=out_buf.decode("utf-8", errors="replace"),
        stderr=err_buf.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_ms=int((time.monotonic() - started) * 1000),
        stdout_truncated=out_trunc,
        stderr_truncated=err_trunc,
    )


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


@runtime_checkable
class MigrationCommandRunner(Protocol):
    """Runs one migration command of a project with the sandbox database in its environment."""

    name: str

    def sqlite_dir(self, project_root: Path) -> Path | None:
        """Directory for a SQLite sandbox file the command can reach (``None``: a private temp dir is fine)."""
        ...

    async def run(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float) -> ProcessResult: ...  # noqa: ASYNC109


class LocalMigrationRunner:
    """Local subprocess in the project directory (development and tests). Refused in production."""

    name = "local"

    def __init__(self, *, allow_production: bool = False) -> None:
        self.allow_production = allow_production

    def sqlite_dir(self, project_root: Path) -> Path | None:
        return None

    async def run(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float) -> ProcessResult:  # noqa: ASYNC109
        if get_settings().env == "production" and not self.allow_production:
            raise DbToolError(
                "project migration commands run only in the execution sandbox in production (no sandbox executor configured)",
                code=DB_SANDBOX_UNAVAILABLE,
            )
        return await run_process(argv, env=env, cwd=cwd, timeout=timeout)


class ExecutorMigrationRunner:
    """Runs the command through the sandbox ``CommandExecutor`` of the job workspace (network on: it reaches the DB).

    ``cwd`` must lie inside the workspace; the command is executed as ``cd <relative cwd> && <command>``. The executor
    syncs the workspace both ways, so a SQLite sandbox file placed below the workspace is visible to the command.
    """

    name = "sandbox"

    def __init__(
        self,
        executor: CommandExecutor,
        workspace: WorkspaceHandle,
        *,
        step_id: uuid.UUID | None = None,
        attempt_id: uuid.UUID | None = None,
        image: str | None = None,
    ) -> None:
        self.executor = executor
        self.workspace = workspace
        self.step_id = step_id
        self.attempt_id = attempt_id
        self.image = image

    def sqlite_dir(self, project_root: Path) -> Path | None:
        return self.workspace.path / ".hermclaw-dbsandbox"

    async def run(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float) -> ProcessResult:  # noqa: ASYNC109
        try:
            rel = await asyncio.to_thread(_relative_cwd, self.workspace.path, cwd)
        except ValueError:
            return ProcessResult(list(argv), None, error="migration command directory is outside the workspace", runner="sandbox")
        command = shlex.join(list(argv)) if rel in ("", ".") else f"cd {shlex.quote(rel)} && {shlex.join(list(argv))}"
        started = time.monotonic()
        req = ExecutionRequest(
            command=command,
            timeout_seconds=max(1, int(timeout)),
            network=True,
            env=dict(env),
            image=self.image,
            job_id=self.workspace.job_id,
            step_id=self.step_id,
            attempt_id=self.attempt_id,
            purpose="tool",
        )
        try:
            res = await self.executor.run(self.workspace, req)
        except Exception as exc:
            return ProcessResult(list(argv), None, error=f"sandbox executor failed: {exc}", runner="sandbox")
        return ProcessResult(
            list(argv),
            res.exit_code,
            stdout=res.stdout,
            stderr=res.stderr,
            timed_out=res.timed_out,
            duration_ms=res.duration_ms or int((time.monotonic() - started) * 1000),
            stdout_truncated=res.stdout_truncated,
            stderr_truncated=res.stderr_truncated,
            error=res.error,
            runner="sandbox",
        )


def _relative_cwd(root: Path, cwd: Path) -> str:
    return cwd.resolve().relative_to(root.resolve()).as_posix()  # ValueError: outside the workspace


__all__ = [
    "ExecutorMigrationRunner",
    "LocalMigrationRunner",
    "MigrationCommandRunner",
    "ProcessResult",
    "base_env",
    "run_process",
]
