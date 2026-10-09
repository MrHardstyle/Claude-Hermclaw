"""Relevance fusion (Bauplan §14 Phase E; P11 11.10).

Six signals produce ranked candidate lists (raw scores, higher is better):

* ``lexical``        – ripgrep matches of the query's identifiers/terms (idf-weighted, whole-word bonus, coverage);
* ``symbol``         – definitions whose (qualified) names match identifiers/terms;
* ``structural``     – file path tokens, declared routes and tables matching the query;
* ``semantic``       – nearest chunks in the EmbeddingGemma/pgvector index;
* ``test_reference`` – tests that import a candidate or mention its matched symbols (tests relevant to the query
  count double);
* ``dependency``     – import-graph neighbours of the strongest preliminary candidates.

Fusion is Reciprocal Rank Fusion: ``rrf(d) = Σ_s w_s / (k + rank_s(d))`` (competition ranking for ties). The
reported ``score`` is ``rrf`` divided by the ideal value over every signal that produced candidates, so it lies in
``[0, 1]`` and a file ranked first by every active signal scores 1.0. A signal that does not list a file contributes
nothing, therefore files matching several signals rank first; among those, better ranks win. Before ranking, each
signal drops candidates scoring below ``signal_relative_floors[signal]`` x its best score: RRF only sees ranks, and a
one-word incidental match must not count as much as presence in a list as the dominant evidence does.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from hermclaw.repo_intelligence.config import RepoIntelConfig
from hermclaw.repo_intelligence.dependencies import DependencyGraph
from hermclaw.repo_intelligence.query import QueryTerms, idf, normalize_identifier, split_identifier, stem
from hermclaw.repo_intelligence.schemas import ChunkHit, FileHit, LexicalHit, Route, SignalHit, SymbolRecord

_RANGE_PRIORITY = ("symbol", "semantic", "lexical", "structural", "test_reference", "dependency")
_KIND_WEIGHT = {
    "class": 1.0, "function": 1.0, "method": 1.0, "interface": 1.0, "trait": 1.0, "enum": 0.9, "type": 0.9,
    "constant": 0.6, "route": 0.8, "table": 0.9, "view": 0.8,
}  # fmt: skip


def _ranked(hits: Iterable[SignalHit], depth: int, floor: float = 0.0) -> list[tuple[SignalHit, int]]:
    """Best hit per file, ordered by score; hits below ``floor`` x the signal's best score are noise and dropped."""
    best: dict[str, SignalHit] = {}
    for h in hits:
        if h.score <= 0:
            continue
        cur = best.get(h.path)
        if cur is None or h.score > cur.score:
            best[h.path] = h
    if best and floor > 0:
        cut = max(h.score for h in best.values()) * floor
        best = {p: h for p, h in best.items() if h.score >= cut}
    ordered = sorted(best.values(), key=lambda h: (-h.score, h.path))[:depth]
    out: list[tuple[SignalHit, int]] = []
    rank = 0
    prev: float | None = None
    for i, h in enumerate(ordered, start=1):
        if prev is None or h.score < prev - 1e-12:
            rank = i
            prev = h.score
        out.append((h, rank))
    return out


def fuse(signals: Mapping[str, Sequence[SignalHit]], cfg: RepoIntelConfig) -> list[FileHit]:
    k = max(1, cfg.rrf_k)
    rrf: dict[str, float] = defaultdict(float)
    raw: dict[str, dict[str, float]] = defaultdict(dict)
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    best: dict[str, dict[str, SignalHit]] = defaultdict(dict)
    active: list[str] = []
    for name, hits in signals.items():
        ranked = _ranked(hits, cfg.signal_depth, cfg.signal_floor(name))
        if not ranked:
            continue
        active.append(name)
        w = cfg.weight(name)
        for h, r in ranked:
            rrf[h.path] += w * max(0.0, min(1.0, h.boost)) / (k + r)
            raw[h.path][name] = round(h.score, 6)
            ranks[h.path][name] = r
            best[h.path][name] = h
    ideal = sum(cfg.weight(s) / (k + 1) for s in active) or 1.0
    out: list[FileHit] = []
    for path, value in rrf.items():
        sig = best[path]
        chosen = next((sig[s] for s in _RANGE_PRIORITY if s in sig), None)
        start = chosen.start_line if chosen else 1
        end = max(start, chosen.end_line) if chosen else 1
        reasons = [f"{s}#{ranks[path][s]}" + (f" {sig[s].detail}" if sig[s].detail else "") for s in _RANGE_PRIORITY if s in sig]
        out.append(
            FileHit(
                path=path,
                score=round(min(1.0, value / ideal), 6),
                rrf=round(value, 8),
                signals=dict(raw[path]),
                ranks=dict(ranks[path]),
                start_line=start,
                end_line=end,
                reasons=reasons[:8],
            )
        )
    out.sort(key=lambda h: (-h.rrf, h.path))
    return out


