"""Storage and search of Hermclaw's knowledge (D-019).

An entry is a short Markdown text with a kind, a status and an origin. Only ``active`` entries reach a model:
``candidate`` entries wait for evidence (the same incident in ``promote_after`` jobs) or for the operator, who reads and
edits everything on the "Wissen" page. Search is semantic (cosine distance of the embedding, as in repository
intelligence) with a word match for entries without a vector of the current embedding model, so the knowledge stays
usable while the embedding model is unavailable.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import MemoryPolicy
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.models.protocols import CallContext, EmbeddingModel
from hermclaw.persistence.models import MemoryEntry
from hermclaw.repo_intelligence.embeddings import validate_vectors

log = get_logger(__name__)

KINDS = ("lesson", "research", "incident", "tool_bug", "convention", "note")
STATUSES = ("candidate", "active", "retired")
SOURCES = ("system", "operator", "research")
KIND_LABELS = {
    "lesson": "Lehre",
    "research": "Recherche",
    "incident": "Vorfall",
    "tool_bug": "Werkzeug-Bug",
    "convention": "Konvention",
    "note": "Notiz",
}
_WORD_RE = re.compile(r"[\w.-]{3,}", re.UNICODE)
_TEXT_SCAN_MAX_ROWS = 2000


@dataclass(frozen=True)
class MemoryHit:
    id: uuid.UUID
    kind: str
    title: str
    content: str
    status: str
    scope: str | None
    score: float

    def render(self, max_chars: int) -> str:
        """The entry as a model sees it: kind, title, text – clipped, secrets masked."""
        body = self.content.strip()
        if len(body) > max_chars:
            body = body[: max_chars - 1].rstrip() + "…"
        head = f"[{self.kind}] {self.title}".strip()
        return DEFAULT_REDACTOR.text(f"{head}\n{body}" if body else head)


def entry_text(title: str, content: str) -> str:
    """The text an entry is embedded and word-matched by."""
    return f"{title.strip()}\n\n{content.strip()}".strip()


def _words(text: str) -> set[str]:
    return {w.lower().strip(".-") for w in _WORD_RE.findall(text) if len(w.strip(".-")) >= 3}


def word_score(query: str, text: str) -> float:
    """Share of the query's words found in ``text`` (0..1): the search for entries without a usable vector."""
    want = _words(query)
    if not want:
        return 0.0
    have = _words(text)
    return len(want & have) / len(want)


def _live(stmt: Any, statuses: Sequence[str], scopes: Sequence[str] | None) -> Any:
    now = datetime.now(UTC)
    stmt = stmt.where(MemoryEntry.status.in_(list(statuses)), or_(MemoryEntry.expires_at.is_(None), MemoryEntry.expires_at > now))
    if scopes is not None:
        stmt = stmt.where(or_(MemoryEntry.scope.is_(None), MemoryEntry.scope.in_(list(scopes))))
    return stmt


