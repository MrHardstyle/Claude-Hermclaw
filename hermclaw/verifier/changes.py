"""Change collection for the verifier: changed paths + operations, added lines, workspace inventory.

Changed paths come from the :class:`~hermclaw.core.interfaces.GitReader` (relative to ``base_sha``: commits,
staged, unstaged and untracked-not-ignored files). The operation of each path is judged against the *base tree*
(``create`` = not in base, ``modify`` = in base and present, ``delete`` = in base and gone) with read-only local git
plumbing on the orchestrator copy; without a readable base tree the porcelain status codes are used.

Added lines are computed with ``git diff -U0 <base> -- <path>`` per tracked file (untracked files: every line), so
the secret/conflict scans only judge what the step introduced. Without local git the GitReader diff is parsed.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path

from hermclaw.contracts.scope import normalise_path
from hermclaw.core.interfaces import GitReader, GitStatusEntry, WorkspaceHandle
from hermclaw.scope.guard import any_match
from hermclaw.tools.gitlocal import LocalGit
from hermclaw.tools.workspace import has_git_segment, is_binary
from hermclaw.verifier.types import AddedContent, AddedLine, ChangeSet, FileChange, Operation

MAX_SCAN_BYTES = 8 * 1024 * 1024  # per file: bigger files/diffs are scanned up to this size (reported as truncated)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SHA = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _valid_rel(path: str) -> str | None:
    try:
        rel = normalise_path(path)
    except ValueError:
        return None
    if rel.endswith("/") or has_git_segment(rel):
        return None
    return rel


def _lexists(root: Path, rel: str) -> bool:
    return os.path.lexists(root / rel)


def _status_operation(entry: GitStatusEntry | None, exists: bool) -> Operation:
    if not exists:
        return "delete"
    code = entry.status if entry is not None else ""
    if code == "??" or "A" in code or code[:1] in ("R", "C"):
        return "create"
    return "modify"


async def collect_changes(workspace: WorkspaceHandle, git: GitReader, lgit: LocalGit) -> ChangeSet:
    """All changed paths with their operation. Raises if the GitReader cannot list changes."""
    raw = list(await git.changed_files(workspace))
    try:
        status = list(await git.status(workspace))
    except Exception:  # status only refines operations/renames; changed_files is authoritative
        status = []
    by_path = {e.path: e for e in status}
    base_files: set[str] | None = None
    if _SHA.match(workspace.base_sha or "") and await lgit.is_repo_root():
        base_files = await lgit.tree_files(workspace.base_sha)
    root = workspace.path
    candidates: list[str] = list(raw)
    candidates.extend(e.orig_path for e in status if e.orig_path and e.orig_path not in raw)
    out = ChangeSet(base_known=base_files is not None)
    seen: set[str] = set()
    for item in candidates:
        rel = _valid_rel(item)
        if rel is None:
            out.invalid_paths.append(item)
            continue
        if rel in seen:
            continue
        seen.add(rel)
        exists = await asyncio.to_thread(_lexists, root, rel)
        entry = by_path.get(rel)
        if base_files is not None:
            in_base = rel in base_files
            if exists:
                op: Operation = "modify" if in_base else "create"
            elif in_base:
                op = "delete"
            else:
                continue  # created and removed again: nothing left to judge
        else:
            op = _status_operation(entry, exists)
        orig = entry.orig_path if entry is not None else None
        out.changes.append(FileChange(rel, op, entry.status if entry is not None else "", orig))
    out.changes.sort(key=lambda c: c.path)
    return out


# ------------------------------------------------------------------------------------------- inventory / reading
async def list_workspace_files(workspace: WorkspaceHandle, lgit: LocalGit) -> list[str]:
    """Existing tracked + untracked-not-ignored files (git), or a full walk of a non-git workspace."""
    root = workspace.path
    if await lgit.is_repo_root():
        files = await lgit.list_files()
        return await asyncio.to_thread(lambda: sorted(f for f in files if not has_git_segment(f) and os.path.lexists(root / f)))
    return await asyncio.to_thread(_walk, root)


def _walk(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        base = Path(dirpath).relative_to(root).as_posix()
        for name in sorted(filenames):
            out.append(name if base == "." else f"{base}/{name}")
    return out


def safe_file(root: Path, rel: str) -> Path | None:
    """Absolute path of a regular, non-symlink file inside ``root`` (``None`` otherwise)."""
    target = root / rel
    try:
        st = os.lstat(target)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    real = Path(os.path.realpath(target))
    root_real = Path(os.path.realpath(root))
    if not real.is_relative_to(root_real):
        return None
    return target


def read_bytes(root: Path, rel: str, *, limit: int = MAX_SCAN_BYTES) -> tuple[bytes | None, bool, str]:
    """``(data, truncated, reason)`` – ``data`` is ``None`` with a reason if the file cannot be read safely."""
    target = safe_file(root, rel)
    if target is None:
        return None, False, "not a regular file inside the workspace (symlink, directory or missing)"
    try:
        with target.open("rb") as fh:
            data = fh.read(limit + 1)
    except OSError as exc:
        return None, False, f"unreadable: {exc.strerror or exc}"
    return data[:limit], len(data) > limit, ""


def read_text(root: Path, rel: str, *, limit: int = MAX_SCAN_BYTES) -> tuple[str | None, str]:
    """Decoded text of a regular file; ``(None, reason)`` for binary/unreadable/oversized files."""
    data, truncated, reason = read_bytes(root, rel, limit=limit)
    if data is None:
        return None, reason
    if truncated:
        return None, f"larger than {limit} bytes"
    if is_binary(data):
        return None, "binary file"
    return data.decode("utf-8", errors="replace"), ""


# ------------------------------------------------------------------------------------------- added lines
def parse_unified_added(diff: str) -> list[AddedLine]:
    """Added lines (with new-file line numbers) of a single-file unified diff."""
    out: list[AddedLine] = []
    line_no = 0
    in_hunk = False
    for raw in diff.split("\n"):
        m = _HUNK.match(raw)
        if m:
            line_no = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+"):
            out.append(AddedLine(line_no, raw[1:]))
            line_no += 1
        elif raw.startswith(" "):
            line_no += 1
        elif raw.startswith(("-", "\\")):
            continue
        elif raw.startswith("diff --git"):
            in_hunk = False
    return out


_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def unquote_git_path(raw: str) -> str:
    """Undo git's C-style quoting of unusual path names (``"a/x\\tb"``)."""
    if not (len(raw) >= 2 and raw.startswith('"') and raw.endswith('"')):
        return raw
    body, out, i = raw[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if re.fullmatch(r"[0-7]{3}", body[i + 1 : i + 4]):
                out.append(int(body[i + 1 : i + 4], 8))
                i += 4
                continue
            out.extend(_ESCAPES.get(nxt, nxt).encode("utf-8"))
            i += 2
            continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="surrogateescape")


