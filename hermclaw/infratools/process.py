"""Command execution for the container/proxy tools: local subprocesses or a registered host through a remote runner.

Every command is an argv list that the tools assembled from validated parts – never a shell string from the model.

* ``host`` ``None`` / ``"local"``: the argv runs as a local subprocess **without a shell**, with a minimal environment
  (PATH/locale plus the engine variables ``DOCKER_HOST``, ``CONTAINER_HOST``, proxies …), in its own process group;
  on timeout or cancellation the whole group is killed.
* any other host id must be configured in ``hosts.yaml``; the argv is joined with :func:`shlex.join` (every word
  quoted) and handed to the injected :class:`RemoteRunner` (production: ``hermclaw.sshtools.client.SshRunner`` – host
  allow-list, pinned host keys, command classification). The remote command is therefore always one line without
  shell operators.

Each run can be audited as a ``command_runs`` row (target ``local`` | ``ssh``; redacted command and output excerpts).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import os
import shlex
import signal
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import HostsConfig
from hermclaw.core.errors import HermclawError
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.infratools.errors import (
    INFRA_COMMAND_FAILED,
    INFRA_ENGINE_UNAVAILABLE,
    INFRA_HOST_UNKNOWN,
    INFRA_REMOTE_UNAVAILABLE,
    INFRA_TIMEOUT,
    InfraToolError,
)
from hermclaw.persistence.models import CommandRun

log = get_logger(__name__)

LOCAL_HOST = "local"
DEFAULT_OUTPUT_BYTES = 1_000_000
EXCERPT_CHARS = 4000
MAX_TIMEOUT_SECONDS = 3 * 3600

#: environment variables passed to local engine/nginx processes (nothing else is inherited)
PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "CONTAINER_HOST",
    "CONTAINER_CONNECTION",
    "CONTAINERS_CONF",
    "CONTAINERS_REGISTRIES_CONF",
    "REGISTRY_AUTH_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

Classification = Literal["read", "mutate", "destructive"]


class RemoteRunner(Protocol):
    """Runs one command line on a configured host and returns ``(exit_code, stdout, stderr)``.

    The production implementation is ``hermclaw.sshtools.client.SshRunner``; it raises on transport errors and
    timeouts. Runners whose ``run`` accepts a keyword ``stdin: bytes | None`` can also stage files (compose sandbox,
    rollback restore on remote hosts).
    """

    async def run(self, host_id: str, command: str, timeout: float) -> tuple[int, str, str]: ...  # noqa: ASYNC109


@dataclass(frozen=True)
class InfraContext:
    """Who triggered a tool call – stored on audit rows, artifacts and deployments."""

    job_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    actor: str = "agent"


@dataclass(frozen=True)
class ExecResult:
    host: str
    display: str  # redacted command line
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    error: str | None = None  # could not start / transport failure
    error_code: str | None = None
    truncated: bool = False
    raw: bool = False  # stdout is unredacted file content (never audited)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None

    @property
    def missing_binary(self) -> bool:
        """The program does not exist on the host (local ENOENT or the shell's 127)."""
        return self.error_code == INFRA_ENGINE_UNAVAILABLE or (self.exit_code == 127 and self.error is None)

    def describe(self, limit: int = 600) -> str:
        """Short human-readable failure description (already redacted)."""
        if self.error:
            return self.error
        if self.timed_out:
            return f"'{self.display}' timed out after {self.duration_ms / 1000:.1f} s"
        out = DEFAULT_REDACTOR.text(self.stdout) if self.raw else self.stdout  # raw output is unredacted file/JSON content
        text = (self.stderr.strip() or out.strip())[-limit:]
        return f"'{self.display}' exited with {self.exit_code}: {text}" if text else f"'{self.display}' exited with {self.exit_code}"


def base_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env["LC_ALL"] = "C.UTF-8"
    if extra:
        env.update(extra)
    return env


def _kill_group(proc: asyncio.subprocess.Process, sig: signal.Signals = signal.SIGKILL) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, sig)


