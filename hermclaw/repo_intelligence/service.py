"""Repository intelligence facade (Bauplan §14 Phase F – context selection).

:class:`RepoIntelligence` implements :class:`hermclaw.core.interfaces.RepoContextProvider` (workspace-first API used
by planner, scope engine, context builder, tool engine, reviewer) and offers :meth:`RepoIntelligence.bind` /
:meth:`RepoIntelligence.open` for a bound, root-first API (:class:`BoundRepo`: ``inventory()``, ``search()``,
``symbols()``, ``read()``, ``context_for()``, ``grep()``, ``find_files()``, ``index()``).

Freshness: every query first brings the index to the workspace HEAD (incremental by Git SHA, see
:mod:`indexer`). Uncommitted edits of the coder are covered by an *overlay*: dirty files are parsed live (symbols,
routes, tables, imports) and replace their indexed rows; deleted files are hidden; lexical search always runs on
the live working tree. A failing signal (database, embedding host, timeout) degrades the result, never the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.errors import NotFoundError, ValidationFailed
from hermclaw.core.interfaces import RepoHit, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext, EmbeddingModel
from hermclaw.repo_intelligence import _proc
from hermclaw.repo_intelligence.config import RepoIntelConfig
from hermclaw.repo_intelligence.dependencies import DependencyGraph, ModuleResolver, psr4_from_composer
from hermclaw.repo_intelligence.embeddings import semantic_search
from hermclaw.repo_intelligence.fileio import lstat_regular, read_bytes, read_text
from hermclaw.repo_intelligence.indexer import SOURCE_TYPE, IndexTarget, RepoIndexer
from hermclaw.repo_intelligence.inventory import build_inventory, status_entries
from hermclaw.repo_intelligence.languages import detect_language, looks_binary
from hermclaw.repo_intelligence.lexical import LexicalSearcher
from hermclaw.repo_intelligence.paths import is_test_path, matches_any
from hermclaw.repo_intelligence.query import QueryTerms, analyze
from hermclaw.repo_intelligence.ranking import (
    dependency_signal,
    fuse,
    lexical_signal,
    referencing_tests_signal,
    semantic_signal,
    structural_signal,
    symbol_signal,
)
from hermclaw.repo_intelligence.reader import FileReader, ReadResult
from hermclaw.repo_intelligence.schemas import (
    ChunkHit,
    FileHit,
    FileMatch,
    FileSymbols,
    ImportRecord,
    IndexStats,
    LexicalResult,
    RepoInventory,
    Route,
    SignalHit,
    SymbolRecord,
)
from hermclaw.repo_intelligence.sources import list_worktree_files
from hermclaw.repo_intelligence.symbols import extract_file_symbols, load_imports, query_symbols, symbols_by_kind

log = get_logger(__name__)
T = TypeVar("T")
_SEARCH_SNIPPET_CHARS = 800
_QUALIFIER_RE = re.compile(r"\.|::|->|\\|#")
_DEF_RANK = {
    "class": 0,
    "interface": 0,
    "trait": 0,
    "enum": 0,
    "function": 0,
    "method": 0,
    "type": 1,
    "constant": 2,
    "table": 2,
    "view": 2,
    "route": 3,
}


@dataclass
class _Overlay:
    dirty: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    files: dict[str, FileSymbols] = field(default_factory=dict)

    @property
    def hidden(self) -> set[str]:
        """Indexed rows of these paths are stale (dirty) or gone (deleted)."""
        return self.dirty | self.deleted

    def symbols(self) -> list[SymbolRecord]:
        return [s for fs in self.files.values() for s in fs.symbols]


@dataclass
class _State:
    revision: str
    files: list[str]
    overlay: _Overlay
    status_digest: str


@dataclass
class SearchOutcome:
    hits: list[FileHit]
    per_signal: dict[str, dict[str, SignalHit]]
    chunks: list[ChunkHit]
    degraded: list[str]
    query: QueryTerms


class RepoIntelligence:
    """Implements :class:`~hermclaw.core.interfaces.RepoContextProvider`."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        embedder: EmbeddingModel | None = None,
        config: RepoIntelConfig | None = None,
        emit_events: bool = True,
    ) -> None:
        if sessionmaker is None:
            from hermclaw.persistence.db import get_sessionmaker

            sessionmaker = get_sessionmaker()
        self.sessionmaker = sessionmaker
        self.cfg = config or RepoIntelConfig()
        self.embedder = embedder
        self.emit_events = emit_events
        self.indexer = RepoIndexer(sessionmaker, embedder=embedder, config=self.cfg)
        self.lexical = LexicalSearcher(self.cfg)
        self.reader = FileReader(self.cfg)
        self._fresh: dict[str, str] = {}
        self._fresh_locks: dict[str, asyncio.Lock] = {}
        self._embed_attempt: dict[str, float] = {}
        self._bg: set[asyncio.Task[Any]] = set()
        self._parse_cache: dict[tuple[str, str], tuple[int, int, FileSymbols]] = {}
        self._graph_cache: dict[tuple[str, str], dict[str, list[ImportRecord]]] = {}
        self._inventory_cache: dict[tuple[str, str, str], RepoInventory] = {}
        self._state_cache: dict[str, tuple[str, str, _State]] = {}
        self._index_tasks: dict[tuple[str, str], asyncio.Task[IndexStats | None]] = {}

    # ============================================================================== RepoContextProvider protocol
    @staticmethod
    def target_of(workspace: WorkspaceHandle) -> IndexTarget:
        return IndexTarget(
            root=Path(workspace.path), repository_key=workspace.repository_key, workspace_id=workspace.id, job_id=workspace.job_id
        )

    async def inventory_summary(self, workspace: WorkspaceHandle) -> dict[str, Any]:
        target = self.target_of(workspace)
        inv = await self.inventory(target)
        summary = inv.summary()
        summary["index"] = await self._index_summary(target)
        return summary

    async def search(self, workspace: WorkspaceHandle, query: str, *, k: int = 20) -> list[RepoHit]:
        target = self.target_of(workspace)
        out = await self.search_files(target, query, k=k)
        root = Path(target.root)
        hits: list[RepoHit] = []
        for h in out.hits:
            hit = _to_repo_hit(h)
            with contextlib.suppress(Exception):
                end = min(h.end_line, h.start_line + 7)
                res = await self.reader.read(root, h.path, h.start_line, end, max_chars=_SEARCH_SNIPPET_CHARS)
                hit.snippet = DEFAULT_REDACTOR.text(res.text)
                hit.end_line = max(h.end_line, res.end_line) if res.text else h.end_line
            hits.append(hit)
        return hits

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]:
        return await self.symbols(self.target_of(workspace), name, k=k)

    async def read(self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000) -> str:
        return (await self.read_range(self.target_of(workspace), path, start, end, max_chars=max_chars)).text

    async def context_for(self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int = 24_000) -> list[RepoHit]:
        return await self.context(self.target_of(workspace), goal, budget_chars=budget_chars)

    # ============================================================================== bound API
    def bind(self, workspace: WorkspaceHandle) -> BoundRepo:
        return BoundRepo(self, self.target_of(workspace))

    def open(self, root: Path | str, repository_key: str, *, job_id: uuid.UUID | None = None) -> BoundRepo:
        return BoundRepo(self, IndexTarget(root=Path(root), repository_key=repository_key, job_id=job_id))

    async def aclose(self) -> None:
        for t in [*self._bg, *self._index_tasks.values()]:
            t.cancel()
        for t in [*self._bg, *self._index_tasks.values()]:
            with contextlib.suppress(BaseException):
                await t
        self._bg.clear()
        self._index_tasks.clear()

    async def wait_background(self) -> None:
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ============================================================================== freshness / state
    def _check_target(self, target: IndexTarget) -> Path:
        key = target.repository_key
        if not isinstance(key, str) or not key.strip() or len(key) > 300:
            raise ValidationFailed("repository_key must be a non-empty string of at most 300 characters", code="REPO_KEY_INVALID")
        root = Path(target.root)
        if not root.is_dir():
            raise NotFoundError(f"workspace directory not found: {root.name}", code="REPO_WORKSPACE_NOT_FOUND")
        return root

    async def ensure_index(self, target: IndexTarget, *, force: bool = False) -> IndexStats | None:
        """Index the current revision if needed (``auto_index``); errors are logged, never raised to queries."""
        root = self._check_target(target)
        key = target.repository_key
        lock = self._fresh_locks.setdefault(key, asyncio.Lock())
        async with lock:
            revision, _src = await self.indexer.current_revision(root)
            if not force and self._fresh.get(key) == revision:
                self._maybe_retry_embedding(target)
                return None
            stats = await self.indexer.index(target, embed=False)
            self._fresh[key] = revision
        if self.embedder is not None and stats.embedding_pending:
            await self._embed_with_budget(target, stats)
        return stats

    async def _embed_with_budget(self, target: IndexTarget, stats: IndexStats) -> None:
        self._embed_attempt[target.repository_key] = time.monotonic()
        run_id = uuid.UUID(stats.run_id) if stats.run_id else None
        task = asyncio.create_task(self.indexer.embed(target, run_id=run_id))
        done, _ = await asyncio.wait({task}, timeout=max(0.0, self.cfg.inline_embed_seconds))
        if task in done:
            with contextlib.suppress(Exception):
                outcome = task.result()
                stats.embedded, stats.embedding_pending, stats.embedding_error = outcome.embedded, outcome.pending, outcome.error
            return
        self._bg.add(task)
        task.add_done_callback(self._bg_done)

    def _bg_done(self, task: asyncio.Task[Any]) -> None:
        self._bg.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("background embedding failed: %s", type(task.exception()).__name__)

    def _maybe_retry_embedding(self, target: IndexTarget) -> None:
        if self.embedder is None:
            return
        key = target.repository_key
        last = self._embed_attempt.get(key)
        if last is not None and time.monotonic() - last < self.cfg.embed_retry_seconds:
            return
        if any(not t.done() for t in self._bg):
            return
        self._embed_attempt[key] = time.monotonic()
        task = asyncio.create_task(self.indexer.embed(target))
        self._bg.add(task)
        task.add_done_callback(self._bg_done)

    async def refresh(self, target: IndexTarget, degraded: list[str] | None = None) -> None:
        """Wait (bounded) for the index to reach the workspace revision; indexing itself is never cancelled by a
        query timeout – it keeps running and later queries pick the result up."""
        if not self.cfg.auto_index:
            return
        tkey = (target.repository_key, str(Path(target.root)))
        task = self._index_tasks.get(tkey)
        if task is None or task.done():
            task = asyncio.create_task(self.ensure_index(target))
            task.add_done_callback(_retrieve)
            self._index_tasks[tkey] = task
        try:
            await asyncio.wait_for(asyncio.shield(task), self.cfg.index_wait_seconds)
        except TimeoutError:
            if degraded is not None:
                degraded.append("index: still running")
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            log.warning("repository index refresh failed for %s: %s", target.repository_key, code)
            if degraded is not None:
                degraded.append(f"index: {code}")

    async def _guard(self, label: str, coro: Awaitable[T], default: T, degraded: list[str]) -> T:
        try:
            return await asyncio.wait_for(coro, self.cfg.query_timeout_seconds)
        except TimeoutError:
            degraded.append(f"{label}: timeout")
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            degraded.append(f"{label}: {code}")
            log.warning("repository intelligence signal %s failed: %s", label, code)
        return default

    async def _state(self, target: IndexTarget, root: Path) -> _State:
        cfg = self.cfg
        is_git = await _proc.is_git_repo(root, timeout_s=cfg.git_timeout_seconds, git_binary=cfg.git_binary)
        revision = (await _proc.head_sha(root, timeout_s=cfg.git_timeout_seconds, git_binary=cfg.git_binary)) if is_git else None
        entries = await status_entries(root, cfg) if is_git else []
        digest = hashlib.sha256(repr(entries).encode()).hexdigest()
        cached = self._state_cache.get(str(root))
        listing_key = f"{revision}:{digest}"
        if cached is not None and cached[0] == listing_key and is_git:
            files = cached[2].files
        else:
            files = (await list_worktree_files(root, cfg, git_repo=is_git)).paths
        overlay = _Overlay()
        if is_git:
            for xy, path, orig in entries:
                if orig:
                    overlay.deleted.add(orig)
                if lstat_regular(root, path) is None:
                    if "D" in xy or xy == "??":
                        overlay.deleted.add(path)
                    continue
                overlay.dirty.add(path)
            overlay.deleted -= overlay.dirty
            resolver = ModuleResolver(files, psr4=self._psr4(root, files))
            for path in sorted(overlay.dirty)[: cfg.overlay_max_files]:
                fs = await asyncio.to_thread(self._parse_live, root, path)
                if fs is not None:
                    for imp in fs.imports:
                        imp.resolved = resolver.resolve(path, fs.language, imp)
                    overlay.files[path] = fs
        state = _State(revision=revision or "worktree", files=files, overlay=overlay, status_digest=digest)
        self._state_cache[str(root)] = (listing_key, target.repository_key, state)
        return state

    def _psr4(self, root: Path, files: Sequence[str]) -> dict[str, list[str]]:
        if "composer.json" not in files:
            return {}
        import json

        try:
            return psr4_from_composer(json.loads(read_text(root, "composer.json", 2_000_000) or "{}"))
        except ValueError:
            return {}

    def _parse_live(self, root: Path, path: str) -> FileSymbols | None:
        if matches_any(path, self.cfg.sensitive_globs) or matches_any(path, self.cfg.index_exclude_globs):
            return None
        st = lstat_regular(root, path)
        if st is None or st.st_size > self.cfg.max_index_file_bytes:
            return None
        key = (str(root), path)
        cached = self._parse_cache.get(key)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
        data = read_bytes(root, path, st.st_size + 1)
        if data is None or looks_binary(data[:8192]):
            return None
        lang = detect_language(path, data[:512]) or "text"
        fs = extract_file_symbols(path, lang, data.decode("utf-8", errors="replace"))
        if len(self._parse_cache) > 5_000:
            self._parse_cache.clear()
        self._parse_cache[key] = (st.st_mtime_ns, st.st_size, fs)
        return fs

    async def _imports(self, key: str, revision: str, overlay: _Overlay) -> dict[str, list[ImportRecord]]:
        ck = (key, revision)
        base = self._graph_cache.get(ck)
        if base is None:
            async with self.sessionmaker() as s:
                base = await load_imports(s, key)
            if len(self._graph_cache) > 32:
                self._graph_cache.clear()
            self._graph_cache[ck] = base
        merged = {p: v for p, v in base.items() if p not in overlay.hidden}
        for p, fs in overlay.files.items():
            merged[p] = list(fs.imports)
        return merged

    async def _index_summary(self, target: IndexTarget) -> dict[str, Any]:
        try:
            run = await self.indexer.latest_run(target.repository_key)
        except Exception as exc:
            return {"available": False, "error": getattr(exc, "code", type(exc).__name__)}
        if run is None:
            return {"available": False}
        st = run.stats or {}
        return {
            "available": True,
            "git_sha": run.git_sha,
            "mode": st.get("mode"),
            "files_indexed": st.get("files_indexed"),
            "symbols": st.get("symbols"),
            "chunks": st.get("chunks"),
            "embedding_model": st.get("embedding_model"),
            "embedding_pending": st.get("embedding_pending"),
        }

    # ============================================================================== inventory
    async def inventory(self, target: IndexTarget) -> RepoInventory:
        root = self._check_target(target)
        degraded: list[str] = []
        await self.refresh(target, degraded)
        state = await self._state(target, root)
        ck = (str(root), state.revision, state.status_digest)
        cached = self._inventory_cache.get(ck)
        if cached is not None:
            return cached
        inv: RepoInventory | None = None
        if not state.overlay.hidden and state.revision != "worktree":
            with contextlib.suppress(Exception):
                run = await self.indexer.latest_run(target.repository_key)
                if run is not None and run.git_sha == state.revision and run.inventory:
                    inv = RepoInventory.model_validate(run.inventory)
        if inv is None:
            started = time.monotonic()
            await self._event(
                target,
                EventType.REPO_INVENTORY_STARTED,
                {"repository_key": target.repository_key, "mode": "live", "git_sha": state.revision},
            )
            inv = await build_inventory(root, self.cfg)
            await self._event(
                target,
                EventType.REPO_INVENTORY_FINISHED,
                {
                    "repository_key": target.repository_key,
                    "mode": "live",
                    "git_sha": state.revision,
                    "file_count": inv.file_count,
                    "primary_languages": inv.primary_languages,
                    "routes": len(inv.routes),
                },
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        inv.warnings = sorted({*inv.warnings, *degraded})
        if len(self._inventory_cache) > 64:
            self._inventory_cache.clear()
        self._inventory_cache[ck] = inv
        return inv

    # ============================================================================== lexical
    async def grep(
        self,
        target: IndexTarget,
        pattern: str | Sequence[str],
        *,
        regex: bool = False,
        globs: Sequence[str] = (),
        exclude_globs: Sequence[str] = (),
        case_sensitive: bool | None = None,
        word: bool = False,
        max_results: int = 200,
    ) -> LexicalResult:
        root = self._check_target(target)
        started = time.monotonic()
        res = await self.lexical.search(
            root,
            pattern,
            mode="regex" if regex else "fixed",
            globs=globs,
            exclude_globs=exclude_globs,
            case_sensitive=case_sensitive,
            word=word,
            max_results=max_results,
        )
        await self._event(
            target,
            EventType.REPO_SEARCH_EXECUTED,
            {
                "repository_key": target.repository_key,
                "kind": "lexical",
                "query": _clip_query(pattern if isinstance(pattern, str) else " | ".join(pattern)),
                "regex": regex,
                "hits": len(res.hits),
                "truncated": res.truncated,
                "engine": res.engine,
            },
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return res

    async def find_files(self, target: IndexTarget, query: str, *, limit: int = 50) -> list[FileMatch]:
        root = self._check_target(target)
        return await self.lexical.find_files(root, query, limit=limit)

    # ============================================================================== semantic
    async def semantic(self, target: IndexTarget, query: str, *, k: int = 20) -> list[ChunkHit]:
        if self.embedder is None:
            return []
        self._check_target(target)
        async with self.sessionmaker() as s:
            return await semantic_search(
                s,
                self.embedder,
                target.repository_key,
                query,
                k=k,
                cfg=self.cfg,
                ctx=CallContext(purpose="embedding", job_id=target.job_id),
            )

    # ============================================================================== fusion search
    async def search_files(self, target: IndexTarget, query: str, *, k: int = 20) -> SearchOutcome:
        root = self._check_target(target)
        started = time.monotonic()
        degraded: list[str] = []
        q = analyze(query, max_terms=self.cfg.lexical_max_terms)
        if q.is_empty():
            return SearchOutcome([], {}, [], [], q)
        await self.refresh(target, degraded)
        state = await self._state(target, root)
        overlay = state.overlay
        key = target.repository_key
        hidden = overlay.hidden
        cfg = self.cfg

        async def _lex() -> LexicalResult:
            return await self.lexical.search(
                root,
                q.lexical_patterns,
                mode="fixed",
                exclude_globs=cfg.index_exclude_globs,
                case_sensitive=False,
                max_results=cfg.lexical_max_results,
            )

        name_terms = sorted({_QUALIFIER_RE.split(i)[-1] for i in q.identifiers} | set(q.terms))

        async def _syms() -> list[SymbolRecord]:
            async with self.sessionmaker() as s:
                return await query_symbols(s, key, name_terms, exclude_paths=hidden, limit=3_000)

        async def _structure() -> list[SymbolRecord]:
            async with self.sessionmaker() as s:
                return await symbols_by_kind(s, key, ("route", "table", "view"), exclude_paths=hidden)

        async def _sem() -> list[ChunkHit]:
            if self.embedder is None:
                return []
            async with self.sessionmaker() as s:
                hits = await semantic_search(
                    s, self.embedder, key, query, k=cfg.signal_depth, cfg=cfg, ctx=CallContext(purpose="embedding", job_id=target.job_id),
                    exclude_paths=overlay.deleted,
                )  # fmt: skip
            return hits

        lex_res, db_syms, struct_syms, chunks = await asyncio.gather(
            self._guard("lexical", _lex(), LexicalResult(), degraded),
            self._guard("symbol", _syms(), [], degraded),
            self._guard("structural", _structure(), [], degraded),
            self._guard("semantic", _sem(), [], degraded),
        )
        live_syms = overlay.symbols()
        all_syms = [*db_syms, *live_syms]
        routes = [_route_of(s) for s in [*struct_syms, *live_syms] if s.kind == "route"]
        tables = [s for s in [*struct_syms, *live_syms] if s.kind in ("table", "view")]
        signals: dict[str, list[SignalHit]] = {
            "lexical": lexical_signal(lex_res.hits, q, len(state.files), context=cfg.snippet_context_lines),
            "symbol": symbol_signal(all_syms, q),
            "structural": structural_signal(state.files, q, routes=[r for r in routes if r is not None], tables=tables),
            "semantic": semantic_signal(chunks),
        }
        prelim = fuse(signals, cfg)
        imports = await self._guard("dependency", self._imports(key, self._fresh.get(key, state.revision), overlay), {}, degraded)
        graph = DependencyGraph.from_imports(imports, drop=overlay.deleted)
        if prelim:
            top = prelim[0].rrf or 1.0
            seeds = [(h.path, h.rrf / top) for h in prelim[: cfg.dependency_seeds]]
            signals["dependency"] = dependency_signal(seeds, graph)
            tests = [p for p in state.files if is_test_path(p)]
            candidates = [h.path for h in prelim[: cfg.test_reference_candidates]]
            relevant = {h.path for h in lex_res.hits if is_test_path(h.path)}
            mentions = await self._guard("test_reference", self._symbol_mentions(root, candidates, all_syms, q, tests), {}, degraded)
            signals["test_reference"] = referencing_tests_signal(
                candidates, graph, tests, relevant_tests=relevant, symbol_mentions=mentions
            )
        fused = fuse(signals, cfg)
        existing = set(state.files)
        fused = [h for h in fused if h.path in existing and not matches_any(h.path, cfg.sensitive_globs)][: max(0, k)]
        per_signal = {name: {h.path: h for h in sorted(hs, key=lambda x: x.score)} for name, hs in signals.items()}  # best wins
        await self._event(
            target,
            EventType.REPO_SEARCH_EXECUTED,
            {
                "repository_key": key,
                "kind": "fusion",
                "query": _clip_query(query),
                "k": k,
                "signals": {name: len(hs) for name, hs in signals.items()},
                "top": [{"path": h.path, "score": round(h.score, 4)} for h in fused[:10]],
                "lexical_engine": lex_res.engine,
                "lexical_truncated": lex_res.truncated,
                "overlay_files": len(overlay.files),
                "degraded": degraded,
            },
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return SearchOutcome(hits=fused, per_signal=per_signal, chunks=chunks, degraded=degraded, query=q)

    async def _symbol_mentions(
        self, root: Path, candidates: Sequence[str], syms: Sequence[SymbolRecord], q: QueryTerms, tests: Sequence[str]
    ) -> dict[str, set[str]]:
        if not tests or not candidates:
            return {}
        cand = set(candidates)
        by_file: dict[str, list[tuple[float, str]]] = defaultdict(list)
        sig = {h.path: h for h in symbol_signal([s for s in syms if s.path in cand], q)}
        for s in syms:
            if s.path in cand and s.kind in ("function", "method", "class", "interface", "trait", "enum") and len(s.name) >= 3:
                rank = 0.0 if sig.get(s.path) and s.name in sig[s.path].detail else 1.0
                by_file[s.path].append((rank, s.name))
        names_by_file = {p: [n for _, n in sorted(v)[:3]] for p, v in by_file.items()}
        names = sorted({n for v in names_by_file.values() for n in v})[:30]
        if not names:
            return {}
        res = await self.lexical.search(
            root, names, mode="fixed", paths=list(tests)[:2_000], case_sensitive=True, word=True, max_results=2_000
        )
        tests_by_name: dict[str, set[str]] = defaultdict(set)
        for h in res.hits:
            for m in h.matches:
                tests_by_name[m].add(h.path)
        return {p: {t for n in ns for t in tests_by_name.get(n, set())} for p, ns in names_by_file.items()}

    # ============================================================================== symbols
    async def symbols(self, target: IndexTarget, name: str, *, k: int = 20) -> list[RepoHit]:
        root = self._check_target(target)
        started = time.monotonic()
        degraded: list[str] = []
        raw = (name or "").strip().removesuffix("()")
        parts = [p for p in _QUALIFIER_RE.split(raw) if p]
        if not parts:
            return []
        last = parts[-1]
        qualifier = [p.lower() for p in parts[:-1]]
        await self.refresh(target, degraded)
        state = await self._state(target, root)
        key = target.repository_key

        async def _exact() -> list[SymbolRecord]:
            async with self.sessionmaker() as s:
                return await query_symbols(s, key, [last], exact=True, exclude_paths=state.overlay.hidden, limit=500)

        async def _fuzzy() -> list[SymbolRecord]:
            async with self.sessionmaker() as s:
                return await query_symbols(s, key, [last], exclude_paths=state.overlay.hidden, limit=500)

        db = await self._guard("symbol", _exact(), [], degraded)
        live = [s for s in state.overlay.symbols() if s.kind != "import" and s.name.lower() == last.lower()]
        if any(d.startswith("symbol") for d in degraded) and not live:
            live = await self._live_symbol_scan(root, last)
        cands = [*db, *live]
        exact_mode = bool(cands)
        if not cands:
            cands = await self._guard("symbol", _fuzzy(), [], degraded)
            cands += [s for s in state.overlay.symbols() if s.kind != "import" and last.lower() in s.name.lower()]
        existing = set(state.files)
        scored: list[tuple[float, SymbolRecord]] = []
        for s in cands:
            if s.path not in existing:
                continue
            scored.append((_symbol_match_score(s, last, qualifier, exact_mode), s))
        scored.sort(key=lambda t: (-t[0], _DEF_RANK.get(t[1].kind, 4), t[1].path, t[1].start_line))
        hits = [
            RepoHit(
                path=s.path,
                start_line=s.start_line,
                end_line=max(s.start_line, s.end_line),
                score=round(score, 4),
                snippet=DEFAULT_REDACTOR.text(s.signature or f"{s.kind} {s.name}")[:300],
                signals={"symbol": round(score, 4)},
            )
            for score, s in scored[: max(0, k)]
            if score > 0
        ]
        await self._event(
            target,
            EventType.REPO_SEARCH_EXECUTED,
            {
                "repository_key": key,
                "kind": "symbol",
                "query": _clip_query(raw),
                "k": k,
                "hits": len(hits),
                "exact": exact_mode,
                "top": [{"path": h.path, "line": h.start_line, "score": h.score} for h in hits[:10]],
                "degraded": degraded,
            },
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return hits

    async def _live_symbol_scan(self, root: Path, name: str) -> list[SymbolRecord]:
        """Index unavailable: parse the files that mention ``name`` (bounded) instead."""
        res = await self.lexical.search(
            root, name, mode="fixed", word=True, case_sensitive=False, max_results=500, exclude_globs=self.cfg.index_exclude_globs
        )
        out: list[SymbolRecord] = []
        for path in sorted({h.path for h in res.hits})[:50]:
            fs = await asyncio.to_thread(self._parse_live, root, path)
            if fs is not None:
                out.extend(s for s in fs.symbols if s.kind != "import" and s.name.lower() == name.lower())
        return out

    # ============================================================================== reads / context
    async def read_range(
        self, target: IndexTarget, path: str, start: int = 1, end: int | None = None, *, max_chars: int | None = None
    ) -> ReadResult:
        root = self._check_target(target)
        return await self.reader.read(root, path, start, end, max_chars=max_chars)

    async def context(self, target: IndexTarget, goal: str, *, budget_chars: int = 24_000) -> list[RepoHit]:
        """Most relevant snippets for ``goal`` within ``budget_chars`` (fresh reads, redacted)."""
        root = self._check_target(target)
        cfg = self.cfg
        outcome = await self.search_files(target, goal, k=cfg.context_search_k)
        remaining = max(0, int(budget_chars))
        out: list[RepoHit] = []
        chunks_by_file: dict[str, list[ChunkHit]] = defaultdict(list)
        for c in outcome.chunks:
            chunks_by_file[c.path].append(c)
        for hit in outcome.hits:
            if remaining < 200:
                break
            ranges: list[tuple[int, int]] = [(hit.start_line, hit.end_line)]
            for name in ("symbol", "semantic", "lexical"):
                sh = outcome.per_signal.get(name, {}).get(hit.path)
                if sh is not None:
                    ranges.append((sh.start_line, sh.end_line))
            ranges.extend((c.start_line, c.end_line) for c in chunks_by_file.get(hit.path, []))
            picked: list[tuple[int, int]] = []
            for r_start, r_end in ranges:
                s = max(1, r_start - cfg.snippet_context_lines)
                e = min(max(s, r_end + cfg.snippet_context_lines), s + cfg.snippet_max_lines - 1)
                if any(not (e < ps or s > pe) for ps, pe in picked):
                    continue
                picked.append((s, e))
                if len(picked) >= cfg.context_max_snippets_per_file:
                    break
            for s, e in sorted(picked):
                overhead = len(hit.path) + 40
                if remaining - overhead < 100:
                    break
                try:
                    res = await self.reader.read(root, hit.path, s, e, max_chars=remaining - overhead)
                except Exception as exc:  # unreadable/binary/protected: skip, never fail the context
                    log.debug("context read skipped %s: %s", hit.path, getattr(exc, "code", type(exc).__name__))
                    break
                if not res.text.strip():
                    continue
                remaining -= len(res.text) + overhead
                out.append(
                    RepoHit(
                        path=hit.path,
                        start_line=res.start_line,
                        end_line=max(res.start_line, res.end_line),
                        score=hit.score,
                        snippet=DEFAULT_REDACTOR.text(res.text),
                        signals=dict(hit.signals),
                    )
                )
        return out

    # ============================================================================== events
    async def _event(self, target: IndexTarget, event_type: str, payload: dict[str, Any], *, duration_ms: int | None = None) -> None:
        if not self.emit_events:
            return
        try:
            async with self.sessionmaker() as s, s.begin():
                await append_event(
                    s,
                    event_type,
                    source_type=SOURCE_TYPE,
                    source_id=target.repository_key[:200],
                    job_id=target.job_id,
                    payload=payload,
                    duration_ms=duration_ms,
                )
        except Exception as exc:  # observability must not break retrieval
            log.warning("could not record %s: %s", event_type, type(exc).__name__)


class BoundRepo:
    """Root-bound facade: ``inventory()``, ``search(query, k)``, ``symbols(name)``, ``read(path, start, end)``,
    ``context_for(goal_text, budget)`` plus ``grep``/``find_files``/``semantic``/``index``."""

    def __init__(self, service: RepoIntelligence, target: IndexTarget) -> None:
        self.service = service
        self.target = target

    async def index(self, *, force_full: bool = False) -> IndexStats:
        return await self.service.indexer.index(self.target, force_full=force_full)

    async def inventory(self) -> RepoInventory:
        return await self.service.inventory(self.target)

    async def search(self, query: str, k: int = 20) -> list[FileHit]:
        return (await self.service.search_files(self.target, query, k=k)).hits

    async def symbols(self, name: str, k: int = 20) -> list[RepoHit]:
        return await self.service.symbols(self.target, name, k=k)

    async def read(self, path: str, start: int = 1, end: int | None = None, *, max_chars: int | None = None) -> ReadResult:
        return await self.service.read_range(self.target, path, start, end, max_chars=max_chars)

    async def context_for(self, goal_text: str, budget: int = 24_000) -> list[RepoHit]:
        return await self.service.context(self.target, goal_text, budget_chars=budget)

    async def grep(self, pattern: str | Sequence[str], **kw: Any) -> LexicalResult:
        return await self.service.grep(self.target, pattern, **kw)

    async def find_files(self, query: str, *, limit: int = 50) -> list[FileMatch]:
        return await self.service.find_files(self.target, query, limit=limit)

    async def semantic(self, query: str, k: int = 20) -> list[ChunkHit]:
        return await self.service.semantic(self.target, query, k=k)


# ============================================================================================= helpers
def _retrieve(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _clip_query(q: str) -> str:
    return DEFAULT_REDACTOR.text(q)[:300]


def _to_repo_hit(h: FileHit) -> RepoHit:
    return RepoHit(path=h.path, start_line=h.start_line, end_line=h.end_line, score=h.score, snippet=h.snippet, signals=dict(h.signals))


def _route_of(s: SymbolRecord) -> Route | None:
    method, _, path = s.name.partition(" ")
    if not path:
        return None
    return Route(method=method, path=path, handler=s.parent, file=s.path, line=s.start_line, framework="generic")


def _symbol_match_score(s: SymbolRecord, last: str, qualifier: Sequence[str], exact_mode: bool) -> float:
    name = s.name
    if exact_mode:
        if name.lower() != last.lower():
            return 0.0
        base = 1.0 if name == last else 0.85
        if qualifier:
            parent_parts = [p for p in re.split(r"[.\\:]+", (s.parent or "").lower()) if p]
            path_tokens = [t for t in re.split(r"[/.]", s.path.rsplit(".", 1)[0].lower()) if t]
            # the qualifier names the enclosing class/namespace or the module/file of the definition
            if not ((parent_parts and parent_parts[-1] == qualifier[-1]) or qualifier[-1] in path_tokens):
                base -= 0.3
        if s.kind in ("route", "table", "view"):
            base *= 0.9
        elif s.kind == "constant":
            base *= 0.95
        return base
    low, ll = name.lower(), last.lower()
    if low.startswith(ll):
        return 0.6
    if ll in low:
        return 0.45
    return 0.0
