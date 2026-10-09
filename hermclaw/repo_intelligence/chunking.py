"""Symbol-aligned chunking for the semantic index (P11 11.8).

* top-level definitions (classes, functions …) become their own chunks; the code between them forms module chunks;
* oversized definitions are split at their members (methods) and, if still too large, into line windows with
  ``overlap`` lines of overlap;
* small adjacent segments are merged up to the maximum so the index does not drown in one-line chunks;
* every chunk carries file, symbol, line range, language and a content hash over the exact embedding input.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.repo_intelligence.config import INDEX_VERSION, RepoIntelConfig
from hermclaw.repo_intelligence.schemas import SymbolRecord

CHARS_PER_TOKEN = 3.2  # same conservative estimate as hermclaw.models.tokens
_CONTAINER_KINDS = ("class", "interface", "trait", "enum")
_DEF_KINDS = ("function", "method", "class", "interface", "trait", "enum", "type", "table", "view")


@dataclass(frozen=True)
class Chunk:
    path: str
    language: str
    start_line: int
    end_line: int
    content: str
    symbol: str | None
    content_hash: str
    embed_text: str


@dataclass
class _Seg:
    start: int  # 1-based inclusive
    end: int
    symbols: list[str]


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def document_text(path: str, symbol: str | None, content: str, cfg: RepoIntelConfig) -> str:
    title = f"{path} {symbol}" if symbol else path
    return cfg.embed_document_template.format(title=title, text=DEFAULT_REDACTOR.text(content))


def query_text(query: str, cfg: RepoIntelConfig) -> str:
    return cfg.embed_query_template.format(query=DEFAULT_REDACTOR.text(query))


def content_hash(embed_text: str) -> str:
    return hashlib.sha256(f"v{INDEX_VERSION}\n{embed_text}".encode()).hexdigest()


def _top_level(symbols: Sequence[SymbolRecord]) -> list[SymbolRecord]:
    defs = sorted((s for s in symbols if s.kind in _DEF_KINDS and s.end_line >= s.start_line), key=lambda s: (s.start_line, -s.end_line))
    out: list[SymbolRecord] = []
    last_end = 0
    for s in defs:
        if s.start_line > last_end:
            out.append(s)
            last_end = s.end_line
    return out


def _members(sym: SymbolRecord, symbols: Sequence[SymbolRecord]) -> list[SymbolRecord]:
    inner = [s for s in symbols if s is not sym and s.start_line > sym.start_line and s.end_line <= sym.end_line and s.kind in _DEF_KINDS]
    return _top_level(inner)


class Chunker:
    def __init__(self, cfg: RepoIntelConfig | None = None) -> None:
        self.cfg = cfg or RepoIntelConfig()
        self.max_chars = int(self.cfg.chunk_max_tokens * CHARS_PER_TOKEN)
        self.min_chars = int(self.cfg.chunk_min_tokens * CHARS_PER_TOKEN)

    def _size(self, lines: list[str], seg: _Seg) -> int:
        return sum(len(x) for x in lines[seg.start - 1 : seg.end])

    def _windows(self, lines: list[str], seg: _Seg) -> list[_Seg]:
        out: list[_Seg] = []
        i = seg.start
        overlap = max(0, self.cfg.chunk_overlap_lines)
        while i <= seg.end:
            size, j = 0, i
            while j <= seg.end and (size + len(lines[j - 1]) <= self.max_chars or j == i):
                size += len(lines[j - 1])
                j += 1
            end = j - 1
            # prefer to break at a blank line in the last third of the window
            if end < seg.end:
                for k in range(end, i + (end - i) * 2 // 3, -1):
                    if not lines[k - 1].strip():
                        end = k
                        break
            out.append(_Seg(i, end, list(seg.symbols)))
            if end >= seg.end:
                break
            i = max(i + 1, end + 1 - overlap)
        return out

    def _split(self, lines: list[str], seg: _Seg, sym: SymbolRecord | None, symbols: Sequence[SymbolRecord]) -> list[_Seg]:
        if self._size(lines, seg) <= self.max_chars:
            return [seg]
        if sym is not None and sym.kind in _CONTAINER_KINDS:
            members = _members(sym, symbols)
            if members:
                parts = self._segments(lines, seg.start, seg.end, members, symbols=symbols, owner=sym.name)
                out: list[_Seg] = []
                for p in parts:
                    out.extend(self._windows(lines, p) if self._size(lines, p) > self.max_chars else [p])
                return out
        return self._windows(lines, seg)

    def _segments(
        self,
        lines: list[str],
        lo: int,
        hi: int,
        defs: Sequence[SymbolRecord],
        *,
        symbols: Sequence[SymbolRecord],
        owner: str | None = None,
    ) -> list[_Seg]:
        segs: list[_Seg] = []
        cur = lo
        base = [owner] if owner else []
        for d in defs:
            s, e = max(lo, d.start_line), min(hi, d.end_line)
            if s > hi or e < lo:
                continue
            if s > cur:
                segs.append(_Seg(cur, s - 1, list(base)))
            name = f"{owner}.{d.name}" if owner else d.name
            segs.extend(self._split(lines, _Seg(s, e, [name]), d, symbols))
            cur = e + 1
        if cur <= hi:
            segs.append(_Seg(cur, hi, list(base)))
        return [g for g in segs if any(lines[i - 1].strip() for i in range(g.start, g.end + 1))]

    def _merge(self, lines: list[str], segs: list[_Seg]) -> list[_Seg]:
        out: list[_Seg] = []
        for seg in segs:
            if out:
                prev = out[-1]
                psize, ssize = self._size(lines, prev), self._size(lines, seg)
                contiguous = seg.start > prev.end and all(not lines[i - 1].strip() for i in range(prev.end + 1, seg.start))
                if contiguous and (psize < self.min_chars or ssize < self.min_chars) and psize + ssize <= self.max_chars:
                    prev.end = seg.end
                    prev.symbols.extend(s for s in seg.symbols if s not in prev.symbols)
                    continue
            out.append(seg)
        return out

    def chunk(self, path: str, language: str, text: str, symbols: Sequence[SymbolRecord] = ()) -> list[Chunk]:
        lines = text.splitlines(keepends=True)
        if not lines:
            return []
        n = len(lines)
        defs = _top_level(symbols)
        segs = self._segments(lines, 1, n, defs, symbols=symbols)
        segs = self._merge(lines, segs)
        chunks: list[Chunk] = []
        for seg in segs:
            content = "".join(lines[seg.start - 1 : seg.end])
            if not content.strip():
                continue
            if len(content) > self.max_chars:  # a single overlong line (minified / data)
                content = content[: self.max_chars]
            symbol = ", ".join(seg.symbols[:3])[:300] if seg.symbols else None
            emb = document_text(path, symbol, content, self.cfg)
            chunks.append(
                Chunk(
                    path=path,
                    language=language,
                    start_line=seg.start,
                    end_line=seg.end,
                    content=content,
                    symbol=symbol,
                    content_hash=content_hash(emb),
                    embed_text=emb,
                )
            )
        return chunks
