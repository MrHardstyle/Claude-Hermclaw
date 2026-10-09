"""Output budgeting helpers: every tool output that reaches a prompt is bounded (``policies.coder.tool_output_chars``)."""

from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def clip_head(text: str, limit: int) -> tuple[str, bool]:
    """Keep the beginning of ``text`` (file contents, listings). Returns ``(text, truncated)``."""
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    marker = f"\n…[truncated: {len(text) - limit} more chars]"
    keep = max(0, limit - len(marker))
    cut = text.rfind("\n", 0, keep)
    if cut < keep // 2:
        cut = keep
    return text[:cut] + marker, True


def clip_middle(text: str, limit: int, *, head_ratio: float = 0.3) -> tuple[str, bool]:
    """Keep head and tail of ``text`` (command output: errors usually appear at the end)."""
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    marker = f"\n…[{len(text) - limit} chars omitted]…\n"
    budget = max(0, limit - len(marker))
    head = int(budget * head_ratio)
    tail = budget - head
    return text[:head] + marker + (text[-tail:] if tail else ""), True


def excerpt(text: str, limit: int = 8000) -> str:
    """Bounded excerpt for persistence (command_runs / test_runs)."""
    return clip_middle(text, limit)[0]
