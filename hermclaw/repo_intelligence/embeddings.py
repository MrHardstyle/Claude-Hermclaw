"""Semantic index: chunk storage, batched embedding and pgvector cosine search (P11 11.8/11.9).

* chunks live in ``code_chunks`` (``vector(768)`` + ``embedding_model``); vectors of different models are never mixed
  (a chunk embedded by another model counts as pending and is re-embedded);
* embedding happens *after* the index transaction committed, in short transactions per batch, so a slow or
  unreachable embedding host never blocks lexical/structural retrieval; failures are recorded, not raised;
* identical embedding inputs (same ``content_hash``) re-use the stored vector instead of calling the model again;
* search: exact scan for small indexes, HNSW (``vector_cosine_ops``) with a raised ``ef_search`` otherwise.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.errors import HermclawError, ValidationFailed
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.models.protocols import CallContext, EmbeddingModel
from hermclaw.persistence.models import EMBEDDING_DIM, CodeChunk
from hermclaw.repo_intelligence.chunking import Chunk, query_text
from hermclaw.repo_intelligence.config import RepoIntelConfig
from hermclaw.repo_intelligence.schemas import ChunkHit

log = get_logger(__name__)


def validate_vectors(vectors: Sequence[Sequence[float]], expected: int) -> list[list[float]]:
    """Embeddings must come back one per input, with ``EMBEDDING_DIM`` finite components."""
    if len(vectors) != expected:
        raise ValidationFailed(f"embedding model returned {len(vectors)} vectors for {expected} inputs", code="EMBEDDING_COUNT_MISMATCH")
    out: list[list[float]] = []
    for v in vectors:
        if len(v) != EMBEDDING_DIM:
            raise ValidationFailed(f"embedding has {len(v)} dimensions, index expects {EMBEDDING_DIM}", code="EMBEDDING_DIM_MISMATCH")
        vec = [float(x) for x in v]
        if not all(math.isfinite(x) for x in vec):
            raise ValidationFailed("embedding contains non-finite values", code="EMBEDDING_INVALID")
        if not any(vec):
            raise ValidationFailed("embedding is the zero vector (cosine undefined)", code="EMBEDDING_INVALID")
        out.append(vec)
    return out


# ============================================================================================= storage
async def reusable_embeddings(session: AsyncSession, repository_key: str, hashes: Iterable[str], model_name: str | None) -> dict[str, Any]:
    """``content_hash -> embedding`` of existing chunks embedded by ``model_name`` (to avoid re-embedding)."""
    if model_name is None:
        return {}
    wanted = sorted(set(hashes))
    out: dict[str, Any] = {}
    for i in range(0, len(wanted), 1_000):
        rows = await session.execute(
            select(CodeChunk.content_hash, CodeChunk.embedding).where(
                CodeChunk.repository_key == repository_key,
                CodeChunk.embedding_model == model_name,
                CodeChunk.embedding.is_not(None),
                CodeChunk.content_hash.in_(wanted[i : i + 1_000]),
            )
        )
        for h, emb in rows.all():
            out.setdefault(h, emb)
    return out


async def replace_file_chunks(
    session: AsyncSession,
    repository_key: str,
    git_sha: str,
    chunks: Sequence[Chunk],
    *,
    paths: Iterable[str],
    remove_paths: Iterable[str] = (),
    replace_all: bool = False,
    model_name: str | None = None,
    batch: int = 1_000,
) -> tuple[int, int]:
    """Replace the chunks of ``paths`` (+ drop ``remove_paths``); returns ``(inserted, embeddings_reused)``."""
    reuse = await reusable_embeddings(session, repository_key, (c.content_hash for c in chunks), model_name)
    if replace_all:
        await session.execute(delete(CodeChunk).where(CodeChunk.repository_key == repository_key))
    else:
        doomed = sorted(set(paths) | set(remove_paths))
        for i in range(0, len(doomed), 1_000):
            await session.execute(
                delete(CodeChunk).where(CodeChunk.repository_key == repository_key, CodeChunk.path.in_(doomed[i : i + 1_000]))
            )
    rows: list[dict[str, Any]] = []
    reused = 0
    for c in chunks:
        emb = reuse.get(c.content_hash)
        if emb is not None:
            reused += 1
        rows.append(
            {
                "id": uuid.uuid4(),
                "repository_key": repository_key,
                "git_sha": git_sha,
                "path": c.path,
                "symbol": c.symbol,
                "language": c.language[:32],
                "start_line": c.start_line,
                "end_line": c.end_line,
                "content_hash": c.content_hash,
                "content": c.content,
                "embedding": emb,
                "embedding_model": model_name if emb is not None else None,
            }
        )
    for i in range(0, len(rows), batch):
        await session.execute(insert(CodeChunk), rows[i : i + batch])
    return len(rows), reused


def _pending_filter(repository_key: str, model_name: str) -> Any:
    return (
        CodeChunk.repository_key == repository_key,
        or_(CodeChunk.embedding.is_(None), CodeChunk.embedding_model.is_(None), CodeChunk.embedding_model != model_name),
    )


async def count_pending(session: AsyncSession, repository_key: str, model_name: str) -> int:
    q = select(func.count()).select_from(CodeChunk).where(*_pending_filter(repository_key, model_name))
    return int((await session.execute(q)).scalar_one())


@dataclass
class EmbedOutcome:
    embedded: int = 0
    pending: int = 0
    batches: int = 0
    error: str | None = None
    error_code: str | None = None


async def embed_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: EmbeddingModel,
    repository_key: str,
    cfg: RepoIntelConfig,
    *,
    ctx: CallContext,
    max_chunks: int | None = None,
) -> EmbedOutcome:
    """Embed chunks without a vector of the current model, batch by batch (keyset pagination on ``id``)."""
    from hermclaw.repo_intelligence.chunking import document_text

    out = EmbedOutcome()
    model = embedder.model_name
    budget = max_chunks if max_chunks is not None else cfg.embed_max_chunks_per_run
    size = max(1, cfg.embed_batch_size)
    last_id: uuid.UUID | None = None
    while out.embedded < budget:
        async with sessionmaker() as s:
            q = select(CodeChunk.id, CodeChunk.path, CodeChunk.symbol, CodeChunk.content, CodeChunk.content_hash).where(
                *_pending_filter(repository_key, model)
            )
            if last_id is not None:
                q = q.where(CodeChunk.id > last_id)
            rows = (await s.execute(q.order_by(CodeChunk.id).limit(min(size, budget - out.embedded)))).all()
        if not rows:
            break
        last_id = rows[-1].id
        texts = [document_text(r.path, r.symbol, r.content, cfg) for r in rows]
        try:
            vectors = validate_vectors(await embedder.embed(texts, ctx=ctx), len(texts))
        except HermclawError as exc:
            out.error, out.error_code = DEFAULT_REDACTOR.text(exc.message)[:500], exc.code
            break
        except Exception as exc:  # transport errors of foreign implementations: recorded, never fatal for the index
            out.error, out.error_code = DEFAULT_REDACTOR.text(f"{type(exc).__name__}: {exc}")[:500], "EMBEDDING_FAILED"
            break
        async with sessionmaker() as s, s.begin():
            for r, vec in zip(rows, vectors, strict=True):
                await s.execute(
                    update(CodeChunk)
                    .where(CodeChunk.id == r.id, CodeChunk.content_hash == r.content_hash)
                    .values(embedding=vec, embedding_model=model)
                )
        out.embedded += len(rows)
        out.batches += 1
    async with sessionmaker() as s:
        out.pending = await count_pending(s, repository_key, model)
    if out.error:
        log.warning("embedding stopped for %s: %s (%s)", repository_key, out.error_code, out.error[:200])
    return out


# ============================================================================================= search
async def semantic_search(
    session: AsyncSession,
    embedder: EmbeddingModel,
    repository_key: str,
    query: str,
    *,
    k: int,
    cfg: RepoIntelConfig,
    ctx: CallContext,
    exclude_paths: Iterable[str] = (),
) -> list[ChunkHit]:
    """Top-``k`` chunks by cosine distance (``<=>``) to the query embedding, nearest first."""
    if k <= 0 or not query.strip():
        return []
    [qvec] = validate_vectors(await embedder.embed([query_text(query, cfg)], ctx=ctx), 1)
    model = embedder.model_name
    total = (
        await session.execute(
            select(func.count())
            .select_from(CodeChunk)
            .where(CodeChunk.repository_key == repository_key, CodeChunk.embedding_model == model, CodeChunk.embedding.is_not(None))
        )
    ).scalar_one()
    if not total:
        return []
    excluded = set(exclude_paths)
    limit = k + min(len(excluded) * 4, 400)
    if total <= cfg.semantic_exact_scan_max_rows:
        await session.execute(text("SET LOCAL enable_indexscan = off"))  # exact KNN (bitmap scans on the key stay allowed)
    else:
        ef = max(int(cfg.hnsw_ef_search), limit * 4)
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {min(ef, 1000):d}"))
    distance = CodeChunk.embedding.cosine_distance(qvec).label("distance")
    stmt = (
        select(
            CodeChunk.path,
            CodeChunk.start_line,
            CodeChunk.end_line,
            CodeChunk.symbol,
            CodeChunk.language,
            CodeChunk.content,
            CodeChunk.git_sha,
            distance,
        )
        .where(CodeChunk.repository_key == repository_key, CodeChunk.embedding_model == model, CodeChunk.embedding.is_not(None))
        .order_by(distance, CodeChunk.path, CodeChunk.start_line)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    hits: list[ChunkHit] = []
    for r in rows:
        if r.path in excluded:
            continue
        d = float(r.distance)
        hits.append(
            ChunkHit(
                path=r.path,
                start_line=r.start_line,
                end_line=r.end_line,
                symbol=r.symbol,
                language=r.language,
                score=max(-1.0, min(1.0, 1.0 - d)),
                distance=d,
                content=r.content,
                git_sha=r.git_sha,
            )
        )
        if len(hits) >= k:
            break
    return hits
