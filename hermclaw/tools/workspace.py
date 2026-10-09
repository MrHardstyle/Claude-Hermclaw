"""Workspace-confined filesystem access for LLM tools (P17 17.1/17.3).

Rules enforced here (independent of the scope contract):

- every path is repository-relative, normalised (``./``, ``//``, trailing ``/``) and free of ``..`` – absolute
  paths, NUL bytes and parent references are refused (``PATH_INVALID``);
- the real path (symlinks resolved) must stay inside the workspace root (``PATH_OUTSIDE_WORKSPACE``), so a
  symlink can never be used to read or write outside the workspace;
- ``.git`` (at any depth) and secret-like files from ``policies.scope.always_forbidden`` are never readable or
  writable through tools (``PATH_FORBIDDEN``) – secrets must not reach prompts;
- writes never follow a symlink (``SYMLINK_REFUSED``) and are atomic (temp file in the same directory + fsync +
  ``os.replace``), preserving the mode of an existing file.
"""

from __future__ import annotations

import os
import posixpath
import re
import stat
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from hermclaw.contracts.scope import normalise_path
from hermclaw.scope.guard import any_match, path_matches
from hermclaw.tools import errors as E
from hermclaw.tools.errors import ToolError

BINARY_SNIFF_BYTES = 8192
# directories that are never interesting for listing/searching when the workspace is not a git repository
NOISE_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"})


def canonical_path(raw: str) -> str:
    """Normalise a model-supplied path to the canonical repository-relative form ('.' for the root)."""
    if not isinstance(raw, str) or "\x00" in raw:
        raise ToolError(E.PATH_INVALID, "path must be a string without NUL bytes")
    cleaned = raw.strip()
    if cleaned in ("", ".", "./"):
        return "."
    try:
        rel = normalise_path(cleaned)
    except ValueError as exc:
        raise ToolError(E.PATH_INVALID, str(exc)) from exc
    rel = posixpath.normpath(rel)
    if rel.startswith("../") or rel == ".." or rel.startswith("/"):
        raise ToolError(E.PATH_INVALID, f"path escapes the workspace: {raw!r}")
    return rel


def has_git_segment(rel: str) -> bool:
    return ".git" in rel.split("/")


def is_binary(data: bytes) -> bool:
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # a multi-byte sequence cut at the end of a sniffed chunk is still text
        return not (exc.start >= len(data) - 3 and exc.reason == "unexpected end of data")
    return False


@dataclass(frozen=True)
class TextFile:
    rel: str
    text: str
    size: int
    mode: int


class WorkspaceFS:
    """Path confinement and file primitives for one workspace root."""

    def __init__(self, root: Path, *, read_forbidden: Sequence[str] = ()) -> None:
        real = Path(os.path.realpath(root))
        if not real.is_dir():
            raise ToolError(E.NOT_A_DIRECTORY, f"workspace root does not exist: {root}")
        self.root = real
        self.read_forbidden = list(read_forbidden)

    # ------------------------------------------------------------------------------------------- paths
    def _inside(self, real: Path) -> bool:
        return real == self.root or real.is_relative_to(self.root)

    def is_hidden(self, rel: str) -> bool:
        """Paths no tool may expose (.git internals, secret files)."""
        return has_git_segment(rel) or any_match(rel, self.read_forbidden)

    def resolve(self, raw: str, *, allow_root: bool = False) -> tuple[str, Path]:
        rel = canonical_path(raw)
        if rel == ".":
            if not allow_root:
                raise ToolError(E.PATH_INVALID, "a file path is required, not the workspace root")
            return rel, self.root
        if self.is_hidden(rel):
            raise ToolError(E.PATH_FORBIDDEN, f"access to '{rel}' is not permitted (git internals or secret file)")
        target = self.root / rel
        real = Path(os.path.realpath(target))
        if not self._inside(real):
            raise ToolError(E.PATH_OUTSIDE_WORKSPACE, f"'{rel}' resolves outside the workspace")
        if real != target:
            # resolved through a symlink: the *target* must not be hidden either
            real_rel = real.relative_to(self.root).as_posix() if real != self.root else "."
            if real_rel != "." and self.is_hidden(real_rel):
                raise ToolError(E.PATH_FORBIDDEN, f"'{rel}' points to a protected path")
        return rel, target

    def resolve_for_write(self, raw: str) -> tuple[str, Path]:
        rel, target = self.resolve(raw)
        if target.is_symlink():
            raise ToolError(E.SYMLINK_REFUSED, f"'{rel}' is a symlink; tools never write through symlinks")
        parent_real = Path(os.path.realpath(target.parent))
        if not self._inside(parent_real):
            raise ToolError(E.PATH_OUTSIDE_WORKSPACE, f"parent directory of '{rel}' resolves outside the workspace")
        if target.is_dir():
            raise ToolError(E.NOT_A_FILE, f"'{rel}' is a directory")
        # every existing ancestor must be a real directory (not a file)
        for anc in target.relative_to(self.root).parents:
            p = self.root / anc
            if p.exists() and not p.is_dir():
                raise ToolError(E.NOT_A_DIRECTORY, f"'{anc.as_posix()}' is a file, cannot create '{rel}' below it")
        return rel, target

    # ------------------------------------------------------------------------------------------- reading
    def read_text(self, raw: str, *, max_bytes: int) -> TextFile:
        rel, target = self.resolve(raw)
        if not target.exists():
            raise ToolError(E.NOT_FOUND, f"'{rel}' does not exist")
        if not target.is_file():
            raise ToolError(E.NOT_A_FILE, f"'{rel}' is not a regular file")
        st = target.stat()
        if st.st_size > max_bytes:
            raise ToolError(
                E.FILE_TOO_LARGE,
                f"'{rel}' has {st.st_size} bytes (limit {max_bytes}); use read_range or find_text",
                data={"bytes": st.st_size},
            )
        data = target.read_bytes()
        if is_binary(data):
            raise ToolError(E.BINARY_FILE, f"'{rel}' is binary or not UTF-8 ({len(data)} bytes); not shown", data={"bytes": len(data)})
        return TextFile(rel=rel, text=data.decode("utf-8"), size=len(data), mode=stat.S_IMODE(st.st_mode))

    def read_lines(self, raw: str, start: int, end: int, *, max_line_chars: int = 4000) -> tuple[str, list[str], int]:
        """Lines ``start..end`` (1-based, inclusive) streamed from disk; returns ``(rel, lines, total_lines)``."""
        rel, target = self.resolve(raw)
        if not target.exists():
            raise ToolError(E.NOT_FOUND, f"'{rel}' does not exist")
        if not target.is_file():
            raise ToolError(E.NOT_A_FILE, f"'{rel}' is not a regular file")
        with target.open("rb") as fh:
            head = fh.read(BINARY_SNIFF_BYTES)
            if is_binary(head):
                raise ToolError(E.BINARY_FILE, f"'{rel}' is binary or not UTF-8; not shown")
            fh.seek(0)
            out: list[str] = []
            total = 0
            for total, raw_line in enumerate(fh, start=1):
                if start <= total <= end:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if len(line) > max_line_chars:
                        line = line[:max_line_chars] + "…[line truncated]"
                    out.append(line)
        return rel, out, total

    # ------------------------------------------------------------------------------------------- writing
    def atomic_write(self, target: Path, data: bytes, *, mode: int | None = None) -> None:
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        if not self._inside(Path(os.path.realpath(parent))):  # re-check after mkdir (TOCTOU)
            raise ToolError(E.PATH_OUTSIDE_WORKSPACE, "parent directory resolves outside the workspace")
        fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f".{target.name}.", suffix=".hermclaw-tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode if mode is not None else 0o644)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------------------------------- inventory
    def walk_files(self) -> list[str]:
        """Fallback inventory for non-git workspaces (skips noise directories, never follows symlinks)."""
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in NOISE_DIRS)
            base = Path(dirpath).relative_to(self.root).as_posix()
            for name in sorted(filenames):
                out.append(name if base == "." else f"{base}/{name}")
        return out

    def visible(self, paths: Iterable[str]) -> list[str]:
        """Existing, non-hidden files from an inventory."""
        out: list[str] = []
        for rel in paths:
            if self.is_hidden(rel):
                continue
            if os.path.lexists(self.root / rel):
                out.append(rel)
        return out


