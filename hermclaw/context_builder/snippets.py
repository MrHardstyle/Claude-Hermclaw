"""Code/test snippets: ranking (16.3), deduplication (16.4) and budgeted packing.

A snippet is a line range of one repository file. ``exact`` snippets carry exactly the lines ``start..end``;
only those can be merged line-accurately. Identical and overlapping (or nearly adjacent) ranges of the same file
are merged into one snippet; identical content is never shown twice (e.g. vendored copies).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace

from hermclaw.context_builder.tokens import char_cost

ORIGIN_PRIORITY = ("failure", "target", "acceptance", "search", "context")


@dataclass(frozen=True)
class Snippet:
    path: str
    start: int
    end: int
    text: str
    score: float
    origins: tuple[str, ...]
    exact: bool = True

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.path, self.start, self.end)

    @property
    def label(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"

    def content_digest(self) -> str:
        normalised = "\n".join(line.rstrip() for line in self.text.splitlines()).strip()
        return hashlib.sha256(normalised.encode("utf-8", errors="surrogateescape")).hexdigest()


def make_snippet(path: str, start: int, text: str, score: float, origin: str, *, end: int | None = None) -> Snippet:
    """Snippet whose range is derived from ``text`` (exact) unless ``end`` disagrees with the line count."""
    start = max(1, start)
    n = max(1, len(text.splitlines()))
    derived_end = start + n - 1
    if end is None or end == derived_end:
        return Snippet(path, start, derived_end, text, score, (origin,), exact=True)
    return Snippet(path, start, max(start, end), text, score, (origin,), exact=False)


def rank_key(s: Snippet) -> tuple[float, str, int, int]:
    return (-s.score, s.path, s.start, s.end)


def _origins(*groups: Iterable[str]) -> tuple[str, ...]:
    seen = {o for g in groups for o in g}
    return tuple(o for o in ORIGIN_PRIORITY if o in seen) + tuple(sorted(seen - set(ORIGIN_PRIORITY)))


def _line_map(s: Snippet) -> dict[int, str]:
    return {s.start + i: line for i, line in enumerate(s.text.splitlines())}


Reader = Callable[[str, int, int], Awaitable[str | None]]


@dataclass
class DedupResult:
    snippets: list[Snippet]
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (label, reason)
    merged: int = 0  # snippets folded into a larger range


async def dedupe_snippets(candidates: Sequence[Snippet], *, merge_gap: int, reader: Reader, max_lines: int) -> DedupResult:
    """Merge identical/overlapping ranges per file and drop repeated content. Deterministic for equal input.

    Overlapping exact snippets are merged line-accurately; if the union is not fully covered by known lines it is
    re-read through ``reader``. Non-exact snippets are only deduplicated by key/content, never merged. A merged
    range never grows beyond ``max_lines``: such a group is kept as separate snippets.
    """
    result = DedupResult(snippets=[])
    by_key: dict[tuple[str, int, int], Snippet] = {}
    for s in candidates:
        prev = by_key.get(s.key)
        if prev is None:
            by_key[s.key] = s
            continue
        result.dropped.append((s.label, "duplicate"))
        best = prev if rank_key(prev) <= rank_key(s) else s
        by_key[s.key] = Snippet(
            best.path, best.start, best.end, best.text, max(prev.score, s.score), _origins(prev.origins, s.origins), best.exact
        )

    per_path: dict[str, list[Snippet]] = {}
    for s in sorted(by_key.values(), key=lambda x: (x.path, x.start, x.end)):
        per_path.setdefault(s.path, []).append(s)

    merged: list[Snippet] = []
    for path in sorted(per_path):
        exact = [s for s in per_path[path] if s.exact]
        merged.extend(s for s in per_path[path] if not s.exact)
        group: list[Snippet] = []
        for s in exact:
            if group and s.start <= max(g.end for g in group) + merge_gap + 1:
                lo = min(g.start for g in group)
                hi = max(*(g.end for g in group), s.end)
                if hi - lo + 1 <= max_lines:
                    group.append(s)
                    continue
            if group:
                merged.extend(await _merge_group(group, reader, result))
            group = [s]
        if group:
            merged.extend(await _merge_group(group, reader, result))

    exact_ranges: dict[str, list[tuple[int, int]]] = {}
    for s in merged:
        if s.exact:
            exact_ranges.setdefault(s.path, []).append((s.start, s.end))
    seen_content: set[str] = set()
    for s in sorted(merged, key=rank_key):
        if not s.exact and any(lo <= s.start and s.end <= hi for lo, hi in exact_ranges.get(s.path, [])):
            result.dropped.append((s.label, "contained"))
            continue
        if not s.text.strip():
            result.dropped.append((s.label, "empty"))
            continue
        digest = s.content_digest()
        if digest in seen_content:
            result.dropped.append((s.label, "duplicate_content"))
            continue
        seen_content.add(digest)
        result.snippets.append(s)
    return result


async def _merge_group(group: list[Snippet], reader: Reader, result: DedupResult) -> list[Snippet]:
    if len(group) == 1:
        return group
    lo = min(g.start for g in group)
    hi = max(g.end for g in group)
    lines: dict[int, str] = {}
    for g in group:
        lines.update(_line_map(g))
    score = max(g.score for g in group)
    origins = _origins(*(g.origins for g in group))
    path = group[0].path
    if all(i in lines for i in range(lo, hi + 1)):
        result.merged += len(group) - 1
        text = "\n".join(lines[i] for i in range(lo, hi + 1))
        return [Snippet(path, lo, hi, text, score, origins, exact=True)]
    fresh = await reader(path, lo, hi)
    if fresh is not None and fresh.strip():
        result.merged += len(group) - 1
        return [replace(make_snippet(path, lo, fresh, score, origins[0]), origins=origins)]
    # the gap could not be read: keep the parts (still deduplicated by key/content)
    return group


def fence_for(text: str) -> str:
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def render_snippet(s: Snippet, *, shown_end: int | None = None, text: str | None = None) -> str:
    body = s.text if text is None else text
    end = s.end if shown_end is None else shown_end
    fence = fence_for(body)
    header = f"### {s.path} lines {s.start}-{end} [{', '.join(s.origins)}]"
    out = f"{header}\n{fence}\n{body.rstrip(chr(10))}\n{fence}\n"
    if shown_end is not None and shown_end < s.end:
        out += f"[… lines {shown_end + 1}-{s.end} not shown; use read_range to see them …]\n"
    return out


@dataclass
class PackResult:
    selected: list[tuple[Snippet, str]]  # (snippet, rendered block) in rank order
    cost: int
    dropped: list[tuple[str, str]]
    truncated: bool


def pack_snippets(snippets: Sequence[Snippet], budget: int, *, min_cost: int, max_items: int) -> PackResult:
    """First-fit packing in rank order; a snippet that does not fit is clipped (head lines) when at least
    ``min_cost`` remains, otherwise skipped (smaller lower-ranked snippets may still fit)."""
    selected: list[tuple[Snippet, str]] = []
    dropped: list[tuple[str, str]] = []
    used = 0
    truncated = False
    for s in sorted(snippets, key=rank_key):
        if len(selected) >= max_items:
            dropped.append((s.label, "limit"))
            continue
        block = render_snippet(s)
        cost = char_cost(block) + 1  # +1: blank line between blocks
        if used + cost <= budget:
            selected.append((s, block))
            used += cost
            continue
        remaining = budget - used
        if remaining >= min_cost and s.exact:
            clipped = _clip_snippet(s, remaining - 1)
            if clipped is not None:
                selected.append((s, clipped))
                used += char_cost(clipped) + 1
                truncated = True
                continue
        dropped.append((s.label, "budget"))
    return PackResult(selected=selected, cost=used, dropped=dropped, truncated=truncated or bool(dropped))


def _clip_snippet(s: Snippet, budget: int) -> str | None:
    lines = s.text.splitlines()
    lo, hi, best = 1, len(lines) - 1, None
    while lo <= hi:  # largest number of head lines whose rendering fits
        mid = (lo + hi) // 2
        block = render_snippet(s, shown_end=s.start + mid - 1, text="\n".join(lines[:mid]))
        if char_cost(block) <= budget:
            best, lo = block, mid + 1
        else:
            hi = mid - 1
    return best


def render_packed(selected: Sequence[tuple[Snippet, str]]) -> str:
    """Blocks grouped by file (files ordered by their best rank), ascending line order inside a file."""
    first_rank: dict[str, int] = {}
    for i, (s, _) in enumerate(selected):
        first_rank.setdefault(s.path, i)
    ordered = sorted(selected, key=lambda item: (first_rank[item[0].path], item[0].start, item[0].end))
    return "\n".join(block for _, block in ordered)
