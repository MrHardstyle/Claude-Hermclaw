"""P11 failure behaviour: embedding host down/invalid, database down, failing index runs, busy locks, missing tools,
pathological files. Real PostgreSQL/pgvector, real git/ripgrep; only the embedding model is a test fake."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hermclaw.contracts.events import EventType
from hermclaw.core.errors import ExternalServiceError, HermclawError, NotFoundError, ResourceUnavailable, ValidationFailed
from hermclaw.models.protocols import CallContext
from hermclaw.persistence.models import CodeChunk, Event, RepoIndexRun
from hermclaw.repo_intelligence import IndexTarget, RepoIndexer, RepoIntelConfig, RepoIntelligence, build_inventory
from hermclaw.repo_intelligence.indexer import advisory_key
from hermclaw.repo_intelligence.sources import GitTreeSource
from tests.integration.test_repo_intelligence_support import HashingEmbedder, build_fixture_repo, commit_all, workspace_handle, write_files

pytestmark = pytest.mark.integration


def _key() -> str:
    return f"fail-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return build_fixture_repo(tmp_path / "fx")


class _BadEmbedder(HashingEmbedder):
    def __init__(self, mode: str) -> None:
        super().__init__(model_name=f"bad-{mode}")
        self.mode = mode

    async def embed(self, texts: list[str], *, ctx: CallContext) -> list[list[float]]:
        if self.mode == "dim":
            return [[0.1] * 12 for _ in texts]
        if self.mode == "count":
            return [self.vector(t) for t in texts[:-1]]
        if self.mode == "nan":
            return [[float("nan")] * 768 for _ in texts]
        if self.mode == "zero":
            return [[0.0] * 768 for _ in texts]
        raise ConnectionError("embedding host 192.168.178.20 unreachable, token=supersecretvalue")


class _SlowEmbedder(HashingEmbedder):
    async def embed(self, texts: list[str], *, ctx: CallContext) -> list[list[float]]:
        await asyncio.sleep(0.6)
        return await super().embed(texts, ctx=ctx)


async def test_embedding_host_down_degrades_and_recovers(sessionmaker: Any, repo: Path) -> None:
    emb = HashingEmbedder(fail_with=ExternalServiceError("embedding service unavailable", code="MODEL_UNAVAILABLE"))
    key = _key()
    indexer = RepoIndexer(sessionmaker, embedder=emb)
    target = IndexTarget(root=repo, repository_key=key)
    stats = await indexer.index(target)
    assert stats.mode == "full" and stats.chunks > 0
    assert stats.embedded == 0 and stats.embedding_pending == stats.chunks and stats.embedding_error
    async with sessionmaker() as s:
        run = await s.get(RepoIndexRun, uuid.UUID(stats.run_id or ""))
        assert run is not None and run.status == "finished" and run.stats["embedding_error"]
        warn = (
            (
                await s.execute(
                    select(Event).where(
                        Event.source_id == key, Event.event_type == EventType.REPO_INDEX_UPDATED, Event.severity == "warning"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert warn and warn[0].payload["error_code"] == "MODEL_UNAVAILABLE"
    # lexical/structural retrieval keeps working; the semantic signal is reported as degraded
    svc = RepoIntelligence(sessionmaker, embedder=emb, config=RepoIntelConfig(index_scope="repository"))
    out = await svc.search_files(target, "calculate invoice total", k=5)
    assert out.hits and out.hits[0].path == "app/services/billing.py"
    assert any(d.startswith("semantic: MODEL_UNAVAILABLE") for d in out.degraded)
    # the model is back: pending chunks are embedded, no full re-index needed
    emb.fail_with = None
    outcome = await indexer.embed(target)
    assert outcome.embedded == stats.chunks and outcome.pending == 0 and outcome.error is None
    assert (await svc.search_files(target, "calculate invoice total", k=5)).degraded == []
    await svc.aclose()


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("dim", "EMBEDDING_DIM_MISMATCH"),
        ("count", "EMBEDDING_COUNT_MISMATCH"),
        ("nan", "EMBEDDING_INVALID"),
        ("zero", "EMBEDDING_INVALID"),
        ("conn", "EMBEDDING_FAILED"),
    ],
)
async def test_invalid_embeddings_are_rejected_not_stored(sessionmaker: Any, repo: Path, mode: str, code: str) -> None:
    key = _key()
    indexer = RepoIndexer(sessionmaker, embedder=_BadEmbedder(mode))
    stats = await indexer.index(IndexTarget(root=repo, repository_key=key))
    assert stats.embedded == 0 and stats.embedding_pending == stats.chunks
    assert stats.embedding_error and "supersecretvalue" not in stats.embedding_error
    async with sessionmaker() as s:
        stored = (await s.execute(select(CodeChunk).where(CodeChunk.repository_key == key, CodeChunk.embedding.is_not(None)))).all()
        ev = (
            (
                await s.execute(
                    select(Event).where(Event.source_id == key, Event.event_type == EventType.REPO_INDEX_UPDATED).order_by(Event.sequence)
                )
            )
            .scalars()
            .all()
        )
    assert stored == []
    assert ev[-1].payload["phase"] == "embedding" and ev[-1].payload["error_code"] == code
    assert "supersecretvalue" not in str(ev[-1].payload)


async def test_slow_embedding_does_not_block_queries(sessionmaker: Any, repo: Path) -> None:
    emb = _SlowEmbedder()
    svc = RepoIntelligence(sessionmaker, embedder=emb, config=RepoIntelConfig(inline_embed_seconds=0.1, embed_batch_size=8))
    bound = svc.open(repo, _key())
    started = time.monotonic()
    hits = await bound.symbols("calculate_invoice_total")
    assert hits and time.monotonic() - started < 5
    await svc.wait_background()  # embedding finished in the background
    async with sessionmaker() as s:
        pending = (
            await s.execute(select(CodeChunk).where(CodeChunk.repository_key == bound.target.repository_key, CodeChunk.embedding.is_(None)))
        ).all()
    assert pending == []
    await svc.aclose()


async def test_database_down_degrades_to_live_retrieval(repo: Path) -> None:
    engine = create_async_engine("postgresql+psycopg://nobody@127.0.0.1:1/nowhere", pool_pre_ping=False)
    dead = async_sessionmaker(engine, expire_on_commit=False)
    svc = RepoIntelligence(dead, config=RepoIntelConfig(index_wait_seconds=10, query_timeout_seconds=5))
    ws = workspace_handle(repo)
    hits = await svc.search(ws, "calculate invoice total", k=5)
    assert hits and hits[0].path == "app/services/billing.py" and "lexical" in hits[0].signals
    out = await svc.search_files(svc.target_of(ws), "calculate invoice total", k=5)
    assert any(d.startswith("index:") for d in out.degraded) and any(d.startswith("symbol:") for d in out.degraded)
    sym = await svc.find_symbol(ws, "calculate_invoice_total")
    assert sym and sym[0].path == "app/services/billing.py"  # live parse of the files that mention the name
    assert (await svc.read(ws, "app/services/billing.py", 6, 6)).startswith("def calculate_invoice_total")
    inv = await svc.inventory(svc.target_of(ws))
    assert inv.file_count > 20 and any(w.startswith("index:") for w in inv.warnings)
    await svc.aclose()
    await engine.dispose()


async def test_failing_index_run_is_recorded(sessionmaker: Any, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(self: GitTreeSource, entries: Any) -> Any:
        raise RuntimeError("disk read error password=topsecret123")
        yield  # pragma: no cover

    monkeypatch.setattr(GitTreeSource, "read", broken)
    key = _key()
    indexer = RepoIndexer(sessionmaker)
    with pytest.raises(RuntimeError):
        await indexer.index(IndexTarget(root=repo, repository_key=key))
    async with sessionmaker() as s:
        runs = (await s.execute(select(RepoIndexRun).where(RepoIndexRun.stats["repository_key"].astext == key))).scalars().all()
        evs = (await s.execute(select(Event).where(Event.source_id == key).order_by(Event.sequence))).scalars().all()
    assert len(runs) == 1 and runs[0].status == "failed" and runs[0].finished_at is not None
    assert runs[0].stats["error_code"] == "RuntimeError" and "topsecret123" not in runs[0].stats["error"]
    assert evs[-1].event_type == EventType.REPO_INDEX_UPDATED and evs[-1].severity == "error"
    assert "topsecret123" not in str(evs[-1].payload)
    # a failed run is never the base of an incremental run
    monkeypatch.undo()
    assert (await indexer.index(IndexTarget(root=repo, repository_key=key))).mode == "full"


async def test_busy_index_lock_times_out(sessionmaker: Any, repo: Path) -> None:
    key = _key()
    async with sessionmaker() as other:
        conn = await other.connection()
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": advisory_key(key)})
        indexer = RepoIndexer(sessionmaker, config=RepoIntelConfig(index_lock_timeout_seconds=0.5))
        with pytest.raises(ResourceUnavailable) as exc:
            await indexer.index(IndexTarget(root=repo, repository_key=key))
        assert exc.value.code == "REPO_INDEX_BUSY"
        await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": advisory_key(key)})
    assert (await indexer.index(IndexTarget(root=repo, repository_key=key))).mode == "full"


async def test_pathological_files_never_fail_the_index(sessionmaker: Any, repo: Path) -> None:
    write_files(
        repo,
        {
            "broken/syntax.py": "def broken(:\n    pass\n\nclass Fine:\n    pass\n",
            "broken/deep.py": "x = " + "(" * 3000 + "1" + ")" * 3000 + "\n",
            "broken/garbage.js": "function (((( {{{{ ]]] class extends\n" * 50,
            "broken/empty.py": "",
            "broken/latin1.php": "<?php\n// caf\xe9\nfunction latin() {}\n",
        },
    )
    (repo / "broken/huge.py").write_text("X = 1\n" * 200_000, encoding="utf-8")
    (repo / "broken/fake_text.py").write_bytes(b"def x():\n\x00\x00\x00 binary payload")
    commit_all(repo, "pathological")
    key = _key()
    stats = await RepoIndexer(sessionmaker, embedder=HashingEmbedder()).index(IndexTarget(root=repo, repository_key=key))
    assert stats.mode == "full" and stats.embedding_pending == 0
    assert stats.files_skipped["too_large"] >= 1 and stats.files_skipped["empty"] >= 1 and stats.files_skipped["binary"] >= 2
    async with sessionmaker() as s:
        paths = {r[0] for r in (await s.execute(select(CodeChunk.path).where(CodeChunk.repository_key == key))).all()}
    assert {"broken/syntax.py", "broken/deep.py", "broken/garbage.js", "broken/latin1.php"} <= paths  # chunked without structure
    assert "broken/huge.py" not in paths and "broken/fake_text.py" not in paths


async def test_missing_git_and_missing_workspace(repo: Path, sessionmaker: Any, tmp_path: Path) -> None:
    inv = await build_inventory(repo, RepoIntelConfig(git_binary="/nonexistent/git"))
    assert not inv.git.is_repo and inv.file_count > 20  # falls back to the non-git listing
    svc = RepoIntelligence(sessionmaker)
    with pytest.raises(NotFoundError):
        await svc.search_files(IndexTarget(root=tmp_path / "does-not-exist", repository_key="k"), "x")
    with pytest.raises(ValidationFailed):
        await svc.search_files(IndexTarget(root=repo, repository_key=""), "x")
    with pytest.raises(ValidationFailed):
        await svc.search_files(IndexTarget(root=repo, repository_key="k" * 301), "x")
    empty = await svc.search_files(IndexTarget(root=repo, repository_key=_key()), "   ")
    assert empty.hits == [] and empty.degraded == []
    with pytest.raises(HermclawError):
        await svc.read_range(IndexTarget(root=repo, repository_key=_key()), "../../etc/passwd")
    await svc.aclose()
