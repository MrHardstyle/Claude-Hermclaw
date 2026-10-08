"""Async git CLI runner with a hardened, deterministic environment (Bauplan §27).

* never prompts (``GIT_TERMINAL_PROMPT=0``, no askpass, ssh ``BatchMode=yes``)
* ignores system/global git config and hooks (``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL=/dev/null``,
  ``core.hooksPath=/dev/null``), no autocrlf, no gpg signing, literal pathspecs, ``LC_ALL=C``
* only plain transports (``GIT_ALLOW_PROTOCOL=file:ssh:https:http``) – ``ext::`` and friends are blocked
* optional ``GIT_SSH_COMMAND`` with a dedicated key and ``StrictHostKeyChecking=yes``
* hard timeout per command (process group killed), bounded output capture, redacted error details
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.gitops.errors import GitCommandError, GitTimeoutError
from hermclaw.gitops.urls import redact_url

log = get_logger(__name__)

GLOBAL_CONFIG: tuple[str, ...] = (
    "core.autocrlf=false",
    "core.safecrlf=false",
    "core.hooksPath=/dev/null",
    "core.quotePath=false",
    "core.fsmonitor=false",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "gc.autoDetach=false",
    "advice.detachedHead=false",
    "credential.helper=",
    "protocol.ext.allow=never",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
    "push.recurseSubmodules=no",
    "init.defaultBranch=main",
)
ALLOWED_PROTOCOLS = "file:ssh:https:http"
DEFAULT_STDERR_LIMIT = 64_000


@dataclass(frozen=True, slots=True)
class GitSshOptions:
    """SSH transport settings: dedicated key, pinned known_hosts, strict host key checking."""

    key_path: Path | None = None
    known_hosts: Path | None = None
    connect_timeout: int = 15

    def command(self) -> str:
        parts = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={int(self.connect_timeout)}",
        ]
        if self.key_path is not None:
            parts += ["-i", str(self.key_path), "-o", "IdentitiesOnly=yes"]
        if self.known_hosts is not None:
            parts += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        return " ".join(shlex.quote(p) for p in parts)


@dataclass(frozen=True, slots=True)
class GitResult:
    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    duration_ms: int

    @property
    def text(self) -> str:
        return os.fsdecode(self.stdout)

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    @property
    def first_line(self) -> str:
        return self.text.strip().splitlines()[0] if self.text.strip() else ""


def sanitize_args(args: Sequence[str]) -> list[str]:
    return [DEFAULT_REDACTOR.text(redact_url(a)) for a in args]


def sanitize_text(text: str, *, limit: int = 4000) -> str:
    clean = DEFAULT_REDACTOR.text(redact_url(text))
    return clean if len(clean) <= limit else clean[:limit] + f"…[{len(clean) - limit} chars truncated]"


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int | None) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    size = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if limit is None:
            chunks.append(chunk)
            continue
        if size < limit:
            take = chunk[: limit - size]
            chunks.append(take)
            size += len(take)
            if len(take) < len(chunk):
                truncated = True
        else:
            truncated = True  # keep draining so the process never blocks on a full pipe
    return b"".join(chunks), truncated


async def _feed(stdin: asyncio.StreamWriter | None, data: bytes | None) -> None:
    if stdin is None:
        return
    try:
        if data:
            stdin.write(data)
            await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await stdin.wait_closed()


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


class GitRunner:
    """Executes ``git`` with the hardened environment. Stateless apart from configuration."""

    def __init__(
        self,
        *,
        author_name: str,
        author_email: str,
        ssh: GitSshOptions | None = None,
        git_binary: str = "git",
        default_timeout: float = 120.0,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.git_binary = git_binary
        self.default_timeout = default_timeout
        self.author_name = author_name
        self.author_email = author_email
        self.ssh = ssh or GitSshOptions()
        self._extra_env = dict(extra_env or {})

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/nonexistent"),
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_ALLOW_PROTOCOL": ALLOWED_PROTOCOLS,
            "GIT_EDITOR": "true",
            "GIT_SEQUENCE_EDITOR": "true",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_COMMITTER_NAME": self.author_name,
            "GIT_COMMITTER_EMAIL": self.author_email,
            "GIT_SSH_COMMAND": self.ssh.command(),
        }
        if "TMPDIR" in os.environ:
            env["TMPDIR"] = os.environ["TMPDIR"]
        env.update(self._extra_env)
        if extra:
            env.update(extra)
        return env

    def argv(self, args: Sequence[str]) -> list[str]:
        out = [self.git_binary]
        for item in GLOBAL_CONFIG:
            out += ["-c", item]
        out += list(args)
        return out

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        input_data: bytes | None = None,
        timeout_s: float | None = None,
        ok_codes: Sequence[int] = (0,),
        check: bool = True,
        env: Mapping[str, str] | None = None,
        max_stdout: int | None = None,
    ) -> GitResult:
        """Run ``git <args>``; raise :class:`GitCommandError` unless the exit code is in ``ok_codes`` (or ``check=False``)."""
        argv = self.argv(args)
        limit = timeout_s if timeout_s is not None else self.default_timeout
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd) if cwd is not None else None,
                env=self.environment(env),
                stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise GitCommandError(
                f"git executable not found or cwd missing: {exc.filename}", details={"args": sanitize_args(args)}
            ) from exc
        io = asyncio.gather(
            _read_bounded(proc.stdout, max_stdout),
            _read_bounded(proc.stderr, DEFAULT_STDERR_LIMIT),
            _feed(proc.stdin, input_data),
            proc.wait(),
        )
        try:
            (stdout, truncated), (stderr, _), _, returncode = await asyncio.wait_for(io, timeout=limit)
        except TimeoutError as exc:
            _kill_group(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
            raise GitTimeoutError(
                f"git {args[0] if args else ''} timed out after {limit:.0f}s",
                details={"args": sanitize_args(args), "timeout_seconds": limit},
            ) from exc
        except asyncio.CancelledError:
            _kill_group(proc)
            raise
        result = GitResult(tuple(args), returncode, stdout, stderr, truncated, int((time.monotonic() - started) * 1000))
        log.debug(
            "git command",
            extra={"git_args": sanitize_args(args)[:6], "exit_code": returncode, "duration_ms": result.duration_ms},
        )
        if check and returncode not in ok_codes:
            stderr_text = sanitize_text(result.err.strip())
            raise GitCommandError(
                f"git {args[0] if args else ''} failed (exit {returncode}): {stderr_text.splitlines()[-1] if stderr_text else ''}",
                details={"args": sanitize_args(args), "exit_code": returncode, "stderr": stderr_text},
            )
        return result