# ----------------------------------------------------------------------------------------------- listing
def filter_listing(files: Iterable[str], base: str, pattern: str | None) -> list[str]:
    prefix = "" if base == "." else base + "/"
    selected = [f for f in files if not prefix or f.startswith(prefix)]
    if pattern:
        pat = pattern.strip()
        if "/" in pat:
            selected = [f for f in selected if path_matches(f, pat)]
        else:
            selected = [f for f in selected if path_matches(posixpath.basename(f), pat)]
    return sorted(selected)


def immediate_children(files: Iterable[str], base: str) -> list[str]:
    prefix = "" if base == "." else base + "/"
    children: dict[str, None] = {}
    for f in files:
        if prefix and not f.startswith(prefix):
            continue
        rest = f[len(prefix) :]
        head, sep, _ = rest.partition("/")
        children.setdefault(head + "/" if sep else head, None)
    return sorted(children, key=lambda c: (not c.endswith("/"), c))


# ----------------------------------------------------------------------------------------------- search
@dataclass
class SearchOutcome:
    lines: list[str]
    matches: int
    files_searched: int
    files_matched: int
    limit_hit: bool
    time_budget_hit: bool


def search_files(
    fs: WorkspaceFS,
    files: Sequence[str],
    pattern: str,
    *,
    regex: bool,
    ignore_case: bool,
    max_results: int,
    max_file_bytes: int = 1_000_000,
    max_line_chars: int = 2000,
    time_budget_seconds: float = 20.0,
) -> SearchOutcome:
    """Line-wise text search over ``files`` (literal by default; regex lines are capped to bound backtracking)."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as exc:
        raise ToolError(E.PATTERN_INVALID, f"invalid regular expression: {exc}") from exc
    deadline = time.monotonic() + time_budget_seconds
    out: list[str] = []
    matches = searched = matched_files = 0
    limit_hit = budget_hit = False
    for rel in files:
        if time.monotonic() > deadline:
            budget_hit = True
            break
        path = fs.root / rel
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > max_file_bytes:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if is_binary(data):
            continue
        searched += 1
        hit_in_file = False
        for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            probe = line[:max_line_chars]
            if rx.search(probe):
                if not hit_in_file:
                    matched_files += 1
                    hit_in_file = True
                matches += 1
                shown = probe if len(probe) <= 300 else probe[:300] + "…"
                out.append(f"{rel}:{lineno}: {shown}")
                if matches >= max_results:
                    limit_hit = True
                    break
        if limit_hit:
            break
    return SearchOutcome(out, matches, searched, matched_files, limit_hit, budget_hit)
