"""Compact step history (step 16.6) – never the chat, never model reasoning.

The last ``full_turns`` turns are shown as one digest line each (tool, args digest, ok/failed, error code, result
digest). Older turns are folded into counts per tool plus their key results (last failure per tool, files changed).
Only runtime-produced digests are used; ``TurnRecord`` has no field for model reasoning and
:func:`strip_reasoning` removes reasoning markup that might have slipped into a digest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hermclaw.context_builder.failure import clip_line
from hermclaw.context_builder.tokens import char_cost, clip_to_cost

_REASONING_BLOCK_RE = re.compile(
    r"(?is)<\s*(think|thinking|reasoning|reflection|scratchpad|analysis)\s*>.*?(?:<\s*/\s*\1\s*>|\Z)"
    r"|<\|channel\|>\s*analysis.*?(?:<\|end\|>|\Z)"
)
_REASONING_CLOSE_RE = re.compile(r"(?is)\A.*?<\s*/\s*(?:think|thinking|reasoning)\s*>")


def strip_reasoning(text: str) -> str:
    """Remove model reasoning markup (``<think>…</think>`` and friends, unterminated blocks to the end)."""
    out = _REASONING_BLOCK_RE.sub("", text)
    return _REASONING_CLOSE_RE.sub("", out)  # a dangling close tag: everything before it was reasoning


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "ok")
    return bool(value)


@dataclass(frozen=True)
class TurnRecord:
    """One executed tool turn as recorded by the runtime (digests, not raw outputs)."""

    turn: int
    tool: str
    args_digest: str = ""
    ok: bool = True
    result_digest: str = ""
    error_code: str | None = None
    mutated_paths: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TurnRecord:
        """Build from a stored row/dict. Unknown keys (e.g. ``status``, ``decision``, ``reasoning``) are ignored."""
        paths = data.get("mutated_paths") or ()
        error_code = data.get("error_code")
        return cls(
            turn=int(data.get("turn", 0)),
            tool=str(data.get("tool", "")),
            args_digest=str(data.get("args_digest", "") or ""),
            ok=_as_bool(data.get("ok", True)),
            result_digest=str(data.get("result_digest", "") or ""),
            error_code=str(error_code) if error_code else None,
            mutated_paths=tuple(str(p) for p in paths),
        )


@dataclass(frozen=True)
class HistoryLimits:
    full_turns: int = 6
    args_chars: int = 240
    result_chars: int = 320
    key_result_chars: int = 140


def _digest(text: str, limit: int) -> str:
    return clip_line(strip_reasoning(text), limit)


def _full_line(r: TurnRecord, limits: HistoryLimits) -> str:
    outcome = "ok" if r.ok else "FAILED"
    if r.error_code:
        outcome += f" [{clip_line(r.error_code, 60)}]"
    args = _digest(r.args_digest, limits.args_chars)
    result = _digest(r.result_digest, limits.result_chars)
    line = f"- turn {r.turn}: {clip_line(r.tool, 60)}"
    if args:
        line += f" {args}"
    line += f" -> {outcome}"
    if result:
        line += f": {result}"
    if r.mutated_paths:
        line += f" (changed: {', '.join(sorted(set(r.mutated_paths))[:8])})"
    return line


def _summary_lines(records: Sequence[TurnRecord], limits: HistoryLimits) -> list[str]:
    if not records:
        return []
    first, last = records[0].turn, records[-1].turn
    order: list[str] = []
    stats: dict[str, dict[str, Any]] = {}
    changed: list[str] = []
    for r in records:
        tool = clip_line(r.tool, 60)
        if tool not in stats:
            order.append(tool)
            stats[tool] = {"n": 0, "ok": 0, "codes": {}, "last_fail": "", "last_ok": ""}
        st = stats[tool]
        st["n"] += 1
        if r.ok:
            st["ok"] += 1
            st["last_ok"] = r.result_digest
        else:
            code = r.error_code or "error"
            st["codes"][code] = st["codes"].get(code, 0) + 1
            st["last_fail"] = r.result_digest or code
        for p in r.mutated_paths:
            if p not in changed:
                changed.append(p)
    parts = []
    for tool in order:
        st = stats[tool]
        failed = st["n"] - st["ok"]
        part = f"{tool} x{st['n']} ({st['ok']} ok"
        if failed:
            codes = ", ".join(f"{c} x{n}" for c, n in sorted(st["codes"].items()))
            part += f", {failed} failed: {codes}"
        parts.append(part + ")")
    lines = [f"- turns {first}-{last} (summary): " + "; ".join(parts)]
    for tool in order:
        st = stats[tool]
        if st["last_fail"]:
            lines.append(f"  last {tool} failure: {_digest(st['last_fail'], limits.key_result_chars)}")
        elif st["last_ok"] and tool.startswith(("run_", "complete", "checkpoint")):
            lines.append(f"  last {tool} result: {_digest(st['last_ok'], limits.key_result_chars)}")
    if changed:
        shown = sorted(changed)[:20]
        more = f" (+{len(changed) - len(shown)} more)" if len(changed) > len(shown) else ""
        lines.append(f"  files changed so far: {', '.join(shown)}{more}")
    return lines


@dataclass(frozen=True)
class HistoryRender:
    body: str
    full_turns: int
    summarised_turns: int
    truncated: bool


def render_history(records: Sequence[TurnRecord], budget: int, limits: HistoryLimits | None = None) -> HistoryRender:
    """Render the compact history so that it fits ``budget`` (character equivalents)."""
    lim = limits or HistoryLimits()
    recs = sorted(records, key=lambda r: r.turn)
    if not recs or budget <= 0:
        return HistoryRender("", 0, 0, bool(recs))
    n_full = min(max(0, lim.full_turns), len(recs))
    shrink = 0
    while True:
        cur = HistoryLimits(
            full_turns=n_full,
            args_chars=max(40, lim.args_chars >> shrink),
            result_chars=max(60, lim.result_chars >> shrink),
            key_result_chars=max(40, lim.key_result_chars >> shrink),
        )
        older, recent = recs[: len(recs) - n_full], recs[len(recs) - n_full :]
        lines = _summary_lines(older, cur) + [_full_line(r, cur) for r in recent]
        body = "\n".join(lines)
        truncated = shrink > 0 or n_full < min(lim.full_turns, len(recs))
        if char_cost(body) <= budget:
            return HistoryRender(body, len(recent), len(older), truncated)
        if n_full > 1:
            n_full -= 1
        elif shrink < 3:
            shrink += 1
        else:
            clipped = clip_to_cost(body, max(0, budget - 2)).rstrip() + "\n…"
            if char_cost(clipped) > budget:
                clipped = clip_to_cost(body, budget)
            return HistoryRender(clipped, len(recent), len(older), True)