def split_multi_file_diff(diff: str) -> dict[str, str]:
    """``{new path: file diff}`` for a multi-file unified diff (deleted files are left out)."""
    files: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in diff.split("\n"):
        if raw.startswith("diff --git "):
            current = None
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current = None
                continue
            target = unquote_git_path(target)
            if target.startswith("b/"):
                target = target[2:]
            current = files.setdefault(target, [])
            continue
        if current is not None:
            current.append(raw)
    return {path: "\n".join(lines) for path, lines in files.items()}


async def _local_added(workspace: WorkspaceHandle, lgit: LocalGit, change: FileChange, tracked: set[str], content: AddedContent) -> None:
    root = workspace.path
    rel = change.path
    if await asyncio.to_thread(lambda: os.path.islink(root / rel)):
        content.skipped[rel] = "symlink"
        return
    if rel in tracked:
        res = await lgit.run(
            "--literal-pathspecs",
            "diff",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            workspace.base_sha,
            "--",
            rel,
        )
        if not res.ok:
            content.skipped[rel] = f"git diff failed: {res.err_text(300)}"
            return
        raw = res.stdout[:MAX_SCAN_BYTES]
        if len(res.stdout) > MAX_SCAN_BYTES:
            content.truncated.append(rel)
        text = raw.decode("utf-8", errors="replace")
        if "\nBinary files " in text or text.startswith("Binary files ") or "\nGIT binary patch" in text:
            content.skipped[rel] = "binary file"
            return
        content.lines[rel] = parse_unified_added(text)
        return
    data, truncated, reason = await asyncio.to_thread(read_bytes, root, rel)
    if data is None:
        content.skipped[rel] = reason
        return
    if is_binary(data):
        content.skipped[rel] = "binary file"
        return
    if truncated:
        content.truncated.append(rel)
    text = data.decode("utf-8", errors="replace")
    content.lines[rel] = [AddedLine(i, line) for i, line in enumerate(text.split("\n"), start=1)]


async def added_content(workspace: WorkspaceHandle, changes: ChangeSet, git: GitReader, lgit: LocalGit) -> AddedContent:
    """Added lines of every created/modified file."""
    existing = changes.existing
    content = AddedContent()
    if not existing:
        return content
    if changes.base_known:
        tracked = await lgit.tracked_files()
        sem = asyncio.Semaphore(8)

        async def one(change: FileChange) -> None:
            async with sem:
                await _local_added(workspace, lgit, change, tracked, content)

        await asyncio.gather(*(one(c) for c in existing))
        return content
    # fallback: the GitReader diff (tracked files) + full content of untracked files
    content.source = "git-reader"
    diff = await git.diff(workspace, [c.path for c in existing], max_bytes=MAX_SCAN_BYTES)
    if len(diff) >= MAX_SCAN_BYTES or "[diff truncated" in diff:
        content.truncated.append("(git reader diff)")
    per_file = split_multi_file_diff(diff)
    for change in existing:
        if change.path in per_file:
            content.lines[change.path] = parse_unified_added(per_file[change.path])
            continue
        if change.operation == "create" or change.status == "??":
            data, truncated, reason = await asyncio.to_thread(read_bytes, workspace.path, change.path)
            if data is None or is_binary(data):
                content.skipped[change.path] = reason or "binary file"
                continue
            if truncated:
                content.truncated.append(change.path)
            text = data.decode("utf-8", errors="replace")
            content.lines[change.path] = [AddedLine(i, line) for i, line in enumerate(text.split("\n"), start=1)]
        else:
            content.skipped[change.path] = "no textual diff available"
    return content


def matching(paths: Iterable[str], globs: Iterable[str]) -> list[str]:
    """Paths matching any of ``globs`` (ScopeGuard glob semantics)."""
    pats = [g for g in globs if g]
    return sorted(p for p in paths if any_match(p, pats))
