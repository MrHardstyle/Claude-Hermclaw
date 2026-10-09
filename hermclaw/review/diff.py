"""Unified-diff splitting and per-file size budgeting for the review prompt (22.1).

The heavy reviewer has a 24K–32K context. A large diff must therefore be shortened *fairly*: every changed file
keeps a header (path, change kind, +/- line counts) and gets a share of the diff budget computed by max–min fair
allocation (small files are shown completely, large files share what is left). Generated files are capped, files
whose path matches a withheld glob (secrets such as ``**/.env``) never show their content, and files beyond
``max_files`` are listed by name only. Every omission is stated explicitly in the prompt.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from hermclaw.review.text import clip_lines, fence
from hermclaw.scope.guard import any_match

ChangeKind = Literal["added", "deleted", "modified", "renamed", "mode", "binary", "unknown"]

_HEADER = "diff --git "
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    change: ChangeKind = "modified"
    body: str = ""  # hunks (from the first '@@') or a short binary note; git header lines removed
    additions: int = 0
    deletions: int = 0

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass
class RenderedDiff:
    text: str
    files: int
    files_truncated: int
    files_omitted: int
    files_withheld: int
    chars_total: int
    chars_included: int
    omitted_paths: list[str] = field(default_factory=list)


def unquote_git_path(raw: str) -> str:
    """Decode git's C-style quoted path (``"dir/\\303\\244.txt"``) – unquoted input is returned unchanged."""
    s = raw.strip()
    if len(s) < 2 or not (s.startswith('"') and s.endswith('"')):
        return s
    body = s[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out.extend(ch.encode("utf-8"))
            i += 1
            continue
        nxt = body[i + 1]
        octal = body[i + 1 : i + 4]
        if len(octal) == 3 and all(c in "01234567" for c in octal):
            out.append(int(octal, 8) & 0xFF)
            i += 4
            continue
        out.append(_ESCAPES.get(nxt, ord(nxt)) if ord(nxt) < 128 else ord("?"))
        i += 2
    return out.decode("utf-8", errors="replace")


def _strip_prefix(path: str, prefix: str) -> str:
    return path[len(prefix) :] if path.startswith(prefix) else path


def _paths_from_header(rest: str) -> tuple[str, str]:
    """``a/<old> b/<new>`` (possibly quoted). Unquoted paths with spaces are resolved by symmetry."""
    rest = rest.strip()
    if rest.startswith('"'):
        end = rest.find('" ', 1)
        while end != -1 and rest[end - 1] == "\\":
            end = rest.find('" ', end + 1)
        if end != -1:
            old, new = rest[: end + 1], rest[end + 2 :]
            return _strip_prefix(unquote_git_path(old), "a/"), _strip_prefix(unquote_git_path(new), "b/")
    if rest.endswith('"'):
        start = rest.find(' "')
        if start != -1:
            return _strip_prefix(rest[:start], "a/"), _strip_prefix(unquote_git_path(rest[start + 1 :]), "b/")
    n = len(rest)
    if n % 2 == 1:
        half = (n - 1) // 2
        old, new = rest[:half], rest[half + 1 :]
        if old.startswith("a/") and new.startswith("b/") and old[2:] == new[2:]:
            return old[2:], new[2:]
    old, _, new = rest.partition(" b/")
    return _strip_prefix(old, "a/"), new


def _parse_block(lines: list[str]) -> FileDiff:
    old_path, new_path = _paths_from_header(lines[0][len(_HEADER) :])
    fd = FileDiff(path=new_path or old_path, old_path=None)
    body_start: int | None = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.startswith("@@"):
            body_start = idx
            break
        if line.startswith("new file mode"):
            fd.change = "added"
        elif line.startswith("deleted file mode"):
            fd.change = "deleted"
        elif line.startswith("rename from "):
            fd.old_path = unquote_git_path(line[len("rename from ") :])
            fd.change = "renamed"
        elif line.startswith("rename to "):
            fd.path = unquote_git_path(line[len("rename to ") :])
            fd.change = "renamed"
        elif line.startswith(("old mode", "new mode")) and fd.change == "modified":
            fd.change = "mode"
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            fd.change = "binary" if fd.change in ("modified", "mode") else fd.change
            fd.body = "Binary files differ (content not shown)"
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            fd.path = _strip_prefix(unquote_git_path(line[4:]), "b/")
        elif line.startswith("--- ") and fd.change == "deleted" and not line.startswith("--- /dev/null"):
            fd.path = _strip_prefix(unquote_git_path(line[4:]), "a/")
    if fd.change == "renamed" and fd.old_path is None and old_path != fd.path:
        fd.old_path = old_path
    if body_start is not None:
        hunk = lines[body_start:]
        fd.body = "\n".join(hunk).rstrip("\n")
        for line in hunk:
            if line.startswith("+") and not line.startswith("+++"):
                fd.additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                fd.deletions += 1
    return fd


def split_diff(diff: str) -> tuple[str, list[FileDiff]]:
    """Split a ``git diff`` into per-file sections. Returns (preamble, files).

    Text that is not part of a ``diff --git`` block (e.g. a truncation note of the git reader) ends up in the
    preamble. A non-empty diff without git headers becomes one pseudo file ``(diff)`` so nothing is lost."""
    if not diff.strip():
        return "", []
    lines = diff.split("\n")
    preamble: list[str] = []
    blocks: list[list[str]] = []
    for line in lines:
        if line.startswith(_HEADER):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            preamble.append(line)
    if not blocks:
        body = diff.strip("\n")
        adds = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
        dels = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
        return "", [FileDiff(path="(diff)", change="unknown", body=body, additions=adds, deletions=dels)]
    files = [_parse_block(b) for b in blocks]
    # trailing notes appended after the last block (e.g. "…[diff truncated …]") belong to the preamble
    last = files[-1]
    tail: list[str] = []
    body_lines = last.body.split("\n") if last.body else []
    while body_lines and body_lines[-1].startswith("…["):
        tail.insert(0, body_lines.pop())
    if tail:
        last.body = "\n".join(body_lines).rstrip("\n")
    return "\n".join([*preamble, *tail]).strip("\n"), files


def fair_allocation(sizes: Sequence[int], budget: int) -> list[int]:
    """Max–min fair split of ``budget`` over ``sizes``: nobody gets more than it needs, the rest is shared."""
    alloc = [0] * len(sizes)
    remaining = max(0, budget)
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    for pos, idx in enumerate(order):
        share = remaining // (len(order) - pos)
        give = min(max(0, sizes[idx]), share)
        alloc[idx] = give
        remaining -= give
    return alloc


def _header(fd: FileDiff, note: str = "") -> str:
    origin = f" (from {fd.old_path})" if fd.old_path and fd.old_path != fd.path else ""
    extra = f" – {note}" if note else ""
    return f"### FILE: {fd.path}{origin} [{fd.change}, +{fd.additions} -{fd.deletions}]{extra}"


def _safe_any_match(path: str, globs: Sequence[str]) -> bool:
    if not globs:
        return False
    try:
        return any_match(path, list(globs))
    except ValueError:  # not a repository-relative path: treat conservatively as matching
        return True


def render_diff(
    files: Sequence[FileDiff],
    budget: int,
    *,
    max_files: int = 80,
    min_file_chars: int = 400,
    generated_globs: Sequence[str] = (),
    generated_cap: int = 400,
    withheld_globs: Sequence[str] = (),
) -> RenderedDiff:
    """Render ``files`` into at most ~``budget`` chars (headers always, bodies fairly budgeted)."""
    shown = list(files[:max_files])
    omitted = [f.path for f in files[max_files:]]
    total = sum(f.size for f in files)
    withheld = [_safe_any_match(f.path, withheld_globs) for f in shown]
    generated = [_safe_any_match(f.path, generated_globs) for f in shown]
    headers = [_header(f) for f in shown]
    overhead_per_file = 12  # fence lines + blank line
    fixed = sum(len(h) + overhead_per_file for h in headers)
    omitted_line = f"(+{len(omitted)} more changed file(s) not shown: {', '.join(omitted[:50])})" if omitted else ""
    body_budget = max(0, budget - fixed - len(omitted_line))
    wants = [0 if withheld[i] else (min(f.size, generated_cap) if generated[i] else f.size) for i, f in enumerate(shown)]
    alloc = fair_allocation(wants, body_budget)
    parts: list[str] = []
    truncated = 0
    included = 0
    for i, fd in enumerate(shown):
        if withheld[i]:
            parts.append(_header(fd, "content withheld by policy (protected path)"))
            continue
        if not fd.body:
            parts.append(_header(fd, "no textual changes"))
            continue
        if alloc[i] >= fd.size:
            parts.append(headers[i] + "\n" + fence(fd.body, "diff"))
            included += fd.size
            continue
        truncated += 1
        if alloc[i] < min_file_chars:
            parts.append(_header(fd, f"diff omitted to fit the review budget ({fd.size} chars)"))
            continue
        body, _ = clip_lines(fd.body, alloc[i], what=f"diff lines of {fd.path}")
        included += len(body)
        parts.append(_header(fd, "diff truncated to fit the review budget") + "\n" + fence(body, "diff"))
    if omitted_line:
        parts.append(omitted_line)
    return RenderedDiff(
        text="\n\n".join(parts),
        files=len(files),
        files_truncated=truncated,
        files_omitted=len(omitted),
        files_withheld=sum(withheld),
        chars_total=total,
        chars_included=included,
        omitted_paths=omitted,
    )
