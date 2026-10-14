"""SSH transport for the admin tools (P26 26.4/26.5/26.7).

:class:`SshRunner` runs exactly one command on one allow-listed host through the OpenSSH client::

    ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<file>
        -o IdentitiesOnly=yes -o ConnectTimeout=10 … -i <key> -p <port> <user>@<address> -- <command>

* ``-F /dev/null`` ignores user/system ssh_config (no ProxyCommand, LocalCommand, agent or forwarding
  surprises); agent, X11 and port forwarding, PTY allocation and host-key updates are switched off.
* The command is one argv element after ``--``; the destination is validated by the registry, so neither
  can be parsed as an ssh option. The host runs it with the login shell of the admin user (optionally
  behind the ``hermclaw-ssh-gate`` forced command).
* Timeout (26.7): the ssh client runs in its own process group and is killed (TERM, then KILL) when the
  timeout elapses or the caller is cancelled. A remote process that ignores the closed connection may
  outlive the session – the forced-command gate / ``timeout`` on the host bounds that side.
* stdout/stderr are read concurrently and capped at ``max_output_bytes`` each (the rest is drained and
  counted, never buffered), decoded leniently and redacted.

It satisfies :class:`hermclaw.wol.idle.RemoteCommandRunner` (``run(host_id, command, timeout)``), so the
idle-sleep hook uses the same allow-list, key handling and audit-free transport. Policy decisions
(approval, read-only) belong to :mod:`hermclaw.sshtools.tools`; the runner only refuses what is never
allowed (unknown/non-allow-listed hosts, ``forbidden`` commands).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.sshtools.errors import (
    SSH_CLIENT_MISSING,
    SSH_COMMAND_INVALID,
    SSH_TRANSPORT_EXIT,
    SshPolicyError,
    SshTimeoutError,
    SshTransportError,
)
from hermclaw.sshtools.keys import KeyManager
from hermclaw.sshtools.policy import SshCommandPolicy
from hermclaw.sshtools.registry import HostRegistry, SshHost

log = get_logger(__name__)

MAX_TIMEOUT_SECONDS = 6 * 3600
_READ_CHUNK = 65536


@dataclass(frozen=True)
class SshResult:
    host_id: str
    exit_code: int | None  # None when the command timed out
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def transport_error(self) -> bool:
        """OpenSSH reports its own failures (connect, auth, host key) with exit status 255."""
        return self.exit_code == SSH_TRANSPORT_EXIT


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, int]:
    if stream is None:
        return b"", 0
    kept = bytearray()
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        room = limit - len(kept)
        if room > 0:
            kept.extend(chunk[:room])
    return bytes(kept), total


async def _feed(stream: asyncio.StreamWriter | None, data: bytes | None) -> None:
    if stream is None:
        return
    try:
        if data:
            stream.write(data)
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the remote side does not read stdin
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            stream.close()


def _kill_group(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


class SshRunner:
    """Async OpenSSH runner bound to the host registry and key manager."""

    def __init__(
        self,
        registry: HostRegistry,
        keys: KeyManager,
        *,
        policy: SshCommandPolicy | None = None,
        default_timeout: float = 120.0,
        connect_timeout: int = 10,
        max_output_bytes: int = 200_000,
        max_sessions_per_host: int = 4,
        server_alive_interval: int = 15,
        kill_grace_seconds: float = 2.0,
        ssh_binary: str = "ssh",
        redactor: Redactor = DEFAULT_REDACTOR,
    ) -> None:
        if max_output_bytes < 1 or max_sessions_per_host < 1 or connect_timeout < 1:
            raise ValueError("max_output_bytes, max_sessions_per_host and connect_timeout must be positive")
        self.registry = registry
        self.keys = keys
        self.policy = policy
        self.default_timeout = float(default_timeout)
        self.connect_timeout = int(connect_timeout)
        self.max_output_bytes = int(max_output_bytes)
        self.server_alive_interval = int(server_alive_interval)
        self.kill_grace_seconds = float(kill_grace_seconds)
        self.ssh_binary = ssh_binary
        self.redactor = redactor
        self._max_sessions = max_sessions_per_host
        self._sessions: defaultdict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(self._max_sessions))

    # ------------------------------------------------------------------------------------------ argv
    def build_argv(self, host: SshHost, key_path: Path, command: str) -> list[str]:
        options = {
            "BatchMode": "yes",
            "StrictHostKeyChecking": "yes",
            "UserKnownHostsFile": str(host.known_hosts),
            "GlobalKnownHostsFile": "/dev/null",
            "IdentitiesOnly": "yes",
            "IdentityAgent": "none",
            "ConnectTimeout": str(self.connect_timeout),
            "PasswordAuthentication": "no",
            "KbdInteractiveAuthentication": "no",
            "PreferredAuthentications": "publickey",
            "ForwardAgent": "no",
            "ForwardX11": "no",
            "ClearAllForwardings": "yes",
            "PermitLocalCommand": "no",
            "RequestTTY": "no",
            "UpdateHostKeys": "no",
            "CheckHostIP": "no",
            "ControlMaster": "no",
            "ControlPath": "none",
            "ServerAliveInterval": str(self.server_alive_interval),
            "ServerAliveCountMax": "3",
            "LogLevel": "ERROR",
        }
        argv = [self.ssh_binary, "-F", "/dev/null"]
        for key, value in options.items():
            argv += ["-o", f"{key}={value}"]
        argv += ["-i", str(key_path), "-p", str(host.port), host.destination, "--", command]
        return argv

    @staticmethod
    def _env() -> dict[str, str]:
        """Minimal environment: no SSH_AUTH_SOCK, no inherited secrets."""
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/nonexistent"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    # ------------------------------------------------------------------------------------------ run
    def _check_command(self, host: SshHost, command: str, *, classify: bool = True) -> None:
        if not command.strip() or any(ch in command for ch in ("\x00", "\n", "\r")):
            raise SshPolicyError("remote command must be one non-empty line", code=SSH_COMMAND_INVALID)
        if classify and self.policy is not None:
            cls = self.policy.classify(host, command)
            if cls.kind == "forbidden":
                raise SshPolicyError(
                    f"command refused on '{host.id}': {cls.reason}", code=cls.code or "SSH_COMMAND_FORBIDDEN", details=cls.to_dict()
                )

    async def preflight(self, host_id: str) -> SshHost:
        """Host allow-list, pinned host key and usable identity – everything checkable without connecting."""
        host = self.registry.get(host_id)
        await self.keys.check_known_hosts(host)
        self.keys.check_identity(host.key_ref)
        return host

    async def execute(
        self,
        host_id: str,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 – the timeout is part of the runner contract
        stdin: bytes | None = None,
        builtin: bool = False,
    ) -> SshResult:
        """Run ``command`` on ``host_id``.

        ``builtin=True`` is reserved for commands the runtime itself assembled from validated parts (upload,
        service status) and already decided on; they skip the free-form classification (whose generic patterns
        could misread validated path names) but not the host allow-list or the single-line check.
        """
        host = self.registry.get(host_id)
        self._check_command(host, command, classify=not builtin)
        limit = float(timeout if timeout is not None else self.default_timeout)
        if not 0 < limit <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout must be within (0, {MAX_TIMEOUT_SECONDS}] seconds")
        await self.keys.check_known_hosts(host)
        async with self._sessions[host.id]:
            with self.keys.identity(host.key_ref) as key_path:
                return await self._spawn(host, self.build_argv(host, key_path, command), limit, stdin)

    async def run(
        self,
        host_id: str,
        command: str,
        timeout: float | None = None,  # noqa: ASYNC109 – fixed RemoteCommandRunner interface
        *,
        stdin: bytes | None = None,
    ) -> tuple[int, str, str]:
        """``(exit_code, stdout, stderr)``; raises :class:`SshTimeoutError` on timeout."""
        res = await self.execute(host_id, command, timeout=timeout, stdin=stdin)
        if res.timed_out or res.exit_code is None:
            limit = timeout if timeout is not None else self.default_timeout
            raise SshTimeoutError(f"ssh command on '{res.host_id}' timed out after {limit:g} s")
        return res.exit_code, res.stdout, res.stderr

    async def _spawn(self, host: SshHost, argv: list[str], timeout: float, stdin: bytes | None) -> SshResult:  # noqa: ASYNC109
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
                start_new_session=True,
            )
        except FileNotFoundError:
            raise SshTransportError(f"ssh client '{self.ssh_binary}' not found", code=SSH_CLIENT_MISSING) from None
        except OSError as exc:
            raise SshTransportError(f"ssh client could not be started: {exc.strerror}") from None

        out_task = asyncio.ensure_future(_read_capped(proc.stdout, self.max_output_bytes))
        err_task = asyncio.ensure_future(_read_capped(proc.stderr, self.max_output_bytes))
        feed_task = asyncio.ensure_future(_feed(proc.stdin, stdin))
        wait_task = asyncio.ensure_future(proc.wait())
        io_tasks = (out_task, err_task, feed_task)
        try:
            # asyncio.wait never cancels: on timeout the readers keep what they got until the pipes close
            await asyncio.wait({*io_tasks, wait_task}, timeout=timeout)
        except BaseException:  # cancelled by the caller: never leave an ssh client behind
            await self._terminate(proc)
            for task in (*io_tasks, wait_task):
                task.cancel()
            raise
        timed_out = not wait_task.done()
        if timed_out:
            await self._terminate(proc)
        if not all(t.done() for t in io_tasks):
            _done, pending = await asyncio.wait(io_tasks, timeout=self.kill_grace_seconds + 1)
            for leftover in pending:
                leftover.cancel()
        if not wait_task.done():
            wait_task.cancel()
        out, out_total = out_task.result() if out_task.done() and not out_task.cancelled() else (b"", 0)
        err, err_total = err_task.result() if err_task.done() and not err_task.cancelled() else (b"", 0)
        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = None if timed_out else proc.returncode
        if timed_out:
            log.warning("ssh command timed out", extra={"host": host.id, "timeout_s": timeout})
        return SshResult(
            host_id=host.id,
            exit_code=exit_code,
            stdout=self.redactor.text(out.decode("utf-8", errors="replace")),
            stderr=self.redactor.text(err.decode("utf-8", errors="replace")),
            timed_out=timed_out,
            duration_ms=duration_ms,
            stdout_bytes=out_total,
            stderr_bytes=err_total,
            stdout_truncated=out_total > len(out),
            stderr_truncated=err_total > len(err),
        )

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        _kill_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), self.kill_grace_seconds)
        except TimeoutError:
            _kill_group(proc.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(proc.wait()), self.kill_grace_seconds + 3)


__all__ = ["MAX_TIMEOUT_SECONDS", "SshResult", "SshRunner"]
