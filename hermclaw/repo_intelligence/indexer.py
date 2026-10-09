"""Index orchestration: full index and incremental reindex by Git SHA (P11 11.12).

One run per index change (``repo_index_runs``):

1. take the per-repository lock (``asyncio.Lock`` in-process + PostgreSQL advisory lock across processes);
2. decide the mode from the latest *finished* run of the ``repository_key``: ``noop`` (same SHA/version),
   ``incremental`` (``git diff --name-status --no-renames <old>..<new>`` → only added/modified/type-changed files are
   re-parsed and re-chunked, deleted files are removed) or ``full`` (no usable previous run, index version changed,
   old commit unknown to this clone, non-git workspace);
3. read the committed tree of the SHA (never uncommitted content), extract symbols/imports/routes/tables, resolve
   imports against the new file set and chunk symbol-aligned;
4. one transaction swaps the rows of the touched paths, re-resolves imports of untouched files when the file set
   changed, finishes the run row (inventory + stats) and appends ``repo.inventory.finished`` / ``repo.index.updated``;
5. after the commit: embed pending chunks in batches (identical content re-uses stored vectors), failures are
   recorded in the run stats and a warning event – lexical/structural retrieval keeps working.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.errors import HermclawError, ResourceUnavailable
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext, EmbeddingModel
from hermclaw.persistence.models import CodeChunk, CodeSymbol, RepoIndexRun, Workspace
from hermclaw.repo_intelligence import _proc
from hermclaw.repo_intelligence.chunking import Chunk, Chunker
from hermclaw.repo_intelligence.config import INDEX_VERSION, RepoIntelConfig
from hermclaw.repo_intelligence.dependencies import ModuleResolver, composer_manifests, merge_psr4, psr4_from_composer
from hermclaw.repo_intelligence.embeddings import EmbedOutcome, count_pending, embed_pending, replace_file_chunks
from hermclaw.repo_intelligence.fileio import decode_text
from hermclaw.repo_intelligence.inventory import build_inventory
from hermclaw.repo_intelligence.languages import detect_language, is_binary_name, looks_binary
from hermclaw.repo_intelligence.paths import matches_any
from hermclaw.repo_intelligence.schemas import FileSymbols, ImportRecord, IndexStats, RepoInventory
from hermclaw.repo_intelligence.sources import GitTreeSource, TreeEntry, WorktreeSource
from hermclaw.repo_intelligence.symbols import replace_file_symbols

log = get_logger(__name__)
SOURCE_TYPE = "repo_intelligence"
_PARSE_BATCH = 64
_MAX_LISTED_CHANGES = 200


@dataclass(frozen=True)
class IndexTarget:
    root: Path
    repository_key: str  # index key (rows of code_symbols/code_chunks)
    workspace_id: uuid.UUID | None = None
    repository_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    repository: str | None = None  # base repository when ``repository_key`` is workspace-scoped

    @property
    def base_repository(self) -> str:
        return self.repository or self.repository_key


@dataclass
class _Parsed:
    symbols: list[FileSymbols]
    chunks: list[Chunk]
    skipped: dict[str, int]


def advisory_key(repository_key: str) -> int:
    digest = hashlib.sha256(f"hermclaw.repo_index:{repository_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class RepoIndexer:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        embedder: EmbeddingModel | None = None,
        config: RepoIntelConfig | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self.cfg = config or RepoIntelConfig()
        self.chunker = Chunker(self.cfg)
        self._locks: dict[str, asyncio.Lock] = {}
        self._embed_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------------------------------- state
    async def latest_run(self, repository_key: str, *, status: str = "finished") -> RepoIndexRun | None:
        async with self.sessionmaker() as s:
            q = (
                select(RepoIndexRun)
                .where(RepoIndexRun.stats["repository_key"].astext == repository_key, RepoIndexRun.status == status)
                .order_by(RepoIndexRun.finished_at.desc().nulls_last(), RepoIndexRun.created_at.desc())
                .limit(1)
            )
            return (await s.execute(q)).scalars().first()

    async def reuse_keys(self, target: IndexTarget) -> list[str]:
        """Other index keys of the same base repository (most recent first) whose vectors may be re-used."""
        limit = max(0, self.cfg.embedding_reuse_max_keys)
        if limit == 0:
            return []
        async with self.sessionmaker() as s:
            key_col = RepoIndexRun.stats["repository_key"].astext
            q = (
                select(key_col, func.max(RepoIndexRun.finished_at).label("last"))
                .where(
                    RepoIndexRun.status == "finished",
                    or_(RepoIndexRun.stats["repository"].astext == target.base_repository, key_col == target.base_repository),
                    key_col != target.repository_key,
                )
                .group_by(key_col)
                .order_by(func.max(RepoIndexRun.finished_at).desc().nulls_last())
                .limit(limit)
            )
            return [str(r[0]) for r in (await s.execute(q)).all() if r[0]]

    async def purge(self, target: IndexTarget) -> dict[str, int]:
        """Delete every symbol/chunk row of ``target.repository_key`` (e.g. when a job workspace is removed).

        Finished runs are marked ``purged`` so the next index of that key is a full one; history rows stay."""
        key = target.repository_key
        async with self._lock(key), self.sessionmaker() as s, s.begin():
            n_sym = _rowcount(await s.execute(delete(CodeSymbol).where(CodeSymbol.repository_key == key)))
            n_chunks = _rowcount(await s.execute(delete(CodeChunk).where(CodeChunk.repository_key == key)))
            await s.execute(
                update(RepoIndexRun)
                .where(RepoIndexRun.stats["repository_key"].astext == key, RepoIndexRun.status == "finished")
                .values(status="purged")
            )
            await append_event(
                s,
                EventType.REPO_INDEX_UPDATED,
                source_type=SOURCE_TYPE,
                source_id=key[:200],
                job_id=target.job_id,
                payload={"repository_key": key, "phase": "purge", "symbols_deleted": n_sym, "chunks_deleted": n_chunks},
            )
        return {"symbols": int(n_sym), "chunks": int(n_chunks)}

    async def current_revision(self, root: Path) -> tuple[str, str]:
        """``(revision, source)``: HEAD SHA for git workspaces, a content fingerprint otherwise."""
        cfg = self.cfg
        if await _proc.is_git_repo(root, timeout_s=cfg.git_timeout_seconds, git_binary=cfg.git_binary):
            sha = await _proc.head_sha(root, timeout_s=cfg.git_timeout_seconds, git_binary=cfg.git_binary)
            if sha:
                return sha, "git"
        src = WorktreeSource(root, cfg)
        return await src.fingerprint(await src.entries()), "worktree"

    @contextlib.asynccontextmanager
    async def _lock(self, repository_key: str) -> AsyncIterator[None]:
        local = self._locks.setdefault(repository_key, asyncio.Lock())
        async with local, self.sessionmaker() as s:
            # autocommit: the session-level advisory lock is held without keeping a transaction open for the whole run
            conn = await s.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
            key = advisory_key(repository_key)
            deadline = time.monotonic() + self.cfg.index_lock_timeout_seconds
            while not (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar():
                if time.monotonic() > deadline:
                    raise ResourceUnavailable(f"repository index '{repository_key}' is busy", code="REPO_INDEX_BUSY")
                await asyncio.sleep(0.25)
            try:
                yield
            finally:
                released = False
                with contextlib.suppress(Exception):
                    released = bool((await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})).scalar())
                if not released:  # never hand a connection that may still hold the lock back to the pool
                    with contextlib.suppress(Exception):
                        await conn.invalidate()
                with contextlib.suppress(Exception):
                    await s.rollback()

    # ------------------------------------------------------------------------------------------- public API
    async def index(self, target: IndexTarget, *, force_full: bool = False, embed: bool = True) -> IndexStats:
        """Bring the index of ``target.repository_key`` to the current revision of ``target.root``."""
        root = Path(target.root)
        started = time.monotonic()
        async with self._lock(target.repository_key):
            stats = await self._index_locked(target, root, force_full=force_full, started=started)
        if embed and self.embedder is not None:
            outcome = await self.embed(target, run_id=uuid.UUID(stats.run_id) if stats.run_id else None)
            stats.embedded, stats.embedding_pending = outcome.embedded, outcome.pending
            stats.embedding_error = outcome.error
            stats.embedding_model = self.embedder.model_name
        elif self.embedder is None:
            stats.embedding_pending = 0
        stats.duration_ms = int((time.monotonic() - started) * 1000)
        return stats

    async def embed(self, target: IndexTarget, *, run_id: uuid.UUID | None = None, max_chunks: int | None = None) -> EmbedOutcome:
        """Embed pending chunks of the repository (no-op without an embedding model)."""
        if self.embedder is None:
            return EmbedOutcome()
        lock = self._embed_locks.setdefault(target.repository_key, asyncio.Lock())
        async with lock:
            ctx = CallContext(purpose="embedding", job_id=target.job_id)
            async with self.sessionmaker() as s:
                if await count_pending(s, target.repository_key, self.embedder.model_name) == 0:
                    return EmbedOutcome()
            reuse = await self.reuse_keys(target)
            outcome = await embed_pending(
                self.sessionmaker, self.embedder, target.repository_key, self.cfg, ctx=ctx, max_chunks=max_chunks, reuse_keys=reuse
            )
            async with self.sessionmaker() as s, s.begin():
                if run_id is not None:
                    run = await s.get(RepoIndexRun, run_id)
                    if run is not None:
                        st = dict(run.stats or {})
                        st.update(
                            embedded=int(st.get("embedded", 0)) + outcome.embedded,
                            embedding_pending=outcome.pending,
                            embedding_error=outcome.error,
                            embedding_model=self.embedder.model_name,
                        )
                        run.stats = st
                await append_event(
                    s,
                    EventType.REPO_INDEX_UPDATED,
                    source_type=SOURCE_TYPE,
                    source_id=target.repository_key[:200],
                    job_id=target.job_id,
                    severity=Severity.warning if outcome.error else Severity.info,
                    payload={
                        "repository_key": target.repository_key,
                        "phase": "embedding",
                        "run_id": str(run_id) if run_id else None,
                        "embedding_model": self.embedder.model_name,
                        "embedded": outcome.embedded,
                        "pending": outcome.pending,
                        "error_code": outcome.error_code,
                        "error": outcome.error,
                    },
                )
            return outcome

    # ------------------------------------------------------------------------------------------- internals
    async def _index_locked(self, target: IndexTarget, root: Path, *, force_full: bool, started: float) -> IndexStats:
        cfg = self.cfg
        key = target.repository_key
        revision, source_kind = await self.current_revision(root)
        prev = await self.latest_run(key)
        prev_stats = (prev.stats or {}) if prev else {}
        same_version = (
            int(prev_stats.get("index_version", -1)) == INDEX_VERSION and prev_stats.get("index_config") == cfg.index_fingerprint()
        )
        if prev is not None and not force_full and same_version and prev.git_sha == revision:
            st = IndexStats.model_validate({k: v for k, v in prev_stats.items() if k in IndexStats.model_fields})
            st.mode, st.run_id, st.base_sha = "noop", str(prev.id), prev.git_sha
            return st
        mode = "full"
        if (
            prev is not None
            and not force_full
            and same_version
            and source_kind == "git"
            and prev_stats.get("source") == "git"
            and await _proc.commit_exists(root, prev.git_sha, timeout_s=cfg.git_timeout_seconds, git_binary=cfg.git_binary)
        ):
            mode = "incremental"
        workspace_id, repository_id = await self._workspace_refs(target)
        async with self.sessionmaker() as s, s.begin():
            run = RepoIndexRun(
                repository_id=repository_id,
                workspace_id=workspace_id,
                git_sha=revision,
                base_index_sha=prev.git_sha if (prev is not None and mode == "incremental") else None,
                status="running",
                stats={
                    "repository_key": key,
                    "repository": target.base_repository,
                    "mode": mode,
                    "source": source_kind,
                    "index_version": INDEX_VERSION,
                    "index_config": cfg.index_fingerprint(),
                },
            )
            s.add(run)
            await s.flush()
            run_id = run.id
            await append_event(
                s,
                EventType.REPO_INVENTORY_STARTED,
                source_type=SOURCE_TYPE,
                source_id=key[:200],
                job_id=target.job_id,
                payload={"repository_key": key, "run_id": str(run_id), "git_sha": revision, "mode": mode},
            )
        try:
            return await self._build(
                target, root, run_id=run_id, revision=revision, source_kind=source_kind, mode=mode, prev=prev, started=started
            )
        except BaseException as exc:
            await self._fail(target, run_id, exc)
            raise

    async def _workspace_refs(self, target: IndexTarget) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        if target.workspace_id is None:
            return None, target.repository_id
        async with self.sessionmaker() as s:
            ws = await s.get(Workspace, target.workspace_id)
            if ws is None:
                return None, target.repository_id
            return ws.id, target.repository_id or ws.repository_id

    async def _fail(self, target: IndexTarget, run_id: uuid.UUID, exc: BaseException) -> None:
        code = exc.code if isinstance(exc, HermclawError) else type(exc).__name__
        message = DEFAULT_REDACTOR.text(str(exc))[:500]
        with contextlib.suppress(Exception):
            async with self.sessionmaker() as s, s.begin():
                run = await s.get(RepoIndexRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.finished_at = datetime.now(UTC)
                    run.stats = {**(run.stats or {}), "error_code": code, "error": message}
                await append_event(
                    s,
                    EventType.REPO_INDEX_UPDATED,
                    source_type=SOURCE_TYPE,
                    source_id=target.repository_key[:200],
                    job_id=target.job_id,
                    severity=Severity.error,
                    payload={
                        "repository_key": target.repository_key,
                        "run_id": str(run_id),
                        "status": "failed",
                        "error_code": code,
                        "error": message,
                    },
                )

    async def _diff(self, root: Path, old: str, new: str) -> tuple[set[str], set[str], set[str]]:
        """``(changed, added, deleted)`` between two commits (``added`` is a subset of ``changed``)."""
        out = await _proc.git_ok(
            root,
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            f"{old}..{new}",
            "--",
            timeout_s=self.cfg.git_timeout_seconds,
            git_binary=self.cfg.git_binary,
            max_output=self.cfg.max_output_bytes,
        )
        items = _proc.split_z(out)
        changed: set[str] = set()
        added: set[str] = set()
        deleted: set[str] = set()
        i = 0
        while i + 1 < len(items):
            status, path = items[i], items[i + 1]
            i += 2
            if status.startswith("D"):
                deleted.add(path)
            else:  # A, M, T (and anything unexpected) -> re-read from the new tree
                changed.add(path)
                if status.startswith("A"):
                    added.add(path)
        return changed, added, deleted

    async def _build(
        self,
        target: IndexTarget,
        root: Path,
        *,
        run_id: uuid.UUID,
        revision: str,
        source_kind: str,
        mode: str,
        prev: RepoIndexRun | None,
        started: float,
    ) -> IndexStats:
        cfg = self.cfg
        key = target.repository_key
        source: GitTreeSource | WorktreeSource = GitTreeSource(root, revision, cfg) if source_kind == "git" else WorktreeSource(root, cfg)
        entries = await source.entries()
        by_path = {e.path: e for e in entries}
        inventory = await build_inventory(root, cfg)
        added: set[str] = set()
        if mode == "incremental" and prev is not None:
            changed, added, deleted = await self._diff(root, prev.git_sha, revision)
            # a changed path that is no longer a regular file in the new tree (symlink, submodule) is a deletion
            deleted |= {p for p in changed if p not in by_path}
            to_index = [by_path[p] for p in sorted(changed) if p in by_path]
        else:
            deleted = set()
            to_index = list(entries)
        resolver = ModuleResolver(by_path.keys(), psr4=await self._psr4(source, by_path))
        parsed = await self._parse(source, to_index, resolver)
        file_set_changed = mode == "full" or bool(deleted) or bool(added)
        model_name = self.embedder.model_name if self.embedder is not None else None
        stats = IndexStats(
            repository_key=key,
            repository=target.base_repository,
            git_sha=revision,
            base_sha=prev.git_sha if (prev is not None and mode == "incremental") else None,
            mode="incremental" if mode == "incremental" else "full",
            source="git" if source_kind == "git" else "worktree",
            run_id=str(run_id),
            files_total=len(entries),
            files_indexed=len(parsed.symbols),
            files_deleted=len(deleted),
            files_skipped=dict(sorted(parsed.skipped.items())),
            changed_paths=sorted(e.path for e in to_index)[:_MAX_LISTED_CHANGES] if mode == "incremental" else [],
            deleted_paths=sorted(deleted)[:_MAX_LISTED_CHANGES],
            embedding_model=model_name,
            index_version=INDEX_VERSION,
            index_config=cfg.index_fingerprint(),
        )
        indexed_paths = [e.path for e in to_index]
        reuse = await self.reuse_keys(target) if model_name else []
        async with self.sessionmaker() as s, s.begin():
            n_sym, n_imp = await replace_file_symbols(
                s, key, revision, parsed.symbols, remove_paths=set(indexed_paths) | deleted, replace_all=(mode == "full")
            )
            n_chunks, reused = await replace_file_chunks(
                s,
                key,
                revision,
                parsed.chunks,
                paths=indexed_paths,
                remove_paths=deleted,
                replace_all=(mode == "full"),
                model_name=model_name,
                reuse_keys=reuse,
            )
            if mode == "incremental" and file_set_changed:
                await self._reresolve_untouched(s, key, resolver, skip=set(indexed_paths) | deleted)
            stats.symbols, stats.imports, stats.chunks, stats.embeddings_reused = n_sym, n_imp, n_chunks, reused
            stats.embedding_pending = (await count_pending(s, key, model_name)) if model_name else 0
            stats.duration_ms = int((time.monotonic() - started) * 1000)
            await s.execute(
                update(RepoIndexRun)
                .where(RepoIndexRun.id == run_id)
                .values(
                    status="finished",
                    finished_at=datetime.now(UTC),
                    inventory=_inventory_json(inventory),
                    stats=stats.model_dump(mode="json"),
                )
            )
            await append_event(
                s,
                EventType.REPO_INVENTORY_FINISHED,
                source_type=SOURCE_TYPE,
                source_id=key[:200],
                job_id=target.job_id,
                payload={
                    "repository_key": key,
                    "run_id": str(run_id),
                    "git_sha": revision,
                    "file_count": inventory.file_count,
                    "primary_languages": inventory.primary_languages,
                    "build_systems": sorted({b.name for b in inventory.build_systems}),
                    "test_frameworks": [f.name for f in inventory.tests.frameworks],
                    "routes": len(inventory.routes),
                },
            )
            await append_event(
                s,
                EventType.REPO_INDEX_UPDATED,
                source_type=SOURCE_TYPE,
                source_id=key[:200],
                job_id=target.job_id,
                duration_ms=stats.duration_ms,
                payload={
                    "repository_key": key,
                    "phase": "index",
                    "status": "finished",
                    **stats.model_dump(
                        mode="json",
                        include={
                            "run_id",
                            "mode",
                            "git_sha",
                            "base_sha",
                            "files_total",
                            "files_indexed",
                            "files_deleted",
                            "symbols",
                            "imports",
                            "chunks",
                            "embeddings_reused",
                            "embedding_pending",
                            "files_skipped",
                        },
                    ),
                    "changed_paths": stats.changed_paths[:50],
                    "deleted_paths": stats.deleted_paths[:50],
                },
            )
        log.info(
            "repository index %s %s@%s: %d files, %d symbols, %d chunks",
            key,
            stats.mode,
            revision[:12],
            stats.files_indexed,
            stats.symbols,
            stats.chunks,
        )
        return stats

    async def _psr4(self, source: GitTreeSource | WorktreeSource, by_path: dict[str, TreeEntry]) -> dict[str, list[str]]:
        """PSR-4 autoload maps of every composer manifest of the workspace (nested PHP projects included)."""
        entries = [by_path[p] for p in composer_manifests(by_path) if by_path[p].size <= 2_000_000]
        maps: list[dict[str, list[str]]] = []
        if not entries:
            return {}
        async with contextlib.aclosing(source.read(entries)) as stream:
            async for entry, data in stream:
                try:
                    parsed = json.loads(decode_text(data))
                except ValueError:
                    continue
                base = entry.path.rsplit("/", 1)[0] if "/" in entry.path else ""
                maps.append(psr4_from_composer(parsed, base_dir=base))
        return merge_psr4(maps)

    async def _parse(self, source: GitTreeSource | WorktreeSource, entries: Sequence[TreeEntry], resolver: ModuleResolver) -> _Parsed:
        cfg = self.cfg
        skipped: dict[str, int] = {}
        wanted: list[TreeEntry] = []
        for e in entries:
            reason = None
            if matches_any(e.path, cfg.sensitive_globs):
                reason = "sensitive"
            elif matches_any(e.path, cfg.index_exclude_globs):
                reason = "excluded"
            elif e.size > cfg.max_index_file_bytes:
                reason = "too_large"
            elif is_binary_name(e.path):
                reason = "binary"
            elif e.size == 0:
                reason = "empty"
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
            else:
                wanted.append(e)
        symbols: list[FileSymbols] = []
        chunks: list[Chunk] = []
        batch: list[tuple[str, bytes]] = []

        def _work(items: list[tuple[str, bytes]]) -> tuple[list[FileSymbols], list[Chunk], dict[str, int]]:
            from hermclaw.repo_intelligence.symbols import extract_file_symbols

            out_s: list[FileSymbols] = []
            out_c: list[Chunk] = []
            skip: dict[str, int] = {}
            for path, data in items:
                if looks_binary(data[:8192]):
                    skip["binary"] = skip.get("binary", 0) + 1
                    continue
                txt = decode_text(data)
                lang = detect_language(path, data[:512]) or "text"
                try:
                    fs = extract_file_symbols(path, lang, txt)
                except Exception as exc:  # one unparsable file never fails the run: it is chunked without structure
                    fs = FileSymbols(path=path, language=lang, parse_error=f"{type(exc).__name__}: {str(exc)[:200]}")
                    skip["parse_error"] = skip.get("parse_error", 0) + 1
                for imp in fs.imports:
                    imp.resolved = resolver.resolve(path, lang, imp)
                out_s.append(fs)
                out_c.extend(self.chunker.chunk(path, lang, txt, fs.symbols))
            return out_s, out_c, skip

        async def _flush() -> None:
            if not batch:
                return
            s_, c_, k_ = await asyncio.to_thread(_work, list(batch))
            batch.clear()
            symbols.extend(s_)
            chunks.extend(c_)
            for r, n in k_.items():
                skipped[r] = skipped.get(r, 0) + n

        async with contextlib.aclosing(source.read(wanted)) as stream:
            async for entry, data in stream:
                batch.append((entry.path, data))
                if len(batch) >= _PARSE_BATCH:
                    await _flush()
        await _flush()
        return _Parsed(symbols=symbols, chunks=chunks, skipped=skipped)

    async def _reresolve_untouched(self, s: AsyncSession, key: str, resolver: ModuleResolver, *, skip: set[str]) -> int:
        """Imports of unchanged files may (un)resolve after files were added/removed: update those rows."""
        rows = (
            await s.execute(
                select(CodeSymbol.id, CodeSymbol.path, CodeSymbol.language, CodeSymbol.start_line, CodeSymbol.references).where(
                    CodeSymbol.repository_key == key, CodeSymbol.kind == "import"
                )
            )
        ).all()
        changed = 0
        for row in rows:
            if row.path in skip:
                continue
            refs = list(row.references or [])
            meta: dict[str, Any] = dict(refs[0]) if refs and isinstance(refs[0], dict) else {}
            try:
                imp = ImportRecord(
                    line=row.start_line, **{k: v for k, v in meta.items() if k in ("module", "names", "kind", "level", "resolved")}
                )
            except Exception as exc:  # malformed legacy row: leave it untouched
                log.debug("skipping malformed import row %s: %s", row.id, type(exc).__name__)
                continue
            new = resolver.resolve(row.path, row.language, imp)
            if new != meta.get("resolved"):
                meta["resolved"] = new
                await s.execute(update(CodeSymbol).where(CodeSymbol.id == row.id).values(references=[meta, *refs[1:]]))
                changed += 1
        return changed


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _inventory_json(inv: RepoInventory) -> dict[str, Any]:
    return inv.model_dump(mode="json")
