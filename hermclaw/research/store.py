"""Persistence of research runs, sources, claims and claim-source links (PostgreSQL is the source of truth).

Every write goes through its own short transaction together with the matching event, so the UI stream shows each
step as soon as it happened (Bauplan §23 "Keine unsichtbaren Quellen").
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.research import Claim, ResearchContract, SourceRecord
from hermclaw.core.errors import NotFoundError, ValidationFailed
from hermclaw.persistence.models import ResearchClaim, ResearchClaimSource, ResearchRun, ResearchSource


@dataclass(frozen=True)
class SourceRow:
    """Everything persisted for one candidate source (read, failed or skipped)."""

    url: str
    domain: str
    title: str = ""
    status: str = "read"  # read|failed|skipped
    error: str | None = None
    published_at: datetime | None = None
    source_type: str = "unknown"
    authority_score: float = 0.0
    relevance_score: float = 0.0
    content_hash: str = ""
    excerpt: str | None = None
    retrieved_at: datetime | None = None


async def create_run(
    session: AsyncSession, *, question: str, job_id: uuid.UUID | None, step_id: uuid.UUID | None, run_id: uuid.UUID | None = None
) -> ResearchRun:
    run = ResearchRun(id=run_id or uuid.uuid4(), job_id=job_id, step_id=step_id, question=question, status="running", queries=[])
    session.add(run)
    await session.flush()
    return run


async def set_queries(session: AsyncSession, run_id: uuid.UUID, queries: Sequence[str], *, model_alias: str | None) -> None:
    await session.execute(update(ResearchRun).where(ResearchRun.id == run_id).values(queries=list(queries), model_alias=model_alias))


async def add_source(session: AsyncSession, run_id: uuid.UUID, row: SourceRow) -> ResearchSource:
    src = ResearchSource(
        id=uuid.uuid4(),
        research_run_id=run_id,
        title=row.title[:2000],
        url=row.url,
        domain=row.domain[:300] or "unknown",
        published_at=row.published_at,
        source_type=row.source_type,
        authority_score=float(row.authority_score),
        relevance_score=float(row.relevance_score),
        content_hash=row.content_hash[:64],
        excerpt=row.excerpt,
        status=row.status,
        error=row.error,
    )
    if row.retrieved_at is not None:
        src.retrieved_at = row.retrieved_at
    session.add(src)
    await session.flush()
    return src


async def add_claim(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    claim: str,
    confidence: float,
    source_ids: Sequence[uuid.UUID],
    contradiction_group: int | None,
    created_at: datetime | None = None,
) -> ResearchClaim:
    """``created_at`` orders the claims of a run (claim index = position); pass strictly increasing values."""
    if not source_ids:
        raise ValidationFailed("a research claim needs at least one source")
    row = ResearchClaim(
        id=uuid.uuid4(),
        research_run_id=run_id,
        claim=claim,
        confidence=float(confidence),
        contradiction_group=contradiction_group,
    )
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    await session.flush()
    session.add_all(ResearchClaimSource(claim_id=row.id, source_id=sid) for sid in dict.fromkeys(source_ids))
    await session.flush()
    return row


async def mark_claims_used(session: AsyncSession, claim_ids: Sequence[uuid.UUID], decision_ref: str) -> int:
    if not claim_ids:
        return 0
    result = await session.execute(
        update(ResearchClaim).where(ResearchClaim.id.in_(list(claim_ids))).values(used_for_decision=True, decision_ref=decision_ref[:2000])
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def finish_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    status: str,
    synthesis: str | None,
    contradictions: list[list[int]],
    model_alias: str | None,
) -> None:
    values: dict[str, Any] = {
        "status": status,
        "synthesis": synthesis,
        "contradictions": contradictions,
        "finished_at": datetime.now(UTC),
    }
    if model_alias is not None:
        values["model_alias"] = model_alias
    await session.execute(update(ResearchRun).where(ResearchRun.id == run_id).values(**values))


async def claim_ids_in_order(session: AsyncSession, run_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        select(ResearchClaim.id).where(ResearchClaim.research_run_id == run_id).order_by(ResearchClaim.created_at, ResearchClaim.id)
    )
    return list(rows.scalars())


async def load_contract(session: AsyncSession, run_id: uuid.UUID) -> ResearchContract:
    """Rebuild the :class:`ResearchContract` of a persisted run (sources with status ``read`` only)."""
    run = await session.get(ResearchRun, run_id)
    if run is None:
        raise NotFoundError(f"research run {run_id} not found")
    sources = list(
        (
            await session.execute(
                select(ResearchSource)
                .where(ResearchSource.research_run_id == run_id, ResearchSource.status == "read")
                .order_by(ResearchSource.retrieved_at, ResearchSource.id)
            )
        ).scalars()
    )
    claims = list(
        (
            await session.execute(
                select(ResearchClaim).where(ResearchClaim.research_run_id == run_id).order_by(ResearchClaim.created_at, ResearchClaim.id)
            )
        ).scalars()
    )
    links: dict[uuid.UUID, list[str]] = {}
    if claims:
        rows = await session.execute(select(ResearchClaimSource).where(ResearchClaimSource.claim_id.in_([c.id for c in claims])))
        for link in rows.scalars():
            links.setdefault(link.claim_id, []).append(str(link.source_id))
    status = run.status if run.status in {"completed", "partial", "failed"} else "failed"
    return ResearchContract(
        question=run.question,
        queries=[str(q) for q in run.queries],
        sources=[
            SourceRecord(
                source_id=str(s.id),
                title=s.title or s.url,
                url=s.url,
                domain=s.domain,
                retrieved_at=s.retrieved_at,
                published_at=s.published_at,
                source_type=s.source_type if s.source_type in _SOURCE_TYPES else "unknown",  # type: ignore[arg-type]
                authority_score=min(max(s.authority_score, 0.0), 1.0),
                relevance_score=min(max(s.relevance_score, 0.0), 1.0),
                content_hash=s.content_hash,
            )
            for s in sources
        ],
        claims=[
            Claim(
                claim=c.claim,
                source_ids=sorted(links.get(c.id, [])) or ["unknown"],
                confidence=min(max(c.confidence, 0.0), 1.0),
                used_for_decision=c.used_for_decision,
            )
            for c in claims
        ],
        contradictions=[[int(i) for i in g] for g in run.contradictions if isinstance(g, list)],
        synthesis=run.synthesis or "",
        status=status,  # type: ignore[arg-type]
    )


_SOURCE_TYPES = frozenset({"official_docs", "official_repo", "standard", "vendor", "secondary", "forum", "unknown"})
