"""Small text helpers of the review stage: clipping with explicit omission markers, safe code fences, redaction."""

from __future__ import annotations

import json
import re
from typing import Any

from hermclaw.core.redaction import DEFAULT_REDACTOR

_BACKTICK_RUN = re.compile(r"`{3,}")
_REASONING_TAGS = "think|thinking|reasoning|thought|scratchpad"
_REASONING_BLOCK = re.compile(rf"<(?P<tag>{_REASONING_TAGS})\b[^>]*>.*?(?:</(?P=tag)\s*>|\Z)", re.IGNORECASE | re.DOTALL)
_REASONING_CLOSE = re.compile(rf"^.*?</(?:{_REASONING_TAGS})\s*>", re.IGNORECASE | re.DOTALL)
_WS = re.compile(r"\s+")
_NOTE_RESERVE = 40


def redact(text: str) -> str:
    """Mask secrets before text reaches a prompt, a database row or an event."""
    return DEFAULT_REDACTOR.text(text) if text else text


def strip_reasoning(text: str) -> str:
    """Remove model reasoning markup (``<think>…</think>`` and friends; an unterminated block runs to the end, a
    dangling close tag means everything before it was reasoning). Reasoning is never stored or displayed."""
    if not text or "<" not in text:
        return text
    out = _REASONING_BLOCK.sub("", text)
    return _REASONING_CLOSE.sub("", out).strip()


def clip(text: str, limit: int, *, what: str = "chars") -> str:
    """``text`` shortened to at most ~``limit`` chars; a marker states how much was omitted."""
    if limit <= 0:
        return f"…[{len(text)} {what} omitted]" if text else ""
    if len(text) <= limit:
        return text
    marker = f"…[{len(text) - limit} {what} omitted]"
    keep = max(0, limit - len(marker))
    return text[:keep] + marker


def clip_lines(text: str, limit: int, *, what: str = "lines") -> tuple[str, bool]:
    """Keep whole lines from the start while they fit into ``limit`` chars. Returns (text, truncated)."""
    if len(text) <= limit:
        return text, False
    lines = text.split("\n")
    budget = max(0, limit - _NOTE_RESERVE)
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > budget:
            break
        out.append(line)
        used += cost
    remaining = len(lines) - len(out)
    if not out and budget > _NOTE_RESERVE:  # one huge first line: keep its beginning
        out.append(clip(lines[0], budget))
        remaining -= 1
    if remaining > 0:
        out.append(f"…[{remaining} more {what} omitted]")
    return "\n".join(out), True


def fence(body: str, lang: str = "") -> str:
    """A Markdown code fence that the body cannot close (longer than any backtick run inside it)."""
    longest = max((len(m.group(0)) for m in _BACKTICK_RUN.finditer(body)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{body.rstrip(chr(10))}\n{ticks}"


def compact_json(value: Any, limit: int) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(value)
    return clip(redact(raw), limit)


def one_line(text: str) -> str:
    return _WS.sub(" ", text).strip()
