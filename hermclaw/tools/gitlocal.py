"""Minimal local git plumbing used by the tool engine on the orchestrator copy of a workspace.

Only these operations exist here, none of them touches HEAD, refs or the commit history:

- read-only queries: ``rev-parse``, ``ls-files``, ``status --porcelain`` (with ``GIT_OPTIONAL_LOCKS=0`` so status
  never rewrites the index), ``apply --numstat`` / ``apply --check``;
- working-tree mutations performed *by the runtime*: ``git apply`` (worktree only, never ``--index``) for the
  ``apply_patch`` tool and ``checkout-index -f`` to restore out-of-scope changes made by sandbox commands.

Commit/branch/push operations belong exclusively to ``hermclaw.gitops``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from hermclaw.core.logging import get_logger

log = get_logger(__name__)

_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


@dataclass(frozen=True)
class GitRun:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def err_text(self, limit: int = 4000) -> str:
        return (self.stderr or self.stdout).decode("utf-8", errors="replace").strip()[:limit]


def _git_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    return env


class LocalGit:
    """Async wrapper around the ``git`` binary for one working tree."""

    def __init__(self, root: Path, *, timeout_seconds: float = 120.0, binary: str = "git") -> None:
        self.root = root
        self.timeout_seconds = timeout_seconds
        self.binary = binary
        self._is_repo: bool | None = None

    async def run(self, *args: str, stdin: bytes | None = None, timeout_seconds: float | None = None) -> GitRun:
        cmd = [self.binary, "-c", "core.quotepath=off", "-c", "core.fsmonitor=false", *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.root),
                env=_git_env(),
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return GitRun(127, b"", b"git binary not found")
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout_seconds or self.timeout_seconds)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return GitRun(124, b"", f"git {args[0] if args else ''} timed out".encode())
        return GitRun(proc.returncode if proc.returncode is not None else 1, out, err)

    async def is_repo_root(self) -> bool:
        """True when ``root`` is the top level of a git working tree (paths are then repository-relative)."""
        if self._is_repo is None:
            res = await self.run("rev-parse", "--is-inside-work-tree", "--show-prefix")
            lines = res.stdout.decode("utf-8", errors="replace").splitlines() if res.ok else []
            self._is_repo = bool(lines) and lines[0].strip() == "true" and (len(lines) < 2 or lines[1].strip() == "")
        return self._is_repo

    async def git_dirs(self) -> list[Path]:
        """Absolute git dir and common dir (they differ for linked worktrees)."""
        res = await self.run("rev-parse", "--absolute-git-dir", "--git-common-dir")
        if not res.ok:
            return []
        out: list[Path] = []
        for raw in res.stdout.decode("utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            p = Path(line)
            if not p.is_absolute():
                p = self.root / p
            p = p.resolve()
            if p not in out:
                out.append(p)
        return out

    async def list_files(self) -> list[str]:
        """Tracked + untracked, not ignored files (repository-relative POSIX paths)."""
        res = await self.run("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        if not res.ok:
            raise RuntimeError(f"git ls-files failed: {res.err_text()}")
        seen: dict[str, None] = {}
        for raw in res.stdout.split(b"\0"):
            if raw:
                seen.setdefault(raw.decode("utf-8", errors="surrogateescape"), None)
        return list(seen)

    async def tracked_files(self) -> set[str]:
        res = await self.run("ls-files", "-z", "--cached")
        if not res.ok:
            raise RuntimeError(f"git ls-files failed: {res.err_text()}")
        return {raw.decode("utf-8", errors="surrogateescape") for raw in res.stdout.split(b"\0") if raw}

    async def tree_files(self, rev: str) -> set[str] | None:
        """All file paths of commit ``rev`` (``None`` if ``rev`` is unknown or this is not a repository)."""
        if not rev or rev.startswith("-"):
            return None
        res = await self.run("ls-tree", "-r", "-z", "--name-only", "--full-tree", f"{rev}^{{tree}}")
        if not res.ok:
            return None
        return {raw.decode("utf-8", errors="surrogateescape") for raw in res.stdout.split(b"\0") if raw}

    async def worktree_dirty(self) -> dict[str, str]:
        """Paths whose working-tree content differs from the index (``XY`` with ``Y != ' '``) or untracked."""
        res = await self.run("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--ignore-submodules=all")
        if not res.ok:
            raise RuntimeError(f"git status failed: {res.err_text()}")
        dirty: dict[str, str] = {}
        for raw in res.stdout.split(b"\0"):
            if len(raw) < 4:
                continue
            code = raw[:2].decode("ascii", errors="replace")
            path = raw[3:].decode("utf-8", errors="surrogateescape")
            if code == "??" or code[1] != " ":
                dirty[path] = code
        return dirty

    async def restore_from_index(self, paths: list[str]) -> GitRun:
        """Rewrite working-tree files from the index (no HEAD/ref/index content change)."""
        if not paths:
            return GitRun(0, b"", b"")
        return await self.run("checkout-index", "-f", "-q", "-z", "--stdin", stdin=b"\0".join(p.encode() for p in paths) + b"\0")

    async def apply_numstat(self, patch: bytes, strip: int) -> GitRun:
        return await self.run("apply", f"-p{strip}", "--recount", "--numstat", "-z", "-", stdin=patch)

    async def apply_check(self, patch: bytes, strip: int) -> GitRun:
        return await self.run("apply", f"-p{strip}", "--recount", "--check", "--whitespace=nowarn", "-", stdin=patch)

    async def apply(self, patch: bytes, strip: int) -> GitRun:
        return await self.run("apply", f"-p{strip}", "--recount", "--whitespace=nowarn", "-", stdin=patch)
