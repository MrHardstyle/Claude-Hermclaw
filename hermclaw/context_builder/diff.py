"""Current diff with per-file truncation (step 16.7).

The diff (from ``GitReader.diff``) is split per file. Small file diffs are shown completely; the budget is
water-filled so that large diffs are truncated per file (header + first hunks, then an explicit marker pointing
to ``git_diff``) instead of one huge file crowding out all others. If even the minimum per file does not fit, the
remaining files are listed by name.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from hermclaw.context_builder.tokens import char_cost, clip_to_cost

_FILE_START_RE = re.compile(r"(?m)^(?=diff --git )")
_HEADER_PATH_RE = re.compile(r"^diff --git (?:\"?a/(?P<a>.+?)\"?) (?:\"?b/(?P<b>.+?)\"?)\s*$")
_HEADER_LINE_PREFIXES = (
    "diff --git ",
    "index ",
    "--- ",
    "+++ ",
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
    "similarity index",
    "rename from",
    "rename to",
    "copy from",
    "copy to",
    "Binary files",
)


@dataclass
class FileDiff:
    path: str
    text: str
    old_path: str | None = None

    @property
    def cost(self) -> int:
        return char_cost(self.text)


def split_diff(diff: str) -> tuple[str, list[FileDiff]]:
    """``(preamble, files)`` – preamble is any text before the first ``diff --git`` line."""
    chunks = _FILE_START_RE.split(diff)
    preamble = ""
    files: list[FileDiff] = []
    for chunk in chunks:
        if not chunk:
            continue
        if not chunk.startswith("diff --git "):
            preamble += chunk
            continue
        first = chunk.split("\n", 1)[0]
        m = _HEADER_PATH_RE.match(first)
        path = (m.group("b") or m.group("a")) if m else first[len("diff --git ") :].strip()
        old = m.group("a") if m else None
        files.append(FileDiff(path=path, text=chunk if chunk.endswith("\n") else chunk + "\n", old_path=old if old != path else None))
    return preamble, files


def omitted_marker(path: str, chars: int) -> str:
    return f'[… {chars} chars of the diff of {path} omitted; use git_diff with paths=["{path}"] …]\n'


def truncate_file_diff(fd: FileDiff, allowance: int) -> tuple[str, bool]:
    if fd.cost <= allowance:
        return fd.text, False
    lines = fd.text.splitlines(keepends=True)
    marker_cost = char_cost(omitted_marker(fd.path, len(fd.text)))
    room = allowance - marker_cost
    if room <= 0:
        return clip_to_cost(omitted_marker(fd.path, len(fd.text)), max(0, allowance)), True
    out: list[str] = []
    used = 0
    for line in lines:
        c = char_cost(line)
        if used + c > room:
            if line.startswith(_HEADER_LINE_PREFIXES) and not out:
                out.append(clip_to_cost(line, max(0, room)).rstrip("\n") + "\n")
            break
        out.append(line)
        used += c
    kept = "".join(out)
    return kept + omitted_marker(fd.path, len(fd.text) - len(kept)), True


@dataclass
class DiffRender:
    body: str
    files_total: int
    files_shown: int
    truncated: bool
    dropped: list[tuple[str, str]] = field(default_factory=list)


def render_diff(diff: str, budget: int, *, min_file_cost: int = 400, exclude: Callable[[str], bool] | None = None) -> DiffRender:
    """Budgeted diff; file diffs whose (old or new) path matches ``exclude`` are left out entirely."""
    preamble, files = split_diff(diff)
    excluded: list[tuple[str, str]] = []
    if exclude is not None:
        kept: list[FileDiff] = []
        for f in files:
            if exclude(f.path) or (f.old_path is not None and exclude(f.old_path)):
                excluded.append((f.path, "excluded"))
            else:
                kept.append(f)
        files = kept
    if excluded:
        note = f"[changes to {len(excluded)} excluded path(s) not shown]\n"
        out = _render_files(preamble, files, max(0, budget - char_cost(note)), min_file_cost)
        out.body = (out.body + "\n" + note.rstrip("\n")).lstrip("\n") if char_cost(note) <= budget else out.body
        out.dropped = excluded + out.dropped
        out.files_total += len(excluded)
        return out
    return _render_files(preamble, files, budget, min_file_cost)


def _render_files(preamble: str, files: list[FileDiff], budget: int, min_file_cost: int) -> DiffRender:
    if not files:
        text = preamble.strip("\n")
        if not text.strip():
            return DiffRender("", 0, 0, False)
        if char_cost(text) <= budget:
            return DiffRender(text, 0, 0, False)
        return DiffRender(clip_to_cost(text, max(0, budget - 2)).rstrip("\n") + "\n…", 0, 0, True)

    total = sum(f.cost for f in files)
    if total <= budget:
        return DiffRender("".join(f.text for f in files).rstrip("\n"), len(files), len(files), False)

    # how many files can be shown at all (in diff order); the rest is listed by name
    reserve_list = char_cost("[+0 more changed files not shown: ]\n") + sum(char_cost(f.path) + 2 for f in files)
    reserve_list = min(reserve_list, max(0, budget // 5))
    usable = budget - reserve_list
    shown_n = len(files)
    while shown_n > 0 and sum(min(f.cost, min_file_cost) for f in files[:shown_n]) > usable:
        shown_n -= 1
    if shown_n == len(files):
        usable = budget  # nothing to list
    shown = files[:shown_n]
    hidden = files[shown_n:]

    # water-filling: smallest diffs complete, the rest share what remains evenly
    allowance: dict[int, int] = {}
    remaining = usable
    order = sorted(range(len(shown)), key=lambda i: (shown[i].cost, i))
    for pos, i in enumerate(order):
        share = remaining // (len(order) - pos)
        allowance[i] = min(shown[i].cost, share)
        remaining -= allowance[i]

    parts: list[str] = []
    dropped: list[tuple[str, str]] = []
    truncated = False
    for i, fd in enumerate(shown):
        text, cut = truncate_file_diff(fd, allowance[i])
        truncated = truncated or cut
        parts.append(text)
    if hidden:
        truncated = True
        names = [f.path for f in hidden]
        for n in names:
            dropped.append((n, "budget"))
        line = f"[+{len(names)} more changed files not shown: {', '.join(names)}]\n"
        room = budget - sum(char_cost(p) for p in parts)
        if char_cost(line) > room:
            line = clip_to_cost(line, max(0, room - 2)).rstrip("\n") + "…]\n" if room > 8 else ""
        parts.append(line)
    body = "".join(parts).rstrip("\n")
    if char_cost(body) > budget:  # pragma: no cover - defensive
        body = clip_to_cost(body, budget)
    return DiffRender(body, len(files), len(shown), truncated, dropped)