# ============================================================================================= signal builders
def _word_bounded(text: str, needle: str) -> bool:
    lo, nd = text.lower(), needle.lower()
    i = lo.find(nd)
    while i != -1:
        before = lo[i - 1] if i > 0 else " "
        after = lo[i + len(nd)] if i + len(nd) < len(lo) else " "
        if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
            return True
        i = lo.find(nd, i + 1)
    return False


def lexical_signal(hits: Sequence[LexicalHit], q: QueryTerms, n_files: int, *, context: int = 3) -> list[SignalHit]:
    patterns = q.lexical_patterns
    if not patterns or not hits:
        return []
    weight: dict[str, float] = {}
    for p in q.phrases:
        weight.setdefault(p.lower(), 3.0)
    for p in q.identifiers:
        weight.setdefault(p.lower(), 2.0)
    for p in q.routes:
        weight.setdefault(p.lower(), 2.0)
    for p in q.terms:
        weight.setdefault(p.lower(), 1.0)
    by_file: dict[str, list[LexicalHit]] = defaultdict(list)
    for h in hits:
        by_file[h.path].append(h)
    counts: dict[str, dict[str, float]] = {}
    best_line: dict[str, tuple[int, int]] = {}
    df: dict[str, int] = defaultdict(int)
    for path, fhits in by_file.items():
        c: dict[str, float] = defaultdict(float)
        top = (0, fhits[0].line)
        for h in fhits:
            found = {m.lower() for m in h.matches} or {p for p in weight if p in h.text.lower()}
            distinct = 0
            for p in found:
                if p not in weight:
                    continue
                c[p] += 1.0 if _word_bounded(h.text, p) else 0.6
                distinct += 1
            if distinct > top[0]:
                top = (distinct, h.line)
        counts[path] = c
        best_line[path] = top
        for p in c:
            df[p] += 1
    n = max(n_files, len(by_file), 1)
    out: list[SignalHit] = []
    for path, c in counts.items():
        if not c:
            continue
        score = sum(weight[p] * idf(df[p], n) * (1.0 + math.log(c[p])) if c[p] >= 1 else weight[p] * idf(df[p], n) * c[p] for p in c)
        coverage = len(c) / len(weight)
        score *= 1.0 + coverage
        line = best_line[path][1]
        out.append(
            SignalHit(
                path=path,
                score=score,
                start_line=max(1, line - context),
                end_line=line + context,
                detail=f"{len(c)}/{len(weight)} terms",
            )
        )
    return out


def _symbol_score(sym: SymbolRecord, q: QueryTerms) -> float:
    name = sym.name
    low = name.lower()
    qual = f"{sym.parent}.{name}".lower() if sym.parent else low
    score = 0.0
    for ident in q.identifiers:
        il = ident.lower().replace("::", ".").replace("->", ".").replace("\\", ".")
        last = il.rsplit(".", 1)[-1]
        if ident == name or il == qual or qual.endswith("." + il):
            score += 3.0
        elif il == low:
            score += 2.5
        elif last == low:
            score += 2.0 if "." in il else 1.5
        elif normalize_identifier(ident) == normalize_identifier(name):
            score += 2.0
        elif len(il) >= 4 and il in low:
            score += 0.5
    subs = {stem(s) for s in split_identifier(name)}
    for t in q.terms:
        if t in subs:
            score += 1.0
        elif len(t) >= 4 and t in low:
            score += 0.4
    for r in q.routes:
        if sym.kind == "route" and r.lower() in low:
            score += 2.0
    return score * _KIND_WEIGHT.get(sym.kind, 0.7)


