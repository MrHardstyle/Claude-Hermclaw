"""Error preservation (step 16.5) and generic head/tail truncation.

The latest failure is the most valuable part of a correction turn, so it is kept *verbatim* whenever it fits its
budget. Only when it is larger, head and tail are kept with an explicit ``[… n chars omitted …]`` marker – and the
first error line(s) plus the final summary lines of test output are always kept in full (as long as they fit the
hard budget at all).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from hermclaw.context_builder.tokens import char_cost, clip_tail_to_cost, clip_to_cost

# first error line(s): generic markers of compiler/interpreter/test-runner errors
_ERROR_LINE_RE = re.compile(
    r"(?i)(?:^\s*E\s{2,}\S|^\s*Traceback \(most recent call last\)|\b(?:error|exception|fatal|panic(?:ked)?|failed|failure)\b|"
    r"^\s*(?:FAIL|FAILED|ERROR)\b|\bassert(?:ion)?\b|\bnot ok\b|segmentation fault|undefined reference|cannot find)"
)
# final summary lines of common test runners / build tools
_SUMMARY_LINE_RE = re.compile(
    r"(?i)(?:^=+ .*\b(?:passed|failed|error|errors|skipped|deselected|xfailed|no tests ran)\b.*=+\s*$|"
    r"^\s*(?:FAILED|ERROR)\s+\S+|^\s*Tests?:\s+\d+|^\s*Test Suites?:\s|^\s*(?:ok|FAIL)\s+\S+\s|"
    r"^\s*(?:Ran \d+ tests?|OK(?: \(.*\))?$|FAILED \(.*\)$)|^\s*test result:|^\s*\d+ (?:passing|failing|pending)\b|"
    r"\b\d+ (?:passed|failed|errors?)\b|^\s*(?:make|npm|error):?.*\b(?:Error|ERR!)\b|exit (?:code|status) \d+)"
)
_FIRST_ERROR_FOLLOW_LINES = 6
_SUMMARY_SCAN_LINES = 40
_MAX_MARKERS = 4


def omission_marker(chars: int) -> str:
    return f"[… {chars} chars omitted …]\n"


def _marker_reserve() -> int:
    return _MAX_MARKERS * char_cost(omission_marker(10**9))


def first_error_block(lines: list[str]) -> list[int]:
    """Indices of the first error line and its directly following detail lines (until a blank line)."""
    for i, line in enumerate(lines):
        if _ERROR_LINE_RE.search(line):
            block = [i]
            for j in range(i + 1, min(len(lines), i + 1 + _FIRST_ERROR_FOLLOW_LINES)):
                if not lines[j].strip():
                    break
                block.append(j)
            return block
    return []


def summary_block(lines: list[str]) -> list[int]:
    """Indices of the final summary lines (matching runner summaries in the tail) plus the last non-empty line."""
    out: list[int] = []
    start = max(0, len(lines) - _SUMMARY_SCAN_LINES)
    for i in range(start, len(lines)):
        if _SUMMARY_LINE_RE.search(lines[i]):
            out.append(i)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            if i not in out:
                out.append(i)
            break
    return sorted(out)


def first_error_line(text: str) -> str:
    """The first error-looking line (stripped), else the first non-empty line."""
    lines = text.splitlines()
    block = first_error_block(lines)
    if block:
        return lines[block[0]].strip()
    return next((ln.strip() for ln in lines if ln.strip()), "")


def _render(lines: list[str], keep: Iterable[int]) -> str:
    kept = sorted(set(keep))
    out: list[str] = []
    prev = -1
    for idx in kept:
        if idx > prev + 1:
            out.append(omission_marker(sum(len(ln) for ln in lines[prev + 1 : idx])))
        out.append(lines[idx])
        prev = idx
    if prev < len(lines) - 1:
        out.append(omission_marker(sum(len(ln) for ln in lines[prev + 1 :])))
    return "".join(out)


def preserve_failure(text: str, max_cost: int) -> tuple[str, bool]:
    """``text`` verbatim if it fits ``max_cost``; else head + tail with omission markers, keeping the first error
    line(s) and the final summary lines in full. Returns ``(rendered, truncated)``. Never exceeds ``max_cost``."""
    if char_cost(text) <= max_cost:
        return text, False
    if max_cost <= 0:
        return "", True
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    reserve = _marker_reserve()
    budget = max_cost - reserve
    if budget <= 0 or len(lines) <= 1:
        return truncate_middle(text, max_cost), True

    costs = [char_cost(ln) for ln in lines]
    keep: set[int] = set()
    used = 0

    def take(idx: int) -> bool:
        nonlocal used
        if idx in keep:
            return True
        if used + costs[idx] > budget:
            return False
        keep.add(idx)
        used += costs[idx]
        return True

    must = first_error_block(lines) + summary_block(lines)
    # priority 1: first error line(s) and final summary lines; a single huge line is clipped (marked with …)
    for pos, idx in enumerate(must):
        if take(idx):
            continue
        remaining = budget - used
        is_first, is_last = pos == 0, idx == must[-1]
        if (is_first or is_last) and remaining > 8:
            allowance = remaining // 2 if is_first and not is_last else remaining
            lines[idx] = clip_to_cost(lines[idx].rstrip("\n"), allowance - 2) + "…\n"
            costs[idx] = char_cost(lines[idx])
            take(idx)
    # priority 2: head and tail context, alternating (head first – most runners print the cause early)
    head, tail = 0, len(lines) - 1
    while True:
        while head <= tail and head in keep:
            head += 1
        while tail >= head and tail in keep:
            tail -= 1
        if head > tail:
            break
        took_head = take(head)
        if took_head:
            head += 1
        while head <= tail and head in keep:
            head += 1
        took_tail = head <= tail and take(tail)
        if took_tail:
            tail -= 1
        if not took_head and not took_tail:
            break
    rendered = _render(lines, keep)
    if char_cost(rendered) > max_cost:  # pragma: no cover - defensive, markers are reserved
        rendered = clip_to_cost(rendered, max_cost)
    return rendered, True


def truncate_middle(text: str, max_cost: int) -> str:
    """Head and tail of ``text`` around an omission marker (character based), never above ``max_cost``."""
    if char_cost(text) <= max_cost:
        return text
    marker_cost = char_cost(omission_marker(len(text)))
    if max_cost <= marker_cost + 2:
        return clip_to_cost(text, max_cost)
    room = max_cost - marker_cost - 1
    head = clip_to_cost(text, room * 2 // 3)
    tail = clip_tail_to_cost(text[len(head) :], room - char_cost(head))
    omitted = len(text) - len(head) - len(tail)
    return head + ("\n" if not head.endswith("\n") else "") + omission_marker(omitted) + tail


def clip_line(text: str, max_cost: int) -> str:
    """Single-line clip with an ellipsis (newlines collapsed)."""
    flat = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    if char_cost(flat) <= max_cost:
        return flat
    return clip_to_cost(flat, max(0, max_cost - 1)) + "…"
