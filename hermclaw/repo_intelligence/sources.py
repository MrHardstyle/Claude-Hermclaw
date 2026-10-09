"""File listings and content sources.

* :func:`list_worktree_files` – what is in the working tree now: tracked + untracked-but-not-ignored files
  (``git ls-files``), ``rg --files`` for non-git directories (honours ``.gitignore`` too), plain walk otherwise.
* :class:`GitTreeSource` – the exact committed tree of one SHA (``git ls-tree`` + ``git cat-file --batch``); the
  index of a SHA is built from this, never from uncommitted working-tree content.
* :class:`WorktreeSource` – the working tree (non-git workspaces).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from hermclaw.repo_intelligence import _proc
from hermclaw.repo_intelligence.config import RepoIntelConfig
from hermclaw.repo_intelligence.fileio import lstat_regular, read_bytes, walk_files


@dataclass(frozen=True)
class FileListing:
    paths: list[str]
    truncated: bool
    method: str  # git|ripgrep|walk


async def list_worktree_files(root: Path, cfg: RepoIntelConfig, *, git_repo: bool | None = None) -> FileListing:
    if git_repo is None:
        git_repo = await _proc.is_git_repo(root, timeout_s=cfg.git_timeout_seconds, git_binary=cfg.git_binary)
    paths: list[str] = []
    method = "walk"
    if git_repo:
        out = await _proc.git_ok(
            root,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            timeout_s=cfg.git_timeout_seconds,
            git_binary=cfg.git_binary,
            max_output=cfg.max_output_bytes,
        )
        paths = _proc.split_z(out)
        method = "git"
    elif _proc.which(cfg.rg_binary):
        res = await _proc.run(
            [cfg.rg_binary, "--files", "--hidden", "--no-require-git", "--no-config", "--null", "--glob", "!.git"],
            cwd=root,
            timeout_s=cfg.rg_timeout_seconds * 3,
            max_output=cfg.max_output_bytes,
        )
        if res.returncode in (0, 1):
            paths = [p.removeprefix("./") for p in _proc.split_z(res.stdout)]
            method = "ripgrep"
    if method == "walk":
        paths = await asyncio.to_thread(lambda: list(walk_files(root, cfg.walk_skip_dirs, cfg.max_files + 1)))
    uniq = sorted({p for p in paths if p and not p.startswith(".git/") and "/.git/" not in f"/{p}"})
    # deleted-but-still-indexed files and symlinks/special files are not part of the listing
    existing = await asyncio.to_thread(lambda: [p for p in uniq if lstat_regular(root, p) is not None])
    truncated = len(existing) > cfg.max_files
    return FileListing(existing[: cfg.max_files], truncated, method)


@dataclass(frozen=True)
class TreeEntry:
    path: str
    size: int
    blob: str | None = None  # git blob id (GitTreeSource)


class GitTreeSource:
    """Committed content of ``sha`` (regular files only; symlinks and submodules are skipped)."""

    kind = "git"

    def __init__(self, root: Path, sha: str, cfg: RepoIntelConfig) -> None:
        self.root = root
        self.sha = sha
        self.cfg = cfg

    async def entries(self) -> list[TreeEntry]:
        out = await _proc.git_ok(
            self.root,
            "ls-tree",
            "-r",
            "-l",
            "-z",
            "--full-tree",
            self.sha,
            timeout_s=self.cfg.git_timeout_seconds,
            git_binary=self.cfg.git_binary,
            max_output=self.cfg.max_output_bytes,
        )
        entries: list[TreeEntry] = []
        for item in _proc.split_z(out):
            meta, _, path = item.partition("\t")
            parts = meta.split()
            if len(parts) != 4 or parts[1] != "blob" or parts[0] not in ("100644", "100755"):
                continue
            try:
                size = int(parts[3])
            except ValueError:
                continue
            entries.append(TreeEntry(path=path, size=size, blob=parts[2]))
        entries.sort(key=lambda e: e.path)
        return entries[: self.cfg.max_files]

    async def read(self, entries: Sequence[TreeEntry]) -> AsyncIterator[tuple[TreeEntry, bytes]]:
        """Stream blob contents through one ``git cat-file --batch`` process (request/response, no deadlock)."""
        if not entries:
            return
        argv = _proc.git_argv(self.cfg.git_binary, "cat-file", "--batch")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_proc._env(),
        )
        assert proc.stdin is not None and proc.stdout is not None
        timeout = self.cfg.git_timeout_seconds
        try:
            for entry in entries:
                if entry.blob is None:
                    continue
                proc.stdin.write(entry.blob.encode("ascii") + b"\n")
                await asyncio.wait_for(proc.stdin.drain(), timeout)
                header = await asyncio.wait_for(proc.stdout.readline(), timeout)
                fields = header.decode("ascii", errors="replace").split()
                if len(fields) < 2 or fields[1] == "missing":
                    continue
                size = int(fields[2])
                data = await asyncio.wait_for(proc.stdout.readexactly(size + 1), timeout)
                yield entry, data[:-1]
        except TimeoutError as exc:
            raise _proc.RepoCommandTimeout("git cat-file timed out") from exc
        except asyncio.IncompleteReadError as exc:
            raise _proc.RepoCommandError("git cat-file ended unexpectedly") from exc
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()


class WorktreeSource:
    """Working-tree content (used for non-git workspaces)."""

    kind = "worktree"

    def __init__(self, root: Path, cfg: RepoIntelConfig) -> None:
        self.root = root
        self.cfg = cfg

    async def entries(self) -> list[TreeEntry]:
        listing = await list_worktree_files(self.root, self.cfg, git_repo=False)

        def _sizes() -> list[TreeEntry]:
            out: list[TreeEntry] = []
            for p in listing.paths:
                st = lstat_regular(self.root, p)
                if st is not None:
                    out.append(TreeEntry(path=p, size=st.st_size))
            return out

        return await asyncio.to_thread(_sizes)

    async def read(self, entries: Sequence[TreeEntry]) -> AsyncIterator[tuple[TreeEntry, bytes]]:
        for entry in entries:
            data = await asyncio.to_thread(read_bytes, self.root, entry.path, entry.size + 1)
            if data is not None:
                yield entry, data

    async def fingerprint(self, entries: Sequence[TreeEntry]) -> str:
        """Pseudo revision of a non-git tree: hash over (path, size, mtime)."""
        import hashlib

        def _fp() -> str:
            h = hashlib.sha256()
            for e in entries:
                try:
                    st = os.lstat(self.root / e.path)
                except OSError:
                    continue
                h.update(f"{e.path}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
            return "worktree:" + h.hexdigest()[:40]

        return await asyncio.to_thread(_fp)