def symbol_signal(symbols: Iterable[SymbolRecord], q: QueryTerms) -> list[SignalHit]:
    per_file: dict[str, list[tuple[float, SymbolRecord]]] = defaultdict(list)
    for s in symbols:
        if s.kind in ("import", "module"):
            continue
        sc = _symbol_score(s, q)
        if sc > 0:
            per_file[s.path].append((sc, s))
    out: list[SignalHit] = []
    for path, scored in per_file.items():
        scored.sort(key=lambda t: (-t[0], t[1].start_line))
        top_score, top = scored[0]
        extra = min(5, len(scored) - 1) * 0.1
        out.append(
            SignalHit(
                path=path,
                score=top_score + extra,
                start_line=top.start_line,
                end_line=top.end_line,
                detail=f"{top.kind} {top.parent + '.' if top.parent and top.kind == 'method' else ''}{top.name}"[:120],
            )
        )
    return out


def _route_norm(path: str) -> str:
    return re.sub(r"\{[^}]*\}|<[^>]*>|:\w+|\[[^\]]*\]", "*", path.lower()).rstrip("/") or "/"


def query_definitions(symbols: Iterable[SymbolRecord], q: QueryTerms, *, limit: int = 8) -> list[SymbolRecord]:
    """The definitions that match the query best (input of the reference part of the structural signal)."""
    scored = [(_symbol_score(s, q), s) for s in symbols if s.kind in _KIND_WEIGHT and s.kind not in ("route", "table", "view")]
    scored = [t for t in scored if t[0] > 0]
    scored.sort(key=lambda t: (-t[0], t[1].path, t[1].start_line))
    return [s for _, s in scored[:limit]]


def structural_signal(
    files: Sequence[str],
    q: QueryTerms,
    *,
    routes: Sequence[Route] = (),
    tables: Sequence[SymbolRecord] = (),
    referenced: Sequence[tuple[SymbolRecord, int]] = (),
) -> list[SignalHit]:
    """Structural evidence: path tokens, declared routes and tables matching the query, and query-matching
    definitions that other files use (``referenced``: definition, number of other files calling/instantiating it)."""
    terms = set(q.terms)
    idents = {normalize_identifier(i) for i in q.identifiers}
    scores: dict[str, float] = defaultdict(float)
    lines: dict[str, tuple[int, int, str]] = {}
    if terms or idents:
        for p in files:
            parts = p.split("/")
            base = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]
            base_tokens = {stem(t) for t in split_identifier(base)}
            dir_tokens = {stem(t) for d in parts[:-1] for t in split_identifier(d)}
            s = 2.0 * len(terms & base_tokens) + 1.0 * len((terms & dir_tokens) - base_tokens)
            if normalize_identifier(base) in idents:
                s += 3.0
            if s > 0:
                scores[p] += s
                lines.setdefault(p, (1, 1, "path"))
    qroutes = [_route_norm(r) for r in q.routes]
    for r in routes:
        rn = _route_norm(r.path)
        segs = {stem(t) for t in split_identifier(r.path)}
        s = 0.0
        for qr in qroutes:
            if qr == rn:
                s += 4.0
            elif qr in rn or rn in qr:
                s += 2.0
        s += 0.75 * len(terms & segs)
        if s > 0:
            scores[r.file] += s
            if r.file not in lines or lines[r.file][2] == "path":
                lines[r.file] = (r.line, r.line, f"route {r.method} {r.path}")
    for t in tables:
        name_tokens = {stem(x) for x in split_identifier(t.name)}
        s = 2.0 * len(terms & name_tokens) + (3.0 if normalize_identifier(t.name) in idents else 0.0)
        if s > 0:
            scores[t.path] += s
            if t.path not in lines or lines[t.path][2] == "path":
                lines[t.path] = (t.start_line, t.end_line, f"{t.kind} {t.name}")
    for d, n_files in referenced:
        if n_files <= 0:
            continue
        scores[d.path] += 1.0 * min(n_files, 5)
        if d.path not in lines or lines[d.path][2] == "path":
            lines[d.path] = (d.start_line, d.end_line, f"{d.kind} {d.name} used by {n_files} file(s)")
    return [SignalHit(path=p, score=s, start_line=lines[p][0], end_line=lines[p][1], detail=lines[p][2][:120]) for p, s in scores.items()]