class MemoryStore:
    """Knowledge entries in PostgreSQL; ``embedder`` may be None (word match only)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], embedder: EmbeddingModel | None, policy: MemoryPolicy) -> None:
        self.sessionmaker, self.embedder, self.policy = sessionmaker, embedder, policy

    # ------------------------------------------------------------------------------------------ embedding
    async def _embed(self, text: str, ctx: CallContext | None) -> tuple[list[float] | None, str | None]:
        if self.embedder is None or not text.strip():
            return None, None
        try:
            [vec] = validate_vectors(await self.embedder.embed([text], ctx=ctx or CallContext(purpose="memory")), 1)
        except Exception as exc:  # the entry stays findable by its words
            log.warning("memory embedding failed", extra={"error_type": type(exc).__name__})
            return None, None
        return vec, self.embedder.model_name

    # ------------------------------------------------------------------------------------------ writing
    async def add(
        self,
        *,
        kind: str,
        title: str,
        content: str,
        status: str = "candidate",
        source: str = "system",
        scope: str | None = None,
        tags: Iterable[str] = (),
        signature: str | None = None,
        job_id: uuid.UUID | None = None,
        evidence: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
        ctx: CallContext | None = None,
    ) -> uuid.UUID:
        _check(kind=kind, status=status, source=source)
        vec, model = await self._embed(entry_text(title, content), ctx)
        row = MemoryEntry(
            kind=kind,
            title=title.strip()[:300],
            content=content.strip(),
            status=status,
            source=source,
            scope=scope,
            tags=sorted({t.strip() for t in tags if t.strip()}),
            signature=signature[:300] if signature else None,
            job_id=job_id,
            evidence=evidence or {},
            embedding=vec,
            embedding_model=model,
            expires_at=expires_at,
        )
        async with self.sessionmaker() as s:
            s.add(row)
            await s.commit()
            return row.id

    async def update(self, entry_id: uuid.UUID, *, ctx: CallContext | None = None, **changes: Any) -> MemoryEntry | None:
        """Change fields of an entry; a changed title or text is embedded again."""
        allowed = {"kind", "title", "content", "status", "scope", "tags", "expires_at", "evidence", "source"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown memory fields: {sorted(unknown)}")
        _check(kind=changes.get("kind"), status=changes.get("status"), source=changes.get("source"))
        async with self.sessionmaker() as s:
            row = await s.get(MemoryEntry, entry_id)
            if row is None:
                return None
            for key, raw in changes.items():
                value = raw
                if key == "tags":
                    value = sorted({str(t).strip() for t in raw if str(t).strip()})
                elif key in ("title", "content") and isinstance(raw, str):
                    value = raw.strip()[:300] if key == "title" else raw.strip()
                setattr(row, key, value)
            if {"title", "content"} & set(changes):
                row.embedding, row.embedding_model = await self._embed(entry_text(row.title, row.content), ctx)
            await s.commit()
            await s.refresh(row)
            return row

    async def record_incident(
        self,
        *,
        signature: str,
        title: str,
        content: str,
        job_id: uuid.UUID | None,
        evidence: dict[str, Any],
        kind: str = "incident",
        scope: str | None = None,
        tags: Iterable[str] = (),
        ctx: CallContext | None = None,
    ) -> tuple[uuid.UUID, int]:
        """One entry per failure signature: a known one counts the new job (once per job) – also a retired one, which
        stays retired, so an entry the operator rejected does not come back; returns (id, jobs seen)."""
        async with self.sessionmaker() as s:
            row = (
                await s.execute(
                    select(MemoryEntry)
                    .where(MemoryEntry.signature == signature[:300], MemoryEntry.kind == kind)
                    .order_by(MemoryEntry.created_at)
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None:
                jobs = list(row.evidence.get("jobs", []))
                if job_id is not None and str(job_id) not in jobs:
                    jobs.append(str(job_id))
                    row.occurrences = len(jobs)
                    examples = list(row.evidence.get("examples", []))[-4:]
                    row.evidence = {**row.evidence, "jobs": jobs[-50:], "examples": [*examples, evidence]}
                await s.commit()
                return row.id, row.occurrences
        jobs = [str(job_id)] if job_id is not None else []
        entry_id = await self.add(
            kind=kind,
            title=title,
            content=content,
            signature=signature,
            job_id=job_id,
            scope=scope,
            tags=tags,
            evidence={"jobs": jobs, "examples": [evidence]},
            ctx=ctx,
        )
        return entry_id, 1

    # ------------------------------------------------------------------------------------------ reading
    async def by_signature(self, signature: str, *, kinds: Sequence[str] | None = None) -> list[MemoryEntry]:
        stmt = select(MemoryEntry).where(MemoryEntry.signature == signature[:300]).order_by(MemoryEntry.created_at)
        if kinds:
            stmt = stmt.where(MemoryEntry.kind.in_(list(kinds)))
        async with self.sessionmaker() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def for_signature(self, signature: str, *, scopes: Sequence[str] | None = None, count: bool = True) -> list[MemoryHit]:
        """Active entries recorded for exactly this failure signature (score 1), each counted as a recall."""
        async with self.sessionmaker() as s:
            stmt = _live(select(MemoryEntry), ("active",), scopes).where(MemoryEntry.signature == signature[:300])
            rows = list((await s.execute(stmt.order_by(MemoryEntry.updated_at.desc()).limit(5))).scalars())
            if count and rows:
                await s.execute(update(MemoryEntry).where(MemoryEntry.id.in_([r.id for r in rows])).values(recalls=MemoryEntry.recalls + 1))
                await s.commit()
            return [_hit(r, 1.0) for r in rows]

    async def get(self, entry_id: uuid.UUID) -> MemoryEntry | None:
        async with self.sessionmaker() as s:
            return await s.get(MemoryEntry, entry_id)

    async def entries(
        self, *, kind: str | None = None, status: str | None = None, q: str | None = None, limit: int = 200
    ) -> list[MemoryEntry]:
        stmt = select(MemoryEntry).order_by(MemoryEntry.updated_at.desc()).limit(max(1, min(limit, 1000)))
        if kind:
            stmt = stmt.where(MemoryEntry.kind == kind)
        if status:
            stmt = stmt.where(MemoryEntry.status == status)
        if q and q.strip():
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(MemoryEntry.title.ilike(like), MemoryEntry.content.ilike(like)))
        async with self.sessionmaker() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def search(
        self,
        query: str,
        *,
        k: int | None = None,
        scopes: Sequence[str] | None = None,
        kinds: Sequence[str] | None = None,
        statuses: Sequence[str] = ("active",),
        min_score: float | None = None,
        count: bool = True,
        ctx: CallContext | None = None,
    ) -> list[MemoryHit]:
        """The ``k`` entries closest to ``query`` (nearest first), at least ``min_score`` similar."""
        k = self.policy.recall_k if k is None else k
        floor = self.policy.min_score if min_score is None else min_score
        if k <= 0 or not query.strip():
            return []
        hits: dict[uuid.UUID, MemoryHit] = {}
        vec, model = await self._embed(query, ctx)
        async with self.sessionmaker() as s:
            if vec is not None:
                await s.execute(text("SET LOCAL enable_indexscan = off"))  # exact KNN: the knowledge is small
                distance = MemoryEntry.embedding.cosine_distance(vec).label("distance")
                stmt = _live(select(MemoryEntry, distance), statuses, scopes).where(
                    MemoryEntry.embedding_model == model, MemoryEntry.embedding.is_not(None)
                )
                if kinds:
                    stmt = stmt.where(MemoryEntry.kind.in_(list(kinds)))
                for row, dist in (await s.execute(stmt.order_by(distance).limit(k * 3))).all():
                    score = max(-1.0, min(1.0, 1.0 - float(dist)))
                    if score >= floor:
                        hits[row.id] = _hit(row, score)
            # entries without a vector of this model: matched by their words
            stmt = _live(select(MemoryEntry), statuses, scopes)
            if model is not None:
                stmt = stmt.where(or_(MemoryEntry.embedding.is_(None), MemoryEntry.embedding_model != model))
            if kinds:
                stmt = stmt.where(MemoryEntry.kind.in_(list(kinds)))
            for row in (await s.execute(stmt.limit(_TEXT_SCAN_MAX_ROWS))).scalars():
                score = word_score(query, entry_text(row.title, row.content))
                if score >= max(floor, 0.34) and row.id not in hits:
                    hits[row.id] = _hit(row, score)
            ranked = sorted(hits.values(), key=lambda h: (-h.score, h.title))[:k]
            if count and ranked:
                await s.execute(
                    update(MemoryEntry).where(MemoryEntry.id.in_([h.id for h in ranked])).values(recalls=MemoryEntry.recalls + 1)
                )
            await s.commit()
        return ranked


def _hit(row: MemoryEntry, score: float) -> MemoryHit:
    return MemoryHit(row.id, row.kind, row.title, row.content, row.status, row.scope, round(score, 4))


def _check(*, kind: str | None, status: str | None, source: str | None) -> None:
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if source is not None and source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
