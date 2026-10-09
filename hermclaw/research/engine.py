"""Research engine (Bauplan §23, P12): question → queries → search → fetch → extract → source records → claims →
source links → freshness → contradiction check → synthesis → decision linkage. Every step is persisted and observable.

Events (``source_type="research"``, ``source_id`` = research run id) carry UI-ready German texts:

* ``research.started``        "Research startet: …"
* ``research.query.started``  "Research sucht: …"                      (one per planned query)
* ``research.source.read``    "Research liest: <title> (<domain>)"     (one per candidate; failed/skipped too)
* ``research.claim.created``  "Quelle verwendet für: <claim>"          (one per claim, with its sources)
* ``research.decision.linked`` "Quellen verwendet für: <decision_ref>"  (claims used by the synthesis/decision)
* ``research.finished``       "Research abgeschlossen: …"              (status, all sources incl. use, contradictions)

Partial failures (a query, a fetch, a model call) never abort the run; they are recorded (failed sources with
status ``failed``) and reflected in the final status ``completed`` / ``partial`` / ``failed``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.research import Claim, ResearchContract, SourceRecord
from hermclaw.core.config import HermclawConfig, ModelsConfig, ResearchPolicy, get_config
from hermclaw.core.errors import ValidationFailed
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext, ChatModel
from hermclaw.persistence.models import ResearchRun
from hermclaw.research import store
from hermclaw.research.claims import ClaimDraft, ClaimExtractor, MergedClaim, SourceScore, merge_claims
from hermclaw.research.contradictions import ContradictionConfirmer, ContradictionReport, LlmContradictionConfirmer, detect_contradictions
from hermclaw.research.errors import SearchDisabled, SearchJsonDisabled
from hermclaw.research.extract import extract_document
from hermclaw.research.fetch import Fetcher, HttpFetcher
from hermclaw.research.planner import PlannedQueries, QueryPlanner
from hermclaw.research.search import (
    DisabledSearchProvider,
    SearchProvider,
    SearchResult,
    SearxngSearchProvider,
    merge_results,
)
from hermclaw.research.sources import (
    Freshness,
    assess_freshness,
    classify_source,
    host_of,
    prior_score,
    question_terms,
    relevance_score,
)
from hermclaw.research.synth import ModelCallParams, SynthesisClaim, SynthesisOutcome, SynthesisThresholds, Synthesizer
from hermclaw.research.text import clip, normalize_ws

log = get_logger(__name__)

SOURCE_TYPE = "research"
RESEARCH_DECISION_LINKED = "research.decision.linked"  # proposed for EventType (shared change)
CONTRADICTION_PENALTY = 0.8  # confidence multiplier for non-preferred claims of a contradiction group


@dataclass(frozen=True)
class ResearchSettings:
    results_per_query: int = 8
    max_sources_per_domain: int = 3
    fetch_concurrency: int = 4
    claim_concurrency: int = 2
    max_claims_per_source: int = 6
    max_claims_total: int = 40
    max_claim_input_chars: int = 12_000
    min_text_chars: int = 120
    excerpt_chars: int = 1500
    fresh_days: int = 365
    stale_days: int = 1095
    confirm_contradictions: bool = False
    thresholds: SynthesisThresholds = field(default_factory=SynthesisThresholds)


@dataclass(frozen=True)
class ResearchModels:
    """Model call parameters: ``fast`` (role fast, Qwen3 8B) and ``deep`` (role planner, Gemma)."""

    fast: ModelCallParams
    deep: ModelCallParams

    @classmethod
    def from_config(cls, models: ModelsConfig, *, fast_role: str = "fast", deep_role: str = "planner") -> ResearchModels:
        def params(role: str) -> ModelCallParams:
            p = models.by_role(role)
            return ModelCallParams(p.alias, p.max_output_tokens, p.temperature, float(p.timeout_seconds))

        return cls(fast=params(fast_role), deep=params(deep_role))


@dataclass
class _ReadSource:
    position: int  # rank among selected candidates (deterministic order)
    db_id: uuid.UUID
    url: str
    title: str
    domain: str
    source_type: str
    authority: float
    relevance: float
    freshness: Freshness
    published_at: datetime | None
    freshness_date: datetime | None
    retrieved_at: datetime
    content_hash: str
    text: str

    def label(self) -> str:
        date = self.freshness_date.date().isoformat() if self.freshness_date else "undated"
        return f"{self.domain} ({self.source_type}, {date})"


@dataclass
class _RunState:
    run_id: uuid.UUID
    question: str
    job_id: uuid.UUID | None
    step_id: uuid.UUID | None
    started: float
    errors: list[dict[str, Any]] = field(default_factory=list)
    query_log: list[dict[str, Any]] = field(default_factory=list)
    failed_sources: int = 0
    skipped_sources: int = 0
    seen_hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class ResearchOutcome:
    run_id: uuid.UUID
    contract: ResearchContract
    claim_ids: list[uuid.UUID]
    synthesis: SynthesisOutcome
    planned: PlannedQueries
    contradictions: ContradictionReport
    errors: list[dict[str, Any]]


class ResearchEngine:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        chat: ChatModel,
        search: SearchProvider,
        fetcher: Fetcher,
        policy: ResearchPolicy,
        *,
        models: ResearchModels | None = None,
        settings: ResearchSettings | None = None,
        confirmer: ContradictionConfirmer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.chat = chat
        self.search = search
        self.fetcher = fetcher
        self.policy = policy
        self.models = models or ResearchModels.from_config(get_config().models)
        self.settings = settings or ResearchSettings()
        self.confirmer = confirmer
        self.clock = clock or (lambda: datetime.now(UTC))
        fast = self.models.fast
        self.planner = QueryPlanner(
            chat,
            fast.alias,
            max_queries=policy.max_queries,
            max_tokens=min(fast.max_tokens or 400, 400),
            temperature=fast.temperature,
            timeout_seconds=fast.timeout_seconds,
        )
        self.extractor = ClaimExtractor(
            chat,
            fast.alias,
            max_claims=self.settings.max_claims_per_source,
            max_input_chars=self.settings.max_claim_input_chars,
            max_tokens=min(fast.max_tokens or 900, 900),
            temperature=0.1,
            timeout_seconds=fast.timeout_seconds,
        )
        self.synthesizer = Synthesizer(chat, fast=fast, deep=self.models.deep, thresholds=self.settings.thresholds)

    async def aclose(self) -> None:
        for obj in (self.search, self.fetcher):
            closer = getattr(obj, "aclose", None)
            if closer is not None:
                with suppress(Exception):
                    await closer()

    # ------------------------------------------------------------------------------------------- plumbing
    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session, session.begin():
            yield session

    async def _event(
        self,
        session: AsyncSession,
        st: _RunState,
        event_type: str,
        payload: dict[str, Any],
        *,
        severity: Severity = Severity.info,
        duration_ms: int | None = None,
    ) -> None:
        await append_event(
            session,
            event_type,
            source_type=SOURCE_TYPE,
            source_id=str(st.run_id),
            job_id=st.job_id,
            step_id=st.step_id,
            severity=severity,
            payload={"research_run_id": str(st.run_id), **payload},
            duration_ms=duration_ms,
        )

    def _error(self, st: _RunState, stage: str, exc: BaseException, **extra: Any) -> str:
        code = str(getattr(exc, "code", type(exc).__name__))
        message = DEFAULT_REDACTOR.text(str(getattr(exc, "message", None) or exc))[:300]
        st.errors.append({"stage": stage, "code": code, "message": message, **extra})
        return code

    # ------------------------------------------------------------------------------------------- public API
    async def run(
        self,
        question: str,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        deep: bool = False,
        *,
        decision_ref: str | None = None,
    ) -> ResearchContract:
        return (await self.run_detailed(question, job_id=job_id, step_id=step_id, deep=deep, decision_ref=decision_ref)).contract

    async def answer_for_worker(self, question: str, *, job_id: uuid.UUID | None = None, step_id: uuid.UUID | None = None) -> str:
        """Adapter for ``ToolCallbacks.on_research``: run research and return a compact cited summary."""
        return format_for_worker(await self.run(question, job_id=job_id, step_id=step_id))

    async def link_decision(self, run_id: uuid.UUID, claim_indices: Sequence[int], decision_ref: str) -> int:
        """Mark claims (0-based indices of the run's claim list) as used for ``decision_ref``; emits an event."""
        if not decision_ref.strip():
            raise ValidationFailed("decision_ref must not be empty")
        async with self._tx() as session:
            ids = await store.claim_ids_in_order(session, run_id)
            bad = [i for i in claim_indices if i < 0 or i >= len(ids)]
            if bad:
                raise ValidationFailed(f"unknown claim indices {bad} (run has {len(ids)} claims)")
            chosen = [ids[i] for i in dict.fromkeys(claim_indices)]
            count = await store.mark_claims_used(session, chosen, decision_ref)
            run = await session.get(ResearchRun, run_id)
            st = _RunState(run_id, run.question if run else "", run.job_id if run else None, run.step_id if run else None, time.monotonic())
            await self._event(
                session,
                st,
                RESEARCH_DECISION_LINKED,
                {
                    "text": f"Quellen verwendet für: {clip(decision_ref, 160)}",
                    "decision_ref": decision_ref,
                    "claims": [i + 1 for i in dict.fromkeys(claim_indices)],
                },
            )
        return count

    async def run_detailed(
        self,
        question: str,
        *,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        deep: bool = False,
        decision_ref: str | None = None,
    ) -> ResearchOutcome:
        q = normalize_ws(DEFAULT_REDACTOR.text(question or ""))
        if len(q) < 3:
            raise ValidationFailed("research question is empty")
        q = q[:2000]
        st = _RunState(uuid.uuid4(), q, job_id, step_id, time.monotonic())
        async with self._tx() as session:
            await store.create_run(session, question=q, job_id=job_id, step_id=step_id, run_id=st.run_id)
            await self._event(
                session,
                st,
                EventType.RESEARCH_STARTED,
                {"text": f"Research startet: {clip(q, 200)}", "question": q, "deep": deep, "provider": getattr(self.search, "name", "")},
            )
        try:
            return await self._pipeline(st, deep=deep, decision_ref=decision_ref)
        except BaseException as exc:
            code = self._error(st, "run", exc)
            log.error("research run aborted", extra={"research_run_id": str(st.run_id), "error_code": code})
            with suppress(Exception):
                await asyncio.shield(self._abort(st, code))
            raise

    async def _abort(self, st: _RunState, code: str) -> None:
        async with self._tx() as session:
            await store.finish_run(session, st.run_id, status="failed", synthesis=None, contradictions=[], model_alias=None)
            await self._event(
                session,
                st,
                EventType.RESEARCH_FINISHED,
                {"text": f"Research abgebrochen ({code})", "status": "failed", "errors": st.errors[-5:]},
                severity=Severity.error,
                duration_ms=int((time.monotonic() - st.started) * 1000),
            )

    # ------------------------------------------------------------------------------------------- pipeline
    async def _pipeline(self, st: _RunState, *, deep: bool, decision_ref: str | None) -> ResearchOutcome:
        ctx_base = {"job_id": st.job_id, "step_id": st.step_id}
        # 12.1 query planning
        planned = await self.planner.plan(st.question, ctx=CallContext(purpose="research_queries", **ctx_base))
        if planned.source == "fallback":
            st.errors.append({"stage": "plan", "code": planned.error or "FALLBACK", "message": "deterministic query fallback used"})
        async with self._tx() as session:
            await store.set_queries(session, st.run_id, planned.queries, model_alias=planned.alias if planned.source == "model" else None)
        # 12.2 search (+ §23.4 primary sources first)
        candidates = await self._search(st, planned.queries)
        # 12.3/12.5/12.4/12.8/12.9 fetch, extract, score, persist
        sources = await self._read_sources(st, candidates, planned.queries)
        # 12.6/12.7 claims with source links
        merged = await self._claims(st, sources)
        # 12.10 contradictions
        report = await self._contradictions(st, sources, merged)
        claim_ids = await self._persist_claims(st, sources, merged)
        # 12.11/12.12 synthesis
        synthesis = await self._synthesize(st, sources, merged, report, deep=deep)
        # decision linkage
        ref = decision_ref or f"research_run:{st.run_id}"
        await self._link(st, merged=merged, claim_ids=claim_ids, synthesis=synthesis, decision_ref=ref, sources=sources)
        return await self._finish(
            st, planned=planned, sources=sources, merged=merged, claim_ids=claim_ids, report=report, synthesis=synthesis
        )

    async def _search(self, st: _RunState, queries: Sequence[str]) -> list[SearchResult]:
        per_query: list[list[SearchResult]] = []
        for idx, query in enumerate(queries):
            async with self._tx() as session:
                await self._event(
                    session,
                    st,
                    EventType.RESEARCH_QUERY_STARTED,
                    {"text": f"Research sucht: {clip(query, 200)}", "query": query, "index": idx, "total": len(queries)},
                )
            t0 = time.monotonic()
            try:
                results = await self.search.search(query, limit=self.settings.results_per_query)
            except Exception as exc:
                code = self._error(st, "search", exc, query=query)
                st.query_log.append({"query": query, "results": 0, "error": code})
                if isinstance(exc, SearchJsonDisabled | SearchDisabled):
                    break  # configuration problem – identical for every further query
                continue
            st.query_log.append({"query": query, "results": len(results), "ms": int((time.monotonic() - t0) * 1000)})
            per_query.append(results)
        merged = merge_results(per_query)
        primary = self.policy.primary_domains
        scored = sorted(
            enumerate(merged),
            key=lambda pair: (-prior_score(pair[1].url, pair[1].rank, query_hits=len(pair[1].queries), primary_domains=primary), pair[0]),
        )
        selected: list[SearchResult] = []
        per_domain: dict[str, int] = {}
        for _, res in scored:
            domain = host_of(res.url).removeprefix("www.")
            if per_domain.get(domain, 0) >= self.settings.max_sources_per_domain:
                continue
            per_domain[domain] = per_domain.get(domain, 0) + 1
            selected.append(res)
            if len(selected) >= self.policy.max_sources:
                break
        return selected

    async def _read_sources(self, st: _RunState, candidates: Sequence[SearchResult], queries: Sequence[str]) -> list[_ReadSource]:
        terms = question_terms(st.question, queries)
        sem = asyncio.Semaphore(max(1, self.settings.fetch_concurrency))
        results: list[_ReadSource | None] = [None] * len(candidates)

        async def worker(position: int, cand: SearchResult) -> None:
            async with sem:
                results[position] = await self._read_one(st, position, cand, terms)

        try:
            async with asyncio.TaskGroup() as tg:
                for pos, cand in enumerate(candidates):
                    tg.create_task(worker(pos, cand))
        except BaseExceptionGroup as group:  # surface the root cause, not the group wrapper
            raise group.exceptions[0] from None
        return [r for r in results if r is not None]

    async def _read_one(self, st: _RunState, position: int, cand: SearchResult, terms: Sequence[str]) -> _ReadSource | None:
        domain = host_of(cand.url).removeprefix("www.") or "unknown"
        cls = classify_source(cand.url, self.policy.primary_domains)
        t0 = time.monotonic()
        try:
            fetched = await self.fetcher.fetch(cand.url)
            doc = extract_document(fetched.text, content_type=fetched.content_type, url=fetched.final_url)
        except Exception as exc:
            code = self._error(st, "fetch", exc, url=cand.url)
            st.failed_sources += 1
            row = store.SourceRow(
                url=cand.url,
                domain=domain,
                title=cand.title,
                status="failed",
                error=f"{code}: {st.errors[-1]['message']}",
                source_type=cls.source_type,
                authority_score=cls.authority_score,
                retrieved_at=self.clock(),
            )
            await self._persist_source(
                st, row, text=f"Research konnte {domain} nicht lesen ({code})", severity=Severity.warning, extra={"error_code": code}, t0=t0
            )
            return None
        final_url = fetched.final_url
        if final_url != cand.url:  # a redirect may lead to another site: classify what was actually read
            domain = host_of(final_url).removeprefix("www.") or domain
            cls = classify_source(final_url, self.policy.primary_domains)
        retrieved = self.clock()
        freshness_date = doc.freshness_date or fetched.last_modified or cand.published_hint
        published = doc.published_at or doc.modified_at or fetched.last_modified or cand.published_hint
        fresh = assess_freshness(freshness_date, now=retrieved, fresh_days=self.settings.fresh_days, stale_days=self.settings.stale_days)
        title = doc.title if doc.title and doc.title != fetched.final_url else (cand.title or doc.title)
        skip_reason: str | None = None
        if len(doc.text) < self.settings.min_text_chars:
            skip_reason = "no extractable text"
        elif doc.content_hash in st.seen_hashes:
            skip_reason = f"duplicate content of {st.seen_hashes[doc.content_hash]}"
        else:
            st.seen_hashes[doc.content_hash] = final_url  # check-and-set without await in between: race free
        relevance = relevance_score(terms, title=title, text=doc.text, snippet=cand.snippet) if skip_reason is None else 0.0
        row = store.SourceRow(
            url=final_url,
            domain=domain,
            title=title,
            status="skipped" if skip_reason else "read",
            error=skip_reason,
            published_at=published,
            source_type=cls.source_type,
            authority_score=cls.authority_score,
            relevance_score=relevance,
            content_hash=doc.content_hash,
            excerpt=clip(doc.text, self.settings.excerpt_chars) if doc.text else None,
            retrieved_at=retrieved,
        )
        extra = {
            "freshness": fresh.status,
            "age_days": fresh.age_days,
            "authority_reason": cls.reason,
            "bytes": fetched.bytes_read,
            "truncated": fetched.truncated,
            "queries": list(cand.queries),
        }
        if skip_reason:
            st.skipped_sources += 1
            await self._persist_source(st, row, text=f"Research überspringt {domain}: {skip_reason}", extra=extra, t0=t0)
            return None
        db_id = await self._persist_source(st, row, text=f"Research liest: {clip(title, 120)} ({domain})", extra=extra, t0=t0)
        return _ReadSource(
            position=position,
            db_id=db_id,
            url=final_url,
            title=title,
            domain=domain,
            source_type=cls.source_type,
            authority=cls.authority_score,
            relevance=relevance,
            freshness=fresh,
            published_at=published,
            freshness_date=freshness_date,
            retrieved_at=retrieved,
            content_hash=doc.content_hash,
            text=doc.text,
        )

    async def _persist_source(
        self,
        st: _RunState,
        row: store.SourceRow,
        *,
        text: str,
        extra: dict[str, Any],
        t0: float,
        severity: Severity = Severity.info,
    ) -> uuid.UUID:
        async with self._tx() as session:
            src = await store.add_source(session, st.run_id, row)
            await self._event(
                session,
                st,
                EventType.RESEARCH_SOURCE_READ,
                {
                    "text": text,
                    "status": row.status,
                    "source_id": str(src.id),
                    "url": row.url,
                    "title": clip(row.title, 300),
                    "domain": row.domain,
                    "source_type": row.source_type,
                    "authority_score": round(row.authority_score, 3),
                    "relevance_score": round(row.relevance_score, 3),
                    "published_at": row.published_at.isoformat() if row.published_at else None,
                    "content_hash": row.content_hash,
                    "error": row.error,
                    **extra,
                },
                severity=severity,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            return src.id

    async def _claims(self, st: _RunState, sources: Sequence[_ReadSource]) -> list[MergedClaim]:
        sem = asyncio.Semaphore(max(1, self.settings.claim_concurrency))
        ctx = CallContext(purpose="research_claims", job_id=st.job_id, step_id=st.step_id)

        async def one(idx: int, src: _ReadSource) -> list[ClaimDraft]:
            async with sem:
                outcome = await self.extractor.extract(st.question, title=src.title, url=src.url, text=src.text, ctx=ctx)
            if outcome.error:
                st.errors.append({"stage": "claims", "code": outcome.error, "message": f"{outcome.method} claims for {src.domain}"})
            return [ClaimDraft(text, idx, outcome.method) for text in outcome.claims]

        drafts_per_source = await asyncio.gather(*(one(i, s) for i, s in enumerate(sources)))
        drafts = [d for ds in drafts_per_source for d in ds]
        scores = [SourceScore(s.authority, s.relevance, s.freshness.factor) for s in sources]
        return merge_claims(drafts, scores, max_claims=self.settings.max_claims_total)

    async def _contradictions(self, st: _RunState, sources: Sequence[_ReadSource], merged: list[MergedClaim]) -> ContradictionReport:
        def newest(claim: MergedClaim) -> float:
            dates = [sources[i].freshness_date for i in claim.source_indices if sources[i].freshness_date is not None]
            return max(d.timestamp() for d in dates) if dates else 0.0  # type: ignore[union-attr]

        confirmer = self.confirmer
        if confirmer is None and self.settings.confirm_contradictions:
            confirmer = LlmContradictionConfirmer(
                self.chat,
                self.models.fast.alias,
                ctx=CallContext(purpose="research_contradictions", job_id=st.job_id, step_id=st.step_id),
                timeout_seconds=self.models.fast.timeout_seconds,
            )
        report = await detect_contradictions(
            [c.text for c in merged],
            source_sets=[frozenset(c.source_indices) for c in merged],
            scores=[(c.confidence, newest(c)) for c in merged],
            confirmer=confirmer,
        )
        for gidx, group in enumerate(report.groups):
            for idx in group.claims:
                merged[idx].contradiction_group = gidx
                if group.preferred is not None and idx != group.preferred:
                    merged[idx].confidence = round(merged[idx].confidence * CONTRADICTION_PENALTY, 4)
        return report

    async def _persist_claims(self, st: _RunState, sources: Sequence[_ReadSource], merged: Sequence[MergedClaim]) -> list[uuid.UUID]:
        ids: list[uuid.UUID] = []
        if not merged:
            return ids
        base = self.clock()
        async with self._tx() as session:
            for idx, claim in enumerate(merged):
                srcs = [sources[i] for i in claim.source_indices]
                row = await store.add_claim(
                    session,
                    st.run_id,
                    claim=claim.text,
                    confidence=claim.confidence,
                    source_ids=[s.db_id for s in srcs],
                    contradiction_group=claim.contradiction_group,
                    created_at=base + timedelta(microseconds=idx),
                )
                ids.append(row.id)
                await self._event(
                    session,
                    st,
                    EventType.RESEARCH_CLAIM_CREATED,
                    {
                        "text": f"Quelle verwendet für: {clip(claim.text, 160)}",
                        "claim_index": idx + 1,
                        "claim_id": str(row.id),
                        "claim": claim.text,
                        "confidence": claim.confidence,
                        "method": sorted(claim.methods),
                        "contradiction_group": None if claim.contradiction_group is None else claim.contradiction_group + 1,
                        "sources": [
                            {"source_id": str(s.db_id), "title": clip(s.title, 200), "url": s.url, "domain": s.domain} for s in srcs
                        ],
                    },
                )
        return ids

    async def _synthesize(
        self, st: _RunState, sources: Sequence[_ReadSource], merged: Sequence[MergedClaim], report: ContradictionReport, *, deep: bool
    ) -> SynthesisOutcome:
        preferred = {g.preferred for g in report.groups if g.preferred is not None}
        claims = [
            SynthesisClaim(
                index=i,
                text=c.text,
                confidence=c.confidence,
                source_labels=tuple(sources[s].label() for s in c.source_indices),
                contradiction_group=c.contradiction_group,
                preferred=i in preferred,
            )
            for i, c in enumerate(merged)
        ]
        reasons = [r for g in report.groups for r in g.reasons]
        outcome = await self.synthesizer.synthesize(
            st.question,
            claims,
            contradictions=reasons,
            deep=deep,
            n_sources=len(sources),
            ctx=CallContext(purpose="research_synthesis", job_id=st.job_id, step_id=st.step_id),
        )
        for err in outcome.errors:
            st.errors.append({"stage": "synthesis", "code": "SYNTHESIS_ISSUE", "message": clip(err, 300)})
        return outcome

    async def _link(
        self,
        st: _RunState,
        *,
        merged: Sequence[MergedClaim],
        claim_ids: Sequence[uuid.UUID],
        synthesis: SynthesisOutcome,
        decision_ref: str,
        sources: Sequence[_ReadSource],
    ) -> None:
        used = [i for i in synthesis.used_claims if 0 <= i < len(claim_ids)]
        if not used:
            return
        async with self._tx() as session:
            await store.mark_claims_used(session, [claim_ids[i] for i in used], decision_ref)
            used_sources = sorted({s for i in used for s in merged[i].source_indices})
            await self._event(
                session,
                st,
                RESEARCH_DECISION_LINKED,
                {
                    "text": f"Quellen verwendet für: {clip(decision_ref, 160)}",
                    "decision_ref": decision_ref,
                    "claims": [i + 1 for i in used],
                    "sources": [
                        {"source_id": str(sources[s].db_id), "url": sources[s].url, "domain": sources[s].domain} for s in used_sources
                    ],
                },
            )

    async def _finish(
        self,
        st: _RunState,
        *,
        planned: PlannedQueries,
        sources: Sequence[_ReadSource],
        merged: Sequence[MergedClaim],
        claim_ids: Sequence[uuid.UUID],
        report: ContradictionReport,
        synthesis: SynthesisOutcome,
    ) -> ResearchOutcome:
        used = set(synthesis.used_claims)
        if not sources or not merged:
            status = "failed"
        elif st.failed_sources or any(q.get("error") for q in st.query_log) or synthesis.mode == "fallback":
            status = "partial"
        else:
            status = "completed"
        text = synthesis.render() if merged else ""
        contradictions = report.as_index_lists()
        async with self._tx() as session:
            await store.finish_run(
                session, st.run_id, status=status, synthesis=text or None, contradictions=contradictions, model_alias=synthesis.alias
            )
            used_for: dict[int, list[int]] = {}
            for i, claim in enumerate(merged):
                for s in claim.source_indices:
                    used_for.setdefault(s, []).append(i + 1)
            await self._event(
                session,
                st,
                EventType.RESEARCH_FINISHED,
                {
                    "text": _finish_text(status, len(sources), len(merged), len(report.groups)),
                    "status": status,
                    "queries": st.query_log,
                    "query_source": planned.source,
                    "sources_read": len(sources),
                    "sources_failed": st.failed_sources,
                    "sources_skipped": st.skipped_sources,
                    "claims": len(merged),
                    "sources_used": [
                        {
                            "source_id": str(s.db_id),
                            "title": clip(s.title, 200),
                            "url": s.url,
                            "domain": s.domain,
                            "source_type": s.source_type,
                            "freshness": s.freshness.status,
                            "used_for_claims": used_for.get(i, []),
                            "used_in_synthesis": any(c - 1 in used for c in used_for.get(i, [])),
                        }
                        for i, s in enumerate(sources)
                    ],
                    "contradictions": [
                        {
                            "claims": [c + 1 for c in g.claims],
                            "reasons": g.reasons,
                            "preferred": None if g.preferred is None else g.preferred + 1,
                        }
                        for g in report.groups
                    ],
                    "synthesis_mode": synthesis.mode,
                    "synthesis_requested_mode": synthesis.requested_mode,
                    "synthesis_alias": synthesis.alias,
                    "synthesis_rejected_sentences": synthesis.rejected_sentences,
                    "synthesis": clip(text, 4000) if text else "",
                    "errors": st.errors[:30],
                },
                severity=Severity.info if status == "completed" else Severity.warning,
                duration_ms=int((time.monotonic() - st.started) * 1000),
            )
        contract = ResearchContract(
            question=st.question,
            queries=list(planned.queries),
            sources=[
                SourceRecord(
                    source_id=str(s.db_id),
                    title=s.title or s.url,
                    url=s.url,
                    domain=s.domain,
                    retrieved_at=s.retrieved_at,
                    published_at=s.published_at,
                    source_type=s.source_type,  # type: ignore[arg-type]
                    authority_score=s.authority,
                    relevance_score=s.relevance,
                    content_hash=s.content_hash,
                )
                for s in sources
            ],
            claims=[
                Claim(
                    claim=c.text,
                    source_ids=[str(sources[i].db_id) for i in c.source_indices],
                    confidence=c.confidence,
                    used_for_decision=i in used,
                )
                for i, c in enumerate(merged)
            ],
            contradictions=contradictions,
            synthesis=text,
            status=status,  # type: ignore[arg-type]
        )
        return ResearchOutcome(st.run_id, contract, list(claim_ids), synthesis, planned, report, st.errors)


def _finish_text(status: str, n_sources: int, n_claims: int, n_conflicts: int) -> str:
    if status == "failed":
        return f"Research fehlgeschlagen: {n_sources} Quellen gelesen, {n_claims} Claims"
    suffix = f", {n_conflicts} Widersprüche" if n_conflicts else ""
    prefix = "Research abgeschlossen" if status == "completed" else "Research teilweise abgeschlossen"
    return f"{prefix}: {n_sources} Quellen, {n_claims} Claims{suffix}"


def format_for_worker(contract: ResearchContract, *, max_chars: int = 6000) -> str:
    """Compact, cited text for a coding worker (tool ``request_research``)."""
    if contract.status == "failed" and not contract.claims:
        return f"Research failed: no usable sources found for '{clip(contract.question, 200)}'."
    by_id = {s.source_id: n + 1 for n, s in enumerate(contract.sources)}
    lines = [f"Research ({contract.status}): {contract.question}", "", contract.synthesis.strip(), "", "Claims:"]
    for n, claim in enumerate(contract.claims, start=1):
        refs = ",".join(f"S{by_id[s]}" for s in claim.source_ids if s in by_id)
        lines.append(f"[{n}] {claim.claim} ({refs}; confidence {claim.confidence:.2f})")
    lines += ["", "Sources:"]
    for n, src in enumerate(contract.sources, start=1):
        lines.append(f"S{n} {src.title} – {src.url} ({src.source_type})")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def build_research_engine(
    sessionmaker: async_sessionmaker[AsyncSession],
    chat: ChatModel,
    config: HermclawConfig | None = None,
    *,
    settings: ResearchSettings | None = None,
    **fetcher_overrides: object,
) -> ResearchEngine:
    """Production wiring from ``policies.research`` and the model profiles (fast + planner roles)."""
    cfg = config or get_config()
    policy = cfg.policies.research
    search: SearchProvider
    if policy.search_provider == "searxng":
        search = SearxngSearchProvider(
            policy.searxng_url, timeout_seconds=float(policy.fetch_timeout_seconds), user_agent=policy.user_agent
        )
    else:
        search = DisabledSearchProvider()
    fetcher = HttpFetcher.from_policy(policy, **fetcher_overrides)
    return ResearchEngine(sessionmaker, chat, search, fetcher, policy, models=ResearchModels.from_config(cfg.models), settings=settings)


ResearchCallback = Callable[[str], Awaitable[str]]


def research_callback(engine: ResearchEngine, *, job_id: uuid.UUID | None = None, step_id: uuid.UUID | None = None) -> ResearchCallback:
    async def on_research(question: str) -> str:
        return await engine.answer_for_worker(question, job_id=job_id, step_id=step_id)

    return on_research


__all__ = [
    "RESEARCH_DECISION_LINKED",
    "ResearchEngine",
    "ResearchModels",
    "ResearchOutcome",
    "ResearchSettings",
    "build_research_engine",
    "format_for_worker",
    "research_callback",
]
