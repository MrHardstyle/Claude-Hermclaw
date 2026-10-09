"""Async subprocess helpers (argv only – never a shell) for git and ripgrep.

Git is used strictly read-only here: no command mutates refs, the index or the working tree
(``GIT_OPTIONAL_LOCKS=0`` keeps ``git status`` from refreshing/locking the index, ``core.fsmonitor`` and hooks
are disabled). Git mutations remain the job of the runtime-controlled Git engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from hermclaw.core.errors import HermclawError

_GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
}
_GIT_SAFE_OPTS = ("-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


class RepoCommandError(HermclawError):
    code = "REPO_COMMAND_FAILED"


class RepoCommandTimeout(RepoCommandError):
    code = "REPO_COMMAND_TIMEOUT"


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


@lru_cache(maxsize=16)
def which(binary: str) -> str | None:
    return shutil.which(binary)


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "USER")}
    env.update(_GIT_ENV)
    if extra:
        env.update(extra)
    return env


async def terminate(proc: asyncio.subprocess.Process, *, drain: bool = True) -> None:
    """Kill the process *group* (children such as shell-spawned helpers included), reap it and drain its pipes so
    the transport closes now and not when the event loop is already gone."""
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # started with start_new_session=True: pgid == pid
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5)
    if drain:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(stream.read(), timeout=2)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    await terminate(proc, drain=True)


async def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    max_output: int = 64_000_000,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
) -> ProcResult:
    """Run ``argv`` with a hard timeout; stdout beyond ``max_output`` is dropped (``truncated``)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(env),
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        raise RepoCommandError(f"cannot start {argv[0]}: {exc}", details={"argv0": argv[0]}) from exc

    async def _read_out() -> tuple[bytes, bool]:
        assert proc.stdout is not None
        buf = bytearray()
        truncated = False
        while True:
            block = await proc.stdout.read(1 << 16)
            if not block:
                break
            if len(buf) < max_output:
                buf.extend(block[: max_output - len(buf)])
                if len(buf) >= max_output:
                    truncated = True
            else:
                truncated = True
        return bytes(buf), truncated

    async def _read_err() -> bytes:
        assert proc.stderr is not None
        data = await proc.stderr.read(64_000)
        while await proc.stderr.read(1 << 16):  # drain, keep the head only
            pass
        return data

    async def _feed() -> None:
        if stdin is None or proc.stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()

    try:
        (out, truncated), err, _ = await asyncio.wait_for(asyncio.gather(_read_out(), _read_err(), _feed()), timeout=timeout_s)
        rc = await asyncio.wait_for(proc.wait(), timeout=max(1.0, timeout_s))
    except TimeoutError as exc:
        await _kill(proc)
        raise RepoCommandTimeout(f"{argv[0]} timed out after {timeout_s:.0f}s", details={"argv0": argv[0]}) from exc
    except BaseException:
        await _kill(proc)
        raise
    return ProcResult(rc, out, err, truncated)


def git_argv(git_binary: str, *args: str) -> list[str]:
    return [git_binary, *_GIT_SAFE_OPTS, *args]


async def git(root: Path, *args: str, timeout_s: float = 60.0, git_binary: str = "git", max_output: int = 64_000_000) -> ProcResult:
    return await run(git_argv(git_binary, *args), cwd=root, timeout_s=timeout_s, max_output=max_output)


async def git_ok(root: Path, *args: str, timeout_s: float = 60.0, git_binary: str = "git", max_output: int = 64_000_000) -> bytes:
    res = await git(root, *args, timeout_s=timeout_s, git_binary=git_binary, max_output=max_output)
    if not res.ok:
        msg = res.stderr.decode("utf-8", errors="replace").strip()[:500]
        raise RepoCommandError(f"git {args[0] if args else ''} failed: {msg}", details={"returncode": res.returncode})
    return res.stdout


async def is_git_repo(root: Path, *, timeout_s: float = 30.0, git_binary: str = "git") -> bool:
    if which(git_binary) is None:
        return False
    try:
        res = await git(root, "rev-parse", "--is-inside-work-tree", timeout_s=timeout_s, git_binary=git_binary)
    except RepoCommandError:
        return False
    if not res.ok or res.text().strip() != "true":
        return False
    # only the workspace root itself counts (a sub directory of an unrelated repo is not "this" repository)
    top = await git(root, "rev-parse", "--show-toplevel", timeout_s=timeout_s, git_binary=git_binary)
    if not top.ok:
        return False
    try:
        top_real, root_real = await asyncio.to_thread(lambda: (os.path.realpath(top.text().strip()), os.path.realpath(root)))
        return top_real == root_real
    except OSError:
        return False


async def head_sha(root: Path, *, timeout_s: float = 30.0, git_binary: str = "git") -> str | None:
    """Commit SHA of HEAD, ``None`` for an unborn branch or a non-repository."""
    try:
        res = await git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}", timeout_s=timeout_s, git_binary=git_binary)
    except RepoCommandError:
        return None
    sha = res.text().strip()
    return sha if res.ok and sha else None


async def commit_exists(root: Path, sha: str, *, timeout_s: float = 30.0, git_binary: str = "git") -> bool:
    if not sha or not all(c in "0123456789abcdef" for c in sha.lower()):
        return False
    try:
        res = await git(root, "cat-file", "-e", f"{sha}^{{commit}}", timeout_s=timeout_s, git_binary=git_binary)
    except RepoCommandError:
        return False
    return res.ok


def split_z(data: bytes) -> list[str]:
    """NUL-separated git output; entries that are not valid UTF-8 are dropped (they cannot be stored or quoted)."""
    out: list[str] = []
    for raw in data.split(b"\0"):
        if not raw:
            continue
        try:
            out.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return out
