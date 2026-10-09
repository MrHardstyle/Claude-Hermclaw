"""Detect and revert file changes made by sandbox commands (P17 17.5 – command side effects).

Before a command the tracker records a cheap snapshot of the workspace:

- ``lstat`` of every candidate file (git: tracked + untracked-not-ignored; generated caches such as
  ``__pycache__`` are skipped unless tracked),
- byte backups of every file whose working-tree content differs from the index (dirty or untracked) – those cannot
  be restored from git; clean tracked files are restored with ``git checkout-index`` (index unchanged),
- a fingerprint and backup of security-relevant git metadata (``HEAD``, ``config``, index entries, refs,
  ``packed-refs``, ``info/``, ``hooks/``) – a command must never commit, move refs, install hooks or change config.

After the command, changes are classified as ``create`` / ``modify`` / ``delete`` and checked with a decision
callback (the step's :class:`~hermclaw.scope.guard.ScopeGuard`). Disallowed changes are reverted (new files removed,
previous content restored) and reported. Detection is repeated (max. 3 rounds) so a command cannot hide a file by
editing ``.gitignore``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hermclaw.contracts.scope import Operation
from hermclaw.core.logging import get_logger
from hermclaw.scope.guard import any_match
from hermclaw.tools.gitlocal import LocalGit
from hermclaw.tools.workspace import WorkspaceFS, has_git_segment

log = get_logger(__name__)

DEFAULT_GENERATED_GLOBS: tuple[str, ...] = (
    "**/__pycache__/**",
    "**/*.pyc",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.tox/**",
    "**/.coverage",
    "**/node_modules/**",
    "**/.hypothesis/**",
)
GIT_META_FILES = ("HEAD", "config", "packed-refs", "shallow", "commondir")
GIT_META_DIRS = ("refs", "info", "hooks")
INDEX_KEY = "index"

Decide = Callable[[str, Operation], tuple[bool, str]]


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int
    mode: int
    ino: int
    link: str | None  # symlink target


@dataclass(frozen=True)
class Backup:
    data: bytes
    mode: int
    link: str | None = None


@dataclass
class GitMeta:
    dirs: list[Path]
    files: dict[str, bytes | str]  # absolute path -> bytes (restorable) or sha256 hex (too large)
    index_entries: str | None  # sha256 of `git ls-files -s`
    index_backups: dict[str, bytes]


@dataclass
class WorkspaceSnapshot:
    git_mode: bool
    files: dict[str, FileState]
    tracked: set[str]
    dirty: set[str]
    backups: dict[str, Backup]
    unbackupable: set[str]
    dirs: set[str]
    git_meta: GitMeta | None
    nested_repos: set[str] = field(default_factory=set)  # untracked embedded repositories ("dir/")


@dataclass(frozen=True)
class DetectedChange:
    path: str
    operation: Operation


@dataclass
class Violation:
    path: str
    operation: Operation
    reason: str
    reverted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "operation": self.operation, "reason": self.reason, "reverted": self.reverted}


@dataclass
class AuditOutcome:
    allowed: list[DetectedChange] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.violations


def _lstat(path: Path) -> FileState | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return None
    link = os.readlink(path) if stat.S_ISLNK(st.st_mode) else None
    return FileState(st.st_size, st.st_mtime_ns, st.st_mode, st.st_ino, link)


def _parent_dirs(paths: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for p in paths:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            out.add("/".join(parts[:i]))
    return out


class WorkspaceTracker:
    def __init__(
        self,
        fs: WorkspaceFS,
        git: LocalGit,
        *,
        generated_globs: Sequence[str] = DEFAULT_GENERATED_GLOBS,
        max_backup_bytes: int = 64 * 1024 * 1024,
        max_file_backup_bytes: int = 8 * 1024 * 1024,
        max_rounds: int = 3,
    ) -> None:
        self.fs = fs
        self.git = git
        self.generated_globs = list(dict.fromkeys(generated_globs))
        self.max_backup_bytes = max_backup_bytes
        self.max_file_backup_bytes = max_file_backup_bytes
        self.max_rounds = max_rounds

    # ------------------------------------------------------------------------------------------- snapshot
    async def _inventory(self, git_mode: bool) -> tuple[list[str], set[str], set[str]]:
        if git_mode:
            files = await self.git.list_files()
            tracked = await self.git.tracked_files()
        else:
            files = await asyncio.to_thread(self.fs.walk_files)
            tracked = set()
        nested = {f.rstrip("/") for f in files if f.endswith("/")}
        keep = [f for f in files if not f.endswith("/") and (f in tracked or not any_match(f, self.generated_globs))]
        return keep, tracked, nested

    async def snapshot(self) -> WorkspaceSnapshot:
        git_mode = await self.git.is_repo_root()
        files, tracked, nested = await self._inventory(git_mode)
        dirty = set((await self.git.worktree_dirty()).keys()) if git_mode else set(files)
        states, backups, unbackupable = await asyncio.to_thread(self._stat_and_backup, files, dirty)
        meta = await self._git_meta() if git_mode else None
        return WorkspaceSnapshot(git_mode, states, tracked, dirty, backups, unbackupable, _parent_dirs(files), meta, nested)

    def _stat_and_backup(self, files: list[str], dirty: set[str]) -> tuple[dict[str, FileState], dict[str, Backup], set[str]]:
        states: dict[str, FileState] = {}
        backups: dict[str, Backup] = {}
        unbackupable: set[str] = set()
        budget = self.max_backup_bytes
        for rel in files:
            st = _lstat(self.fs.root / rel)
            if st is None:
                continue
            states[rel] = st
            if rel not in dirty:
                continue
            if st.link is not None:
                backups[rel] = Backup(b"", st.mode, st.link)
                continue
            if st.size > self.max_file_backup_bytes or st.size > budget:
                unbackupable.add(rel)
                continue
            try:
                data = (self.fs.root / rel).read_bytes()
            except OSError:
                unbackupable.add(rel)
                continue
            budget -= len(data)
            backups[rel] = Backup(data, stat.S_IMODE(st.mode))
        return states, backups, unbackupable

    async def _git_meta(self) -> GitMeta:
        dirs = await self.git.git_dirs()
        files = await asyncio.to_thread(self._read_meta_files, dirs)
        index_backups: dict[str, bytes] = {}
        for d in dirs:
            idx = d / INDEX_KEY
            if idx.is_file() and idx.stat().st_size <= self.max_backup_bytes:
                index_backups[str(idx)] = await asyncio.to_thread(idx.read_bytes)
        return GitMeta(dirs, files, await self._index_fingerprint(), index_backups)

    async def _index_fingerprint(self) -> str | None:
        res = await self.git.run("ls-files", "-s", "-z")
        return hashlib.sha256(res.stdout).hexdigest() if res.ok else None

    def _read_meta_files(self, dirs: list[Path]) -> dict[str, bytes | str]:
        out: dict[str, bytes | str] = {}
        for d in dirs:
            candidates = [d / n for n in GIT_META_FILES]
            for sub in GIT_META_DIRS:
                base = d / sub
                if base.is_dir():
                    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
                        candidates.extend(Path(dirpath) / f for f in filenames)
            for p in candidates:
                try:
                    if not p.is_file() or p.is_symlink():
                        continue
                    data = p.read_bytes()
                except OSError:
                    continue
                out[str(p)] = data if len(data) <= self.max_file_backup_bytes else hashlib.sha256(data).hexdigest()
        return out

    # ------------------------------------------------------------------------------------------- detection
    async def detect(self, before: WorkspaceSnapshot) -> list[DetectedChange]:
        files, _tracked, nested = await self._inventory(before.git_mode)
        candidates = list(dict.fromkeys([*before.files.keys(), *files]))
        dirty_after = set((await self.git.worktree_dirty()).keys()) if before.git_mode else set()
        changes = await asyncio.to_thread(self._compare, before, candidates, dirty_after)
        # a new embedded repository is reported as its ``.git`` entry (always refused, removed as a whole)
        changes.extend(DetectedChange(f"{repo}/.git", "create") for repo in sorted(nested - before.nested_repos))
        return changes

    def _compare(self, before: WorkspaceSnapshot, candidates: list[str], dirty_after: set[str]) -> list[DetectedChange]:
        changes: list[DetectedChange] = []
        for rel in candidates:
            old = before.files.get(rel)
            new = _lstat(self.fs.root / rel)
            if old is None and new is None:
                continue
            if old is None:
                changes.append(DetectedChange(rel, "create"))
            elif new is None:
                changes.append(DetectedChange(rel, "delete"))
            elif old != new and self._content_changed(before, rel, new, dirty_after):
                changes.append(DetectedChange(rel, "modify"))
        return changes

    def _content_changed(self, before: WorkspaceSnapshot, rel: str, new: FileState, dirty_after: set[str]) -> bool:
        backup = before.backups.get(rel)
        if backup is not None:
            if backup.link is not None or new.link is not None:
                return backup.link != new.link
            if backup.mode != stat.S_IMODE(new.mode):
                return True
            try:
                return (self.fs.root / rel).read_bytes() != backup.data
            except OSError:
                return True
        if before.git_mode and rel in before.tracked and rel not in before.dirty:
            return rel in dirty_after  # git compares content when the stat data differs
        return True

    async def git_meta_changes(self, before: WorkspaceSnapshot) -> list[tuple[str, Operation]]:
        if before.git_meta is None:
            return []
        now = await asyncio.to_thread(self._read_meta_files, before.git_meta.dirs)
        out: list[tuple[str, Operation]] = []
        for path, old in before.git_meta.files.items():
            if path not in now:
                out.append((path, "delete"))
            elif now[path] != old:
                out.append((path, "modify"))
        out.extend((path, "create") for path in now if path not in before.git_meta.files)
        meta = before.git_meta
        if meta.index_entries is not None and await self._index_fingerprint() != meta.index_entries:
            index_paths = list(meta.index_backups) or [str(d / INDEX_KEY) for d in meta.dirs[:1]]
            out.extend((p, "modify") for p in index_paths)
        return out

    # ------------------------------------------------------------------------------------------- audit + revert
    def _judge(self, change: DetectedChange, decide: Decide) -> tuple[bool, str]:
        if has_git_segment(change.path):
            return False, "git metadata inside the workspace may not be changed"
        if change.operation != "delete":
            target = self.fs.root / change.path
            if target.is_symlink():
                real = Path(os.path.realpath(target))
                if not (real == self.fs.root or real.is_relative_to(self.fs.root)):
                    return False, "symlink points outside the workspace"
        return decide(change.path, change.operation)

    async def audit(self, before: WorkspaceSnapshot, decide: Decide) -> AuditOutcome:
        """Detect changes since ``before``; revert every change ``decide`` refuses. Returns surviving changes."""
        outcome = AuditOutcome()
        reported: dict[str, Violation] = {}
        changes: list[DetectedChange] = []
        for _round in range(self.max_rounds):
            changes = await self.detect(before)
            bad: list[tuple[DetectedChange, str]] = []
            for ch in changes:
                ok, reason = self._judge(ch, decide)
                if not ok:
                    bad.append((ch, reason))
            meta = await self.git_meta_changes(before)
            if not bad and not meta:
                break
            for ch, reason in bad:
                reported.setdefault(ch.path, Violation(ch.path, ch.operation, reason))
            for path, op in meta:
                key = ".git:" + path
                reported.setdefault(key, Violation(self._meta_label(before, path), op, "git metadata may only be changed by the runtime"))
            await self._revert_files(before, [ch for ch, _ in bad])
            await asyncio.to_thread(self._restore_meta, before, meta)
        # final state
        changes = await self.detect(before)
        still_bad = {ch.path for ch in changes if not self._judge(ch, decide)[0]}
        meta_left = {p for p, _ in await self.git_meta_changes(before)}
        for key, v in reported.items():
            v.reverted = (key[5:] not in meta_left) if key.startswith(".git:") else (v.path not in still_bad)
        outcome.violations = list(reported.values())
        outcome.allowed = [ch for ch in changes if ch.path not in still_bad and ch.path not in reported]
        return outcome

    def _meta_label(self, before: WorkspaceSnapshot, path: str) -> str:
        p = Path(path)
        for d in before.git_meta.dirs if before.git_meta else []:
            if p == d or p.is_relative_to(d):
                return ".git/" + p.relative_to(d).as_posix()
        return path

    async def _revert_files(self, before: WorkspaceSnapshot, changes: list[DetectedChange]) -> None:
        creates = [c for c in changes if c.operation == "create"]
        restores = [c for c in changes if c.operation != "create"]
        await asyncio.to_thread(self._remove_created, before, creates)
        from_index: list[str] = []
        for ch in restores:
            backup = before.backups.get(ch.path)
            if backup is not None:
                await asyncio.to_thread(self._restore_backup, ch.path, backup)
            elif before.git_mode and ch.path in before.tracked and ch.path not in before.dirty:
                from_index.append(ch.path)
            else:
                log.warning("cannot restore out-of-scope change", extra={"path": ch.path, "operation": ch.operation})
        if from_index:
            await asyncio.to_thread(self._clear_for_restore, from_index)
            res = await self.git.restore_from_index(from_index)
            if not res.ok:
                log.warning("git checkout-index failed during revert", extra={"error": res.err_text(500)})

    def _remove_created(self, before: WorkspaceSnapshot, creates: list[DetectedChange]) -> None:
        root = self.fs.root
        for ch in creates:
            target = root / ch.path
            try:
                if ch.path.endswith("/.git") and ch.path[:-5] not in before.dirs and (root / ch.path[:-5]).is_dir():
                    shutil.rmtree(root / ch.path[:-5])  # embedded repository created by the command
                    target = root / ch.path[:-5]
                elif target.is_symlink() or target.is_file():
                    target.unlink()
            except OSError as exc:
                log.warning("could not remove out-of-scope file", extra={"path": ch.path, "error": str(exc)})
                continue
            parent = target.parent
            while parent != root and parent.is_relative_to(root):
                rel = parent.relative_to(root).as_posix()
                if rel in before.dirs:
                    break
                try:
                    parent.rmdir()  # only succeeds when empty
                except OSError:
                    break
                parent = parent.parent

    def _clear_for_restore(self, paths: list[str]) -> None:
        """A path replaced by a directory/symlink must be cleared before the file can be restored."""
        for rel in paths:
            target = self.fs.root / rel
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                try:
                    target.rmdir()
                except OSError:
                    log.warning("cannot restore file replaced by a non-empty directory", extra={"path": rel})

    def _restore_backup(self, rel: str, backup: Backup) -> None:
        target = self.fs.root / rel
        self._clear_for_restore([rel])
        target.parent.mkdir(parents=True, exist_ok=True)
        if backup.link is not None:
            if target.is_symlink() or target.exists():
                target.unlink()
            os.symlink(backup.link, target)
            return
        if target.is_symlink():
            target.unlink()
        self.fs.atomic_write(target, backup.data, mode=backup.mode)

    def _restore_meta(self, before: WorkspaceSnapshot, changes: list[tuple[str, Operation]]) -> None:
        meta = before.git_meta
        if meta is None:
            return
        for path, op in changes:
            p = Path(path)
            if path in meta.index_backups:
                self._write_raw(p, meta.index_backups[path])
                continue
            if op == "create":
                try:
                    p.unlink()
                except OSError as exc:
                    log.warning("could not remove git metadata file", extra={"path": path, "error": str(exc)})
                continue
            old = meta.files.get(path)
            if isinstance(old, bytes):
                self._write_raw(p, old)
            else:
                log.warning("git metadata file too large to restore", extra={"path": path})

    @staticmethod
    def _write_raw(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".hermclaw-restore")
        tmp.write_bytes(data)
        os.replace(tmp, path)