async def run_local(
    argv: Sequence[str],
    *,
    timeout: float,  # noqa: ASYNC109 – kills the process group on expiry
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
    redactor: Redactor = DEFAULT_REDACTOR,
    raw_stdout: bool = False,
) -> ExecResult:
    """Run ``argv`` without a shell; output is capped, decoded and redacted.

    ``raw_stdout`` keeps stdout unredacted – only for reading file content that is checksummed afterwards (rollback
    artifacts); such output is never written to the audit trail.
    """
    args = [str(a) for a in argv]
    display = redactor.text(shlex.join(args))
    if not args:
        raise InfraToolError("empty command", code=INFRA_COMMAND_FAILED)
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise InfraToolError(f"timeout must be within (0, {MAX_TIMEOUT_SECONDS}] seconds", code=INFRA_COMMAND_FAILED)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=base_env(env),
            start_new_session=True,
        )
    except FileNotFoundError:
        return ExecResult(LOCAL_HOST, display, None, error=f"'{Path(args[0]).name}' is not installed", error_code=INFRA_ENGINE_UNAVAILABLE)
    except (PermissionError, NotADirectoryError, OSError) as exc:
        return ExecResult(LOCAL_HOST, display, None, error=f"cannot start '{Path(args[0]).name}': {exc}", error_code=INFRA_COMMAND_FAILED)

    out_buf = bytearray()
    err_buf = bytearray()

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

    async def feed() -> bool:
        if proc.stdin is None or stdin is None:
            return False
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.close()
        return False

    timed_out = False
    truncated = False
    try:
        async with asyncio.timeout(timeout):
            _, t_out, t_err = await asyncio.gather(feed(), pump(proc.stdout, out_buf), pump(proc.stderr, err_buf))
            truncated = t_out or t_err
            await proc.wait()
    except TimeoutError:
        timed_out = True
        _kill_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)
    except BaseException:
        _kill_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(proc.wait()), 5)
        raise
    finally:
        if proc.returncode is None:
            _kill_group(proc)
    return ExecResult(
        LOCAL_HOST,
        display,
        None if timed_out else proc.returncode,
        stdout=out_buf.decode("utf-8", errors="replace") if raw_stdout else redactor.text(out_buf.decode("utf-8", errors="replace")),
        stderr=redactor.text(err_buf.decode("utf-8", errors="replace")),
        timed_out=timed_out,
        duration_ms=int((time.monotonic() - started) * 1000),
        error_code=INFRA_TIMEOUT if timed_out else None,
        truncated=truncated,
    )


def accepts_stdin(runner: object) -> bool:
    """True when ``runner.run`` takes a ``stdin`` keyword (needed to stage files on a remote host)."""
    run = getattr(runner, "run", None)
    if run is None:
        return False
    try:
        params = inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False
    return "stdin" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class LocalRunner:
    """:class:`RemoteRunner` that executes the command line on *this* machine (``shlex.split``, no shell).

    Used for development/test setups where the "remote" host is the local machine, and by the tests to drive the
    remote code paths (quoting, staging, rollback restore) against real processes.
    """

    def __init__(self, *, max_output_bytes: int = DEFAULT_OUTPUT_BYTES) -> None:
        self.max_output_bytes = max_output_bytes
        self.commands: list[str] = []

    async def run(self, host_id: str, command: str, timeout: float, *, stdin: bytes | None = None) -> tuple[int, str, str]:  # noqa: ASYNC109
        self.commands.append(command)
        res = await run_local(shlex.split(command), timeout=timeout, stdin=stdin, max_output_bytes=self.max_output_bytes)
        if res.timed_out:
            raise InfraToolError(f"command on '{host_id}' timed out after {timeout:g} s", code=INFRA_TIMEOUT)
        if res.error:
            return 127, "", res.error
        return int(res.exit_code or 0), res.stdout, res.stderr