def semantic_signal(chunks: Sequence[ChunkHit], *, min_similarity: float = 0.0, relative_floor: float = 0.0) -> list[SignalHit]:
    """Best chunk per file. Nearest-neighbour search always returns ``k`` rows, so neighbours below
    ``min_similarity`` or below ``relative_floor`` x the best similarity are not evidence and are dropped."""
    best: dict[str, ChunkHit] = {}
    for c in chunks:
        if c.path not in best or c.score > best[c.path].score:
            best[c.path] = c
    if not best:
        return []
    top = max(c.score for c in best.values())
    # cosine similarity may be <= 0 for unrelated text; shift so every kept neighbour has a positive score
    return [
        SignalHit(path=p, score=1.0 + c.score, start_line=c.start_line, end_line=c.end_line, detail=f"sim {c.score:.3f}")
        for p, c in best.items()
        if c.score > min_similarity and c.score >= top * relative_floor
    ]


_TEST_AFFIXES = re.compile(r"^(?:test[_-]?)|(?:[_.-]?(?:test|tests|spec|specs))$")


def name_subject(path: str) -> str:
    """Normalised subject of a file name: ``tests/test_billing.py``/``billing.test.ts``/``BillingTest.php`` -> ``billing``."""
    name = path.rsplit("/", 1)[-1]
    parts = name.split(".")
    stem = ".".join(parts[:-1]) if len(parts) > 1 and parts[0] else name  # drop the extension only
    low = stem.lower()
    if low in ("conftest", "__init__", "setup", "index"):
        return ""
    for _ in range(2):
        low = _TEST_AFFIXES.sub("", low)
    return re.sub(r"[^a-z0-9]", "", low)


def referencing_tests_signal(
    candidates: Sequence[str],
    graph: DependencyGraph,
    test_files: Iterable[str],
    *,
    relevant_tests: Iterable[str] = (),
    symbol_mentions: Mapping[str, Iterable[str]] | None = None,
    relevance: Mapping[str, float] | None = None,
) -> list[SignalHit]:
    """Tests that exercise a candidate: they import it (1.0), mention its matched symbols (0.5) or are named after it
    (``test_<name>``/``<name>.test``/``<Name>Test``: 0.75). Tests that are themselves relevant to the query count double.

    ``symbol_mentions``: candidate path -> test files mentioning one of its matched symbols.
    ``relevance``: candidate path -> preliminary relevance in ``(0, 1]``; tests are supporting evidence, so a weakly
    relevant candidate only gets that share of the signal's fusion contribution (``SignalHit.boost``).
    """
    tests = set(test_files)
    relevant = set(relevant_tests)
    by_subject: dict[str, set[str]] = defaultdict(set)
    for t in tests:
        subj = name_subject(t)
        if len(subj) >= 3:
            by_subject[subj].add(t)
    out: list[SignalHit] = []
    for c in candidates:
        if c in tests:
            continue
        importing = {t for t in graph.importers.get(c, ()) if t in tests}
        mentioning = (set(symbol_mentions.get(c, ())) & tests) - importing if symbol_mentions else set()
        named = by_subject.get(name_subject(c), set()) - importing - mentioning
        score = sum(1.0 + (1.0 if t in relevant else 0.0) for t in importing)
        score += sum(0.5 * (1.0 + (1.0 if t in relevant else 0.0)) for t in mentioning)
        score += sum(0.75 * (1.0 + (1.0 if t in relevant else 0.0)) for t in named)
        if score > 0:
            n = len(importing | mentioning | named)
            rel = 1.0 if relevance is None else max(0.0, min(1.0, float(relevance.get(c, 0.0))))
            if rel <= 0:
                continue
            out.append(SignalHit(path=c, score=score * rel, start_line=1, end_line=1, detail=f"{n} test(s)", boost=rel))
    return out


def dependency_signal(seeds: Sequence[tuple[str, float]], graph: DependencyGraph) -> list[SignalHit]:
    """Neighbours of the seeds in the import graph, weighted by the seeds' (normalised) preliminary scores."""
    scores: dict[str, float] = defaultdict(float)
    via: dict[str, str] = {}
    seed_paths = {p for p, _ in seeds}
    for path, weight in seeds:
        for n in graph.neighbors(path):
            if n == path:
                continue
            scores[n] += weight
            via.setdefault(n, path)
    return [
        SignalHit(path=p, score=s, start_line=1, end_line=1, detail=f"{'seed+' if p in seed_paths else ''}near {via[p]}"[:120])
        for p, s in scores.items()
    ]
