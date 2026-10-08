"""Path-level git operations on one working tree (no database, no policy).

Used by :class:`hermclaw.gitops.engine.GitEngine` (which adds locking, policy and audit) and by the
read-only :class:`hermclaw.gitops.reader.WorkspaceGitReader` that backs the LLM's git read tools.
All paths are passed through NUL-separated stdin/pathspec files or after ``--``; ``GIT_LITERAL_PATHSPECS``
is set by the runner, so file names are never interpreted as options or globs.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.gitops import _fs
from hermclaw.gitops.errors import WorkspaceStateError
from hermclaw.gitops.parsing import parse_name_status, parse_numstat, parse_porcelain_v1, split_nul
from hermclaw.gitops.runner import GitRunner
from hermclaw.gitops.types import DiffResult, FileChange, StatusEntry

SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
DEFAULT_MAX_PATCH_BYTES = 200_000
DEFAULT_MAX_FILES = 2_000
IN_PROGRESS_MARKERS: tuple[tuple[str, str], ...] = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
)


def is_sha(value: str) -> bool:
    return bool(SHA_RE.match(value))


def nul_join(paths: Sequence[str]) -> bytes:
    return b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)


async def rev_parse(runner: GitRunner, cwd: Path, rev: str) -> str | None:
    """Full commit SHA for ``rev`` or ``None`` if it does not resolve to a commit."""
    if not rev or rev.startswith("-"):
        return None
    res = await runner.run(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=cwd, check=False)
    sha = res.first_line
    return sha if res.returncode == 0 and is_sha(sha) else None


async def head_sha(runner: GitRunner, cwd: Path) -> str:
    sha = await rev_parse(runner, cwd, "HEAD")
    if sha is None:
        raise WorkspaceStateError("workspace has no HEAD commit", details={"path": str(cwd)})
    return sha


async def current_branch(runner: GitRunner, cwd: Path) -> str | None:
    res = await runner.run(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd, check=False)
    if res.returncode != 0:
        return None
    return res.first_line or None


async def git_dir(runner: GitRunner, cwd: Path) -> Path:
    res = await runner.run(["rev-parse", "--absolute-git-dir"], cwd=cwd)
    return Path(res.first_line)


async def is_work_tree(runner: GitRunner, cwd: Path) -> bool:
    if not await asyncio.to_thread(_fs.is_dir, cwd):
        return False
    res = await runner.run(["rev-parse", "--is-inside-work-tree", "--show-toplevel"], cwd=cwd, check=False)
    lines = res.text.strip().splitlines()
    if res.returncode != 0 or not lines or lines[0] != "true":
        return False
    return len(lines) > 1 and await asyncio.to_thread(_fs.same_path, Path(lines[1]), cwd)


async def is_ancestor(runner: GitRunner, cwd: Path, ancestor: str, descendant: str) -> bool:
    res = await runner.run(["merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd, ok_codes=(0, 1))
    return res.returncode == 0


async def has_commit(runner: GitRunner, cwd: Path, sha: str) -> bool:
    res = await runner.run(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd, check=False)
    return res.returncode == 0


async def count_commits(runner: GitRunner, cwd: Path, revision_range: str) -> int:
    res = await runner.run(["rev-list", "--count", revision_range], cwd=cwd)
    return int(res.first_line or "0")


async def read_status(runner: GitRunner, cwd: Path, *, renames: bool = False) -> list[StatusEntry]:
    """Porcelain status incl. every untracked file (``--untracked-files=all``); ignored files are excluded."""
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"]
    args.append("--renames" if renames else "--no-renames")
    res = await runner.run(args, cwd=cwd)
    return [StatusEntry(path=e.path, index=e.index, worktree=e.worktree, orig_path=e.orig_path) for e in parse_porcelain_v1(res.stdout)]


async def conflicted_files(runner: GitRunner, cwd: Path) -> list[str]:
    res = await runner.run(["diff", "--name-only", "--diff-filter=U", "-z"], cwd=cwd, check=False)
    return sorted(set(split_nul(res.stdout)))


async def staged_paths(runner: GitRunner, cwd: Path) -> list[str]:
    """Paths whose index entry differs from HEAD (renames disabled: both sides are listed)."""
    res = await runner.run(["diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"], cwd=cwd)
    return sorted(set(split_nul(res.stdout)))


async def operations_in_progress(runner: GitRunner, cwd: Path) -> list[str]:
    gdir = await git_dir(runner, cwd)
    found: list[str] = []
    for marker, name in IN_PROGRESS_MARKERS:
        if await asyncio.to_thread(_fs.exists, gdir / marker) and name not in found:
            found.append(name)
    return found


async def untracked_files(runner: GitRunner, cwd: Path, *, env: dict[str, str] | None = None) -> list[str]:
    res = await runner.run(["ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd, env=env)
    return [p for p in split_nul(res.stdout) if not p.endswith("/")]


@contextlib.asynccontextmanager
async def _scratch_index(runner: GitRunner, cwd: Path) -> AsyncIterator[dict[str, str]]:
    """Temporary copy of the index so intent-to-add entries never touch the real index."""
    gdir = await git_dir(runner, cwd)
    scratch = gdir / f"hermclaw-scratch-{uuid.uuid4().hex}.index"
    try:
        await asyncio.to_thread(_fs.copy_if_exists, gdir / "index", scratch)
        yield {"GIT_INDEX_FILE": str(scratch)}
    finally:
        await asyncio.to_thread(_fs.remove_file, scratch)
        await asyncio.to_thread(_fs.remove_file, scratch.with_name(scratch.name + ".lock"))


async def read_diff(
    runner: GitRunner,
    cwd: Path,
    base: str,
    *,
    paths: Sequence[str] | None = None,
    include_untracked: bool = True,
    include_patch: bool = True,
    renames: bool = True,
    max_patch_bytes: int = DEFAULT_MAX_PATCH_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    context_lines: int = 3,
) -> DiffResult:
    """Diff of the working tree (committed + staged + unstaged + untracked) against ``base``.

    Untracked files are included through intent-to-add entries in a scratch index; the real index is never
    modified. The patch is redacted and capped at ``max_patch_bytes`` (``patch_truncated`` tells).
    """
    if not is_sha(base) and await rev_parse(runner, cwd, base) is None:
        raise WorkspaceStateError(f"diff base {base!r} does not resolve to a commit", details={"base": base})
    pathspec = ["--", *paths] if paths else []
    rename_flag = "-M" if renames else "--no-renames"
    async with _scratch_index(runner, cwd) as env:
        if include_untracked:
            new_files = await untracked_files(runner, cwd, env=env)
            if new_files:
                await runner.run(
                    ["add", "--intent-to-add", "--pathspec-from-file=-", "--pathspec-file-nul"],
                    cwd=cwd,
                    env=env,
                    input_data=nul_join(new_files),
                )
        common = ["diff", "--no-color", "--no-ext-diff", rename_flag]
        name_status = await runner.run([*common, "--name-status", "-z", base, *pathspec], cwd=cwd, env=env)
        numstat = await runner.run([*common, "--numstat", "-z", base, *pathspec], cwd=cwd, env=env)
        patch_text = ""
        patch_truncated = False
        patch_bytes = 0
        if include_patch:
            patch = await runner.run(
                [*common, "--no-textconv", f"-U{max(0, int(context_lines))}", base, *pathspec],
                cwd=cwd,
                env=env,
                max_stdout=max(0, int(max_patch_bytes)),
            )
            patch_text = DEFAULT_REDACTOR.text(patch.stdout.decode("utf-8", "replace"))
            patch_truncated = patch.stdout_truncated
            patch_bytes = len(patch.stdout)
    stats = {(n.path, n.old_path): n for n in parse_numstat(numstat.stdout)}
    stats_by_path = {n.path: n for n in stats.values()}
    files: list[FileChange] = []
    adds = dels = 0
    for entry in parse_name_status(name_status.stdout):
        st = stats.get((entry.path, entry.old_path)) or stats_by_path.get(entry.path)
        a = st.additions if st else None
        d = st.deletions if st else None
        adds += a or 0
        dels += d or 0
        files.append(
            FileChange(
                path=entry.path,
                status=entry.status,
                old_path=entry.old_path,
                additions=a,
                deletions=d,
                binary=bool(st and st.binary),
            )
        )
    truncated = len(files) > max_files
    return DiffResult(
        base=base,
        files=files[:max_files],
        files_truncated=truncated,
        patch=patch_text,
        patch_truncated=patch_truncated,
        patch_bytes=patch_bytes,
        total_additions=adds,
        total_deletions=dels,
    )


async def changed_files(runner: GitRunner, cwd: Path, base: str) -> list[str]:
    """Every path changed relative to ``base`` (renames reported as delete + create), sorted."""
    diff = await read_diff(runner, cwd, base, include_patch=False, renames=False, max_files=1_000_000)
    return sorted(set(diff.changed_paths))


async def ls_remote_head(runner: GitRunner, cwd: Path, remote: str, branch: str) -> str | None:
    """SHA of ``refs/heads/<branch>`` on ``remote`` (``None`` if the branch does not exist)."""
    ref = f"refs/heads/{branch}"
    res = await runner.run(["ls-remote", "--refs", remote, ref], cwd=cwd, timeout_s=None)
    for line in res.text.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref and is_sha(sha.strip()):
            return sha.strip()
    return None