class CommandAudit:
    """Writes one ``command_runs`` row per executed command (redacted)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], *, redactor: Redactor = DEFAULT_REDACTOR) -> None:
        self.sessionmaker = sessionmaker
        self.redactor = redactor

    async def record(self, res: ExecResult, ctx: InfraContext, classification: Classification) -> None:
        try:
            async with self.sessionmaker() as session, session.begin():
                session.add(
                    CommandRun(
                        job_id=ctx.job_id,
                        step_id=ctx.step_id,
                        attempt_id=ctx.attempt_id,
                        worker_id=res.host[:100],
                        target="local" if res.host == LOCAL_HOST else "ssh",
                        command=self.redactor.text(res.display)[:8000],
                        classification=classification,
                        exit_code=res.exit_code,
                        timed_out=res.timed_out,
                        network=False,
                        stdout_excerpt=None if res.raw else (self.redactor.text(res.stdout[-EXCERPT_CHARS:]) or None),
                        stderr_excerpt=self.redactor.text((res.error or res.stderr)[-EXCERPT_CHARS:]) or None,
                        duration_ms=res.duration_ms,
                    )
                )
        except Exception as exc:  # auditing must never break the tool itself
            log.warning("command audit failed", extra={"error": str(exc)[:300]})


@dataclass
class HostExecutor:
    """Routes argv commands to the local machine or a configured host."""

    remote: RemoteRunner | None = None
    hosts: HostsConfig | None = None
    redactor: Redactor = DEFAULT_REDACTOR
    audit: CommandAudit | None = None
    env: dict[str, str] = field(default_factory=dict)
    max_output_bytes: int = DEFAULT_OUTPUT_BYTES

    @staticmethod
    def is_local(host: str | None) -> bool:
        return host is None or host == LOCAL_HOST

    @property
    def remote_stdin(self) -> bool:
        return self.remote is not None and accepts_stdin(self.remote)

    def check_host(self, host: str | None) -> str:
        """Normalised host id; remote hosts must be configured and need a runner."""
        if self.is_local(host):
            return LOCAL_HOST
        assert host is not None
        if self.hosts is None:
            raise InfraToolError(f"host '{host}' cannot be checked: no host configuration loaded", code=INFRA_HOST_UNKNOWN)
        try:
            self.hosts.by_id(host)
        except HermclawError:
            known = ", ".join(h.id for h in self.hosts.hosts)
            raise InfraToolError(f"unknown host '{host}' (configured: {known})", code=INFRA_HOST_UNKNOWN) from None
        if self.remote is None:
            raise InfraToolError(f"no remote runner configured – host '{host}' is not reachable", code=INFRA_REMOTE_UNAVAILABLE)
        return host

    async def run(
        self,
        argv: Sequence[str],
        *,
        host: str | None = None,
        timeout: float = 60.0,  # noqa: ASYNC109
        cwd: Path | None = None,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        classification: Classification = "read",
        ctx: InfraContext | None = None,
        audit: bool = True,
        raw_stdout: bool = False,
    ) -> ExecResult:
        target = self.check_host(host)
        if target == LOCAL_HOST:
            merged = {**self.env, **(env or {})}
            res = await run_local(
                argv,
                timeout=timeout,
                cwd=cwd,
                env=merged,
                stdin=stdin,
                max_output_bytes=self.max_output_bytes,
                redactor=self.redactor,
                raw_stdout=raw_stdout,
            )
        else:
            res = await self._run_remote(target, argv, timeout=timeout, cwd=cwd, stdin=stdin, env=env, raw_stdout=raw_stdout)
        if raw_stdout:
            res = dataclasses.replace(res, raw=True)
        if audit and self.audit is not None:
            await self.audit.record(res, ctx or InfraContext(), classification)
        return res

    async def _run_remote(
        self,
        host: str,
        argv: Sequence[str],
        *,
        timeout: float,  # noqa: ASYNC109
        cwd: Path | None,
        stdin: bytes | None,
        env: Mapping[str, str] | None,
        raw_stdout: bool = False,
    ) -> ExecResult:
        if cwd is not None or env:
            raise InfraToolError("remote commands cannot set a working directory or environment", code=INFRA_COMMAND_FAILED)
        command = shlex.join([str(a) for a in argv])
        display = self.redactor.text(command)
        if any(ch in command for ch in ("\n", "\r", "\x00")):
            raise InfraToolError("remote command must be a single line", code=INFRA_COMMAND_FAILED)
        assert self.remote is not None
        started = time.monotonic()
        try:
            if stdin is not None:
                if not self.remote_stdin:
                    raise InfraToolError("the remote runner cannot send data (stdin)", code=INFRA_REMOTE_UNAVAILABLE)
                run: Any = self.remote.run
                rc, out, err = await run(host, command, timeout, stdin=stdin)
            else:
                rc, out, err = await self.remote.run(host, command, timeout)
        except InfraToolError as exc:
            timed_out = exc.code == INFRA_TIMEOUT
            if exc.code == INFRA_REMOTE_UNAVAILABLE:
                raise
            return ExecResult(
                host,
                display,
                None,
                timed_out=timed_out,
                error=self.redactor.text(exc.message),
                error_code=exc.code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except (HermclawError, TimeoutError, OSError) as exc:
            code = getattr(exc, "code", "") or ""
            timed_out = isinstance(exc, TimeoutError) or "TIMEOUT" in str(code).upper() or "Timeout" in type(exc).__name__
            return ExecResult(
                host,
                display,
                None,
                timed_out=timed_out,
                error=self.redactor.text(f"{type(exc).__name__}: {exc}")[:2000],
                error_code=INFRA_TIMEOUT if timed_out else INFRA_COMMAND_FAILED,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return ExecResult(
            host,
            display,
            int(rc),
            stdout=out if raw_stdout else self.redactor.text(out),
            stderr=self.redactor.text(err),
            duration_ms=int((time.monotonic() - started) * 1000),
        )


__all__ = [
    "DEFAULT_OUTPUT_BYTES",
    "LOCAL_HOST",
    "PASSTHROUGH_ENV",
    "Classification",
    "CommandAudit",
    "ExecResult",
    "HostExecutor",
    "InfraContext",
    "LocalRunner",
    "RemoteRunner",
    "accepts_stdin",
    "base_env",
    "run_local",
]
