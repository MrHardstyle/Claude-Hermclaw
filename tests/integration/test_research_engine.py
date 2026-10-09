"""P12 end-to-end: ResearchEngine against PostgreSQL, a local fixture HTTP server (official docs page, forum page with a
conflicting version number, blog page with a JSON-LD date), StaticSearchProvider / fake SearXNG and a scripted
ChatModel (test code only). Asserts persistence, scores, claim links, contradictions, cited synthesis, decision
linkage, events and the fallback paths."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.config import ResearchPolicy, load_config
from hermclaw.core.errors import ValidationFailed
from hermclaw.models.protocols import ChatMessage
from hermclaw.persistence.models import Event, Job, ResearchClaim, ResearchClaimSource, ResearchRun, ResearchSource
from hermclaw.research import store
from hermclaw.research.engine import (
    RESEARCH_DECISION_LINKED,
    ResearchEngine,
    ResearchModels,
    ResearchSettings,
    build_research_engine,
    format_for_worker,
    research_callback,
)
from hermclaw.research.errors import SearchUnavailable
from hermclaw.research.fetch import HttpFetcher
from hermclaw.research.search import DisabledSearchProvider, SearchResult, SearxngSearchProvider, StaticSearchProvider
from hermclaw.research.synth import ModelCallParams
from tests.integration.test_research_support import FixtureServer, Route, ScriptedChat, resolver_for

pytestmark = pytest.mark.integration

QUESTION = "Which Python version does Toolkit 4.2 require?"
FAST = ModelCallParams("fast-router", 1024, 0.1, 30.0)
DEEP = ModelCallParams("planner-gemma", 4096, 0.2, 60.0)
MODELS = ResearchModels(fast=FAST, deep=DEEP)
HOSTS = {
    "docs.toolkit.test": "127.0.0.1",
    "forum.toolkit.test": "127.0.0.1",
    "blog.example.test": "127.0.0.1",
    "mirror.example.test": "127.0.0.1",
    "broken.example.test": "127.0.0.1",
    "files.example.test": "127.0.0.1",
    "tiny.example.test": "127.0.0.1",
    "intranet.corp.test": "10.0.0.7",  # private address – the SSRF guard must refuse it
}

DOCS_PAGE = """<!doctype html><html lang="en"><head><title>Installing Toolkit 4.2 - Toolkit documentation</title>
<meta property="article:modified_time" content="2026-09-01T10:00:00Z"></head><body>
<nav><a href="/">Home</a> <a href="/api">API reference</a> Python 2 legacy docs</nav>
<main><h1>Installing Toolkit</h1>
<p>Toolkit 4.2 requires Python 3.10 or newer. Install Toolkit with the command pip install toolkit.</p>
<p>The toolkit configuration file is named toolkit.toml and lives in the project root directory.</p>
<p>Older interpreters are rejected at import time with a clear error message.</p></main>
<footer>Copyright Toolkit authors. Python 3.6 archive.</footer></body></html>"""

FORUM_PAGE = """<!doctype html><html><head><title>Python version for toolkit? - Toolkit forum</title></head><body>
<header>Toolkit community forum</header>
<article><p>Posted <time datetime="2021-03-01T08:00:00Z">March 2021</time></p>
<p>Toolkit 4.2 requires Python 3.8 or newer according to my tests on several machines.</p>
<p>I installed it on three laptops and everything worked fine for my small python projects.</p></article>
</body></html>"""

BLOG_PAGE = """<!doctype html><html><head><title>What is new in Toolkit 4.2</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "BlogPosting",
"headline": "What is new in Toolkit 4.2", "datePublished": "2026-05-01T09:00:00Z"}</script></head><body>
<article><h1>What is new in Toolkit 4.2</h1>
<p>Toolkit 4.2 ships a new async runner that speeds up builds for large python projects.</p>
<p>The release also improves the python version checks and the error output for configuration problems.</p></article>
</body></html>"""

CLAIMS_BY_HOST = {
    "docs.toolkit.test": [
        "Toolkit 4.2 requires Python 3.10 or newer.",
        "The toolkit configuration file is named toolkit.toml and lives in the project root directory.",
    ],
    "forum.toolkit.test": ["Toolkit 4.2 requires Python 3.8 or newer."],
    "blog.example.test": ["Toolkit 4.2 ships a new async runner that speeds up builds for large python projects."],
    "mirror.example.test": ["Toolkit 4.2 requires Python 3.10 or newer."],
}
_CLAIM_LINE_RE = re.compile(r"^\[(\d+)\] (.+?)  \(confidence", re.M)


# ----------------------------------------------------------------------------------------------- fixtures / helpers
@pytest.fixture
def server() -> Iterator[FixtureServer]:
    with FixtureServer() as srv:
        srv.add("/install", Route(DOCS_PAGE), host="docs.toolkit.test")
        srv.add("/t/42", Route(FORUM_PAGE), host="forum.toolkit.test")
        srv.add("/posts/toolkit-4-2", Route(BLOG_PAGE), host="blog.example.test")
        srv.add("/copy/install", Route(DOCS_PAGE.replace("<title>", "<title>Mirror: ")), host="mirror.example.test")
        srv.add("/down", Route("boom", status=500, content_type="text/plain"), host="broken.example.test")
        srv.add("/manual.pdf", Route(b"%PDF-1.7", content_type="application/pdf"), host="files.example.test")
        srv.add("/", Route("<html><body><p>Toolkit.</p></body></html>"), host="tiny.example.test")
        srv.add("/", Route("<html><body><p>internal</p></body></html>"), host="intranet.corp.test")
        yield srv


def url(server: FixtureServer, host: str, path: str) -> str:
    return f"http://{host}:{server.port}{path}"


def fetcher() -> HttpFetcher:
    return HttpFetcher(timeout_seconds=5, allowed_networks=["127.0.0.0/8"], resolver=resolver_for(HOSTS), user_agent="HermclawTest/1.0")


def policy(**kw: Any) -> ResearchPolicy:
    base: dict[str, Any] = {"max_queries": 3, "max_sources": 8, "primary_domains": ["docs.toolkit.test"], "fetch_timeout_seconds": 5}
    base.update(kw)
    return ResearchPolicy(**base)


def source_host(messages: list[ChatMessage]) -> str:
    m = re.search(r"Source URL: https?://([^:/\s]+)", messages[-1].content)
    assert m, "claim prompt must name the source URL"
    return m.group(1)


def claims_from_prompt(messages: list[ChatMessage]) -> dict[int, str]:
    return {int(n): text for n, text in _CLAIM_LINE_RE.findall(messages[-1].content)}


def cited_synthesis(alias: str, messages: list[ChatMessage], _n: int) -> dict[str, Any]:
    claims = claims_from_prompt(messages)
    py = [n for n, t in claims.items() if "requires Python" in t]
    rest = [n for n in claims if n not in py]
    answer = "The sources disagree on the minimum Python version for Toolkit 4.2 " + "".join(f"[{n}]" for n in py) + "."
    answer += " The official documentation is newer and more authoritative " + (f"[{py[0]}]." if py else "[1].")
    return {
        "answer": answer,
        "key_points": [f"{claims[n].rstrip('.')} [{n}]" for n in rest],
        "used_claims": sorted(claims),
        "open_questions": [],
    }


def happy_chat() -> ScriptedChat:
    return ScriptedChat(
        handlers={
            "research_queries": lambda a, m, n: {"queries": ["toolkit 4.2 python version requirement", "toolkit 4.2 install python"]},
            "research_claims": lambda a, m, n: {"claims": CLAIMS_BY_HOST.get(source_host(m), [])},
            "research_synthesis": cited_synthesis,
        }
    )


def engine_for(
    sm: async_sessionmaker[AsyncSession], chat: ScriptedChat, search: Any, *, pol: ResearchPolicy | None = None, **settings: Any
) -> ResearchEngine:
    return ResearchEngine(sm, chat, search, fetcher(), pol or policy(), models=MODELS, settings=ResearchSettings(**settings))


async def events_of(sm: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> list[Event]:
    async with sm() as s:
        rows = await s.execute(select(Event).where(Event.source_id == str(run_id)).order_by(Event.sequence))
        return list(rows.scalars())


async def sources_of(sm: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> list[ResearchSource]:
    async with sm() as s:
        rows = await s.execute(select(ResearchSource).where(ResearchSource.research_run_id == run_id))
        return list(rows.scalars())


async def claims_of(sm: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> list[tuple[ResearchClaim, set[uuid.UUID]]]:
    async with sm() as s:
        claims = list(
            (
                await s.execute(
                    select(ResearchClaim).where(ResearchClaim.research_run_id == run_id).order_by(ResearchClaim.created_at, ResearchClaim.id)
                )
            ).scalars()
        )
        out = []
        for c in claims:
            links = await s.execute(select(ResearchClaimSource.source_id).where(ResearchClaimSource.claim_id == c.id))
            out.append((c, set(links.scalars())))
        return out


def static_search(server: FixtureServer) -> StaticSearchProvider:
    docs = url(server, "docs.toolkit.test", "/install")
    forum = url(server, "forum.toolkit.test", "/t/42")
    blog = url(server, "blog.example.test", "/posts/toolkit-4-2")
    return StaticSearchProvider(
        {
            "toolkit 4.2 python version requirement": [forum, docs + "?utm_source=searx", SearchResult(blog, title="Blog")],
            "toolkit 4.2 install python": [docs, blog],
        }
    )


# ----------------------------------------------------------------------------------------------- happy path
async def test_full_pipeline_persists_scored_sources_linked_claims_contradiction_and_cited_synthesis(
    sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer
) -> None:
    chat = happy_chat()
    search = static_search(server)
    engine = engine_for(sessionmaker, chat, search)
    try:
        outcome = await engine.run_detailed(QUESTION, decision_ref="decision:python-floor")
    finally:
        await engine.aclose()
    contract = outcome.contract
    run_id = outcome.run_id

    # 12.1 queries from the fast model, capped by policy
    assert contract.queries == ["toolkit 4.2 python version requirement", "toolkit 4.2 install python"]
    assert chat.aliases("research_queries") == ["fast-router"] and outcome.planned.source == "model"
    assert search.calls == contract.queries

    # 12.2/12.3/12.4/12.9 deduplicated sources, primary source first, scores persisted
    assert contract.status == "completed"
    assert [s.domain for s in contract.sources] == ["docs.toolkit.test", "blog.example.test", "forum.toolkit.test"]
    by_domain = {s.domain: s for s in contract.sources}
    assert by_domain["docs.toolkit.test"].source_type == "official_docs" and by_domain["docs.toolkit.test"].authority_score == 0.95
    assert by_domain["forum.toolkit.test"].source_type == "forum" and by_domain["forum.toolkit.test"].authority_score == 0.3
    assert by_domain["blog.example.test"].source_type == "secondary"
    assert all(0 < s.relevance_score <= 1 for s in contract.sources)
    assert by_domain["docs.toolkit.test"].url.endswith("/install")  # tracking parameter removed, duplicates merged
    rows = await sources_of(sessionmaker, run_id)
    assert len(rows) == 3 and {r.status for r in rows} == {"read"}
    db = {r.domain: r for r in rows}
    for r in rows:
        assert len(r.content_hash) == 64 and r.excerpt and r.relevance_score > 0 and r.authority_score > 0
    # 12.5 boilerplate dropped from the excerpt; 12.8 dates from meta / JSON-LD / <time>
    assert "Python 3.10" in (db["docs.toolkit.test"].excerpt or "") and "legacy" not in (db["docs.toolkit.test"].excerpt or "")
    assert db["docs.toolkit.test"].published_at is not None and db["docs.toolkit.test"].published_at.isoformat().startswith("2026-09-01")
    assert db["blog.example.test"].published_at is not None and db["blog.example.test"].published_at.isoformat().startswith("2026-05-01")
    assert db["forum.toolkit.test"].published_at is not None and db["forum.toolkit.test"].published_at.year == 2021
    assert server.requests and all(r["ua"] == "HermclawTest/1.0" for r in server.requests)

    # 12.6/12.7 claims with source links
    texts = [c.claim for c in contract.claims]
    assert texts == [
        "Toolkit 4.2 requires Python 3.10 or newer.",
        "The toolkit configuration file is named toolkit.toml and lives in the project root directory.",
        "Toolkit 4.2 ships a new async runner that speeds up builds for large python projects.",
        "Toolkit 4.2 requires Python 3.8 or newer.",
    ]
    db_claims = await claims_of(sessionmaker, run_id)
    assert [c.claim for c, _ in db_claims] == texts
    src_id = {s.domain: uuid.UUID(s.source_id) for s in contract.sources}
    assert db_claims[0][1] == {src_id["docs.toolkit.test"]} and db_claims[3][1] == {src_id["forum.toolkit.test"]}
    assert [c.source_ids for c in contract.claims][2] == [str(src_id["blog.example.test"])]
    # confidence = authority × relevance × freshness: the official claim beats the forum claim
    assert contract.claims[0].confidence > contract.claims[3].confidence > 0

    # 12.10 contradiction docs (3.10) vs forum (3.8), preferred = official docs
    assert contract.contradictions == [[0, 3]]
    assert db_claims[0][0].contradiction_group == 0 and db_claims[3][0].contradiction_group == 0
    assert db_claims[1][0].contradiction_group is None
    assert outcome.contradictions.groups[0].preferred == 0

    # 12.11/12.12 contradictions make the synthesis "deep" (planner role = Gemma); synthesis cites claims
    assert chat.aliases("research_synthesis") == ["planner-gemma"]
    assert outcome.synthesis.mode == "deep" and outcome.synthesis.alias == "planner-gemma"
    assert "[1][4]" in contract.synthesis and "Key points:" in contract.synthesis
    async with sessionmaker() as s:
        run = await s.get(ResearchRun, run_id)
    assert run is not None and run.status == "completed" and run.synthesis == contract.synthesis
    assert run.contradictions == [[0, 3]] and run.queries == contract.queries and run.finished_at is not None
    assert run.model_alias == "planner-gemma"

    # decision linkage: every cited claim is marked with the decision reference
    assert all(c.used_for_decision for c in contract.claims)
    assert all(c.used_for_decision and c.decision_ref == "decision:python-floor" for c, _ in db_claims)

    # 12.13 observable steps with UI texts
    events = await events_of(sessionmaker, run_id)
    types = [e.event_type for e in events]
    assert types[0] == EventType.RESEARCH_STARTED and types[-1] == EventType.RESEARCH_FINISHED
    assert types.count(EventType.RESEARCH_QUERY_STARTED) == 2
    assert types.count(EventType.RESEARCH_SOURCE_READ) == 3
    assert types.count(EventType.RESEARCH_CLAIM_CREATED) == 4
    assert types.count(RESEARCH_DECISION_LINKED) == 1
    assert types.index(EventType.RESEARCH_QUERY_STARTED) < types.index(EventType.RESEARCH_SOURCE_READ) < types.index(
        EventType.RESEARCH_CLAIM_CREATED
    )
    texts_ui = [str(e.payload.get("text", "")) for e in events]
    assert any(t.startswith("Research sucht: toolkit 4.2 python version requirement") for t in texts_ui)
    assert any(t.startswith("Research liest: Installing Toolkit 4.2") for t in texts_ui)
    assert any(t.startswith("Quelle verwendet für: Toolkit 4.2 requires Python 3.10") for t in texts_ui)
    assert all(e.payload["research_run_id"] == str(run_id) for e in events)
    finished = events[-1].payload
    assert finished["status"] == "completed" and finished["sources_read"] == 3 and finished["claims"] == 4
    assert finished["contradictions"][0]["claims"] == [1, 4] and finished["contradictions"][0]["preferred"] == 1
    used = {s["domain"]: s for s in finished["sources_used"]}
    assert used["forum.toolkit.test"]["used_for_claims"] == [4] and used["docs.toolkit.test"]["freshness"] == "fresh"
    assert used["forum.toolkit.test"]["freshness"] == "stale"
    read_ev = next(e for e in events if e.event_type == EventType.RESEARCH_SOURCE_READ and e.payload["domain"] == "docs.toolkit.test")
    assert read_ev.payload["source_type"] == "official_docs" and read_ev.payload["authority_score"] == 0.95
    assert sorted(read_ev.payload["queries"]) == sorted(contract.queries)  # found by both queries

    # persisted contract can be rebuilt
    async with sessionmaker() as s:
        loaded = await store.load_contract(s, run_id)
    assert [c.claim for c in loaded.claims] == texts and loaded.synthesis == contract.synthesis
    assert {s.source_id for s in loaded.sources} == {s.source_id for s in contract.sources}
    assert loaded.contradictions == [[0, 3]] and loaded.status == "completed"


async def test_run_links_job_and_identical_claims_from_two_sources_are_merged(
    sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer
) -> None:
    async with sessionmaker() as s, s.begin():
        job = Job(title="research job", prompt="p", status="cancelled")
        s.add(job)
        await s.flush()
        job_id = job.id
    mirror = url(server, "mirror.example.test", "/copy/install")
    docs = url(server, "docs.toolkit.test", "/install")
    chat = happy_chat()
    # the mirror serves the same text under another title; the content hash covers the extracted text only
    search = StaticSearchProvider(default=[docs, mirror])
    engine = engine_for(sessionmaker, chat, search)
    try:
        contract = await engine.run(QUESTION, job_id=job_id)
    finally:
        await engine.aclose()
    # identical extracted text -> the mirror is skipped as duplicate content (no second, unverifiable copy)
    events = [e for e in await _job_events(sessionmaker, job_id) if e.event_type == EventType.RESEARCH_SOURCE_READ]
    statuses = {e.payload["domain"]: (e.payload["status"], e.payload["error"]) for e in events}
    assert statuses["docs.toolkit.test"] == ("read", None)
    assert statuses["mirror.example.test"][0] == "skipped" and "duplicate content" in statuses["mirror.example.test"][1]
    assert [s.domain for s in contract.sources] == ["docs.toolkit.test"]
    assert all(e.job_id == job_id for e in events)
    async with sessionmaker() as s:
        run = (await s.execute(select(ResearchRun).where(ResearchRun.job_id == job_id))).scalar_one()
    assert run.status == "completed"


async def _job_events(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> list[Event]:
    async with sm() as s:
        return list((await s.execute(select(Event).where(Event.job_id == job_id).order_by(Event.sequence))).scalars())


async def test_same_claim_from_two_distinct_documents_is_one_claim_with_two_sources(
    sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer
) -> None:
    other = DOCS_PAGE.replace("Older interpreters are rejected", "Legacy interpreters get refused")
    server.add("/copy/install", Route(other), host="mirror.example.test")
    docs = url(server, "docs.toolkit.test", "/install")
    mirror = url(server, "mirror.example.test", "/copy/install")
    engine = engine_for(sessionmaker, happy_chat(), StaticSearchProvider(default=[docs, mirror]))
    try:
        outcome = await engine.run_detailed(QUESTION)
    finally:
        await engine.aclose()
    contract = outcome.contract
    ids = {s.domain: s.source_id for s in contract.sources}
    python_claim = next(c for c in contract.claims if "Python 3.10" in c.claim)
    assert sorted(python_claim.source_ids) == sorted([ids["docs.toolkit.test"], ids["mirror.example.test"]])
    single = engine_for(sessionmaker, happy_chat(), StaticSearchProvider(default=[docs]))
    try:
        alone = await single.run(QUESTION)
    finally:
        await single.aclose()
    # noisy-OR: a claim confirmed by two sources is more confident than the same claim from one source
    assert python_claim.confidence > next(c for c in alone.claims if "Python 3.10" in c.claim).confidence
    db_claims = await claims_of(sessionmaker, outcome.run_id)
    linked = next(links for c, links in db_claims if "Python 3.10" in c.claim)
    assert linked == {uuid.UUID(ids["docs.toolkit.test"]), uuid.UUID(ids["mirror.example.test"])}


# ----------------------------------------------------------------------------------------------- fallbacks
async def test_invalid_model_output_everywhere_uses_deterministic_fallbacks(
    sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer
) -> None:
    chat = ScriptedChat(
        handlers={
            "research_queries": lambda a, m, n: "this is not json",
            # hallucinated, ungrounded claims (values not in the source) -> dropped -> heuristic sentences
            "research_claims": lambda a, m, n: {"claims": ["Toolkit 9.9 requires Rust 1.70 and a GPU with 48 GB memory."]},
            # uncited prose is rejected (also after the repair round) -> deterministic cited synthesis
            "research_synthesis": lambda a, m, n: {"answer": "Toolkit needs a recent Python interpreter on all platforms.", "used_claims": [1]},
        }
    )
    docs = url(server, "docs.toolkit.test", "/install")
    forum = url(server, "forum.toolkit.test", "/t/42")
    engine = engine_for(sessionmaker, chat, StaticSearchProvider(default=[docs, forum]))
    try:
        outcome = await engine.run_detailed(QUESTION)
    finally:
        await engine.aclose()
    contract = outcome.contract
    assert outcome.planned.source == "fallback" and contract.queries[0] == "Which Python version does Toolkit 4.2 require"
    assert len(contract.queries) <= 3
    # heuristic claims are real sentences of the sources
    assert contract.claims and all("Rust" not in c.claim for c in contract.claims)
    assert any(c.claim.startswith("Toolkit 4.2 requires Python 3.10") for c in contract.claims)
    assert any(c.claim.startswith("Toolkit 4.2 requires Python 3.8") for c in contract.claims)
    assert contract.contradictions  # still detected deterministically
    # deep requested (contradiction) -> planner and fast both fail validation -> deterministic fallback
    assert outcome.synthesis.requested_mode == "deep" and outcome.synthesis.mode == "fallback"
    assert chat.aliases("research_synthesis") == ["planner-gemma", "planner-gemma", "fast-router", "fast-router"]
    assert "Toolkit needs a recent Python interpreter" not in contract.synthesis
    assert re.search(r"\[\d+\]", contract.synthesis) and "Sources disagree" in contract.synthesis
    assert contract.status == "partial"
    assert any(c.used_for_decision for c in contract.claims)
    stages = {e["stage"] for e in outcome.errors}
    assert {"plan", "claims", "synthesis"} <= stages
    events = await events_of(sessionmaker, outcome.run_id)
    claim_events = [e for e in events if e.event_type == EventType.RESEARCH_CLAIM_CREATED]
    assert claim_events and all(e.payload["method"] == ["heuristic"] for e in claim_events)
    assert events[-1].payload["query_source"] == "fallback" and events[-1].payload["synthesis_mode"] == "fallback"


async def test_model_unavailable_still_produces_a_cited_answer(
    sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer
) -> None:
    chat = ScriptedChat()  # every model call fails (ModelOutputInvalid)
    engine = engine_for(sessionmaker, chat, StaticSearchProvider(default=[url(server, "docs.toolkit.test", "/install")]))
    try:
        contract = await engine.run(QUESTION, deep=True)
    finally:
        await engine.aclose()
    assert contract.status == "partial" and contract.claims and "[1]" in contract.synthesis
    assert chat.count("research_queries") == 1 and chat.count("research_claims") == 1


# ----------------------------------------------------------------------------------------------- partial failures
async def test_failed_blocked_unsupported_and_tiny_sources_are_recorded_and_run_continues(
    sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer
) -> None:
    good = url(server, "docs.toolkit.test", "/install")
    bad = [
        url(server, "broken.example.test", "/down"),
        url(server, "files.example.test", "/manual.pdf"),
        url(server, "intranet.corp.test", "/"),
        url(server, "tiny.example.test", "/"),
        "http://unknown-host.example.test/x",
    ]
    search = StaticSearchProvider(
        {"q ok toolkit python": [good, *bad]},
        failures={"q down toolkit python": SearchUnavailable("searx down")},
    )
    chat = happy_chat()
    chat.handlers["research_queries"] = lambda a, m, n: {"queries": ["q ok toolkit python", "q down toolkit python"]}
    engine = engine_for(sessionmaker, chat, search)
    try:
        outcome = await engine.run_detailed(QUESTION)
    finally:
        await engine.aclose()
    contract = outcome.contract
    assert contract.status == "partial"
    assert [s.domain for s in contract.sources] == ["docs.toolkit.test"]
    rows = {r.domain: r for r in await sources_of(sessionmaker, outcome.run_id)}
    assert rows["docs.toolkit.test"].status == "read"
    assert rows["broken.example.test"].status == "failed" and (rows["broken.example.test"].error or "").startswith("FETCH_HTTP_ERROR")
    assert rows["files.example.test"].status == "failed" and (rows["files.example.test"].error or "").startswith("FETCH_UNSUPPORTED_CONTENT")
    assert rows["intranet.corp.test"].status == "failed" and (rows["intranet.corp.test"].error or "").startswith("FETCH_BLOCKED")
    assert rows["unknown-host.example.test"].status == "failed" and (rows["unknown-host.example.test"].error or "").startswith("FETCH_FAILED")
    assert rows["tiny.example.test"].status == "skipped" and rows["tiny.example.test"].error == "no extractable text"
    # SSRF: the private host never got a request
    assert not any(r["host"] == "intranet.corp.test" for r in server.requests)
    events = await events_of(sessionmaker, outcome.run_id)
    failed = [e for e in events if e.event_type == EventType.RESEARCH_SOURCE_READ and e.payload["status"] == "failed"]
    assert len(failed) == 4 and all(e.severity == "warning" for e in failed)
    assert all(str(e.payload["text"]).startswith("Research konnte") for e in failed)
    finished = events[-1].payload
    assert finished["sources_failed"] == 4 and finished["sources_skipped"] == 1
    assert {"query": "q down toolkit python", "results": 0, "error": "SEARCH_UNAVAILABLE"} in finished["queries"]
    assert events[-1].severity == "warning"


async def test_no_usable_source_marks_run_failed(sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer) -> None:
    engine = engine_for(sessionmaker, happy_chat(), StaticSearchProvider(default=[url(server, "broken.example.test", "/down")]))
    try:
        outcome = await engine.run_detailed(QUESTION)
    finally:
        await engine.aclose()
    assert outcome.contract.status == "failed" and outcome.contract.sources == [] and outcome.contract.synthesis == ""
    assert format_for_worker(outcome.contract).startswith("Research failed")
    async with sessionmaker() as s:
        run = await s.get(ResearchRun, outcome.run_id)
    assert run is not None and run.status == "failed" and run.synthesis is None


async def test_disabled_search_fails_fast(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    engine = engine_for(sessionmaker, happy_chat(), DisabledSearchProvider())
    outcome = await engine.run_detailed(QUESTION)
    await engine.aclose()
    assert outcome.contract.status == "failed"
    assert [e["code"] for e in outcome.errors if e["stage"] == "search"] == ["SEARCH_DISABLED"]  # stopped after query 1


# ----------------------------------------------------------------------------------------------- SearXNG end-to-end
async def test_engine_with_fake_searxng_server(sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer) -> None:
    docs = url(server, "docs.toolkit.test", "/install")
    forum = url(server, "forum.toolkit.test", "/t/42")
    answer = {"query": "x", "results": [{"url": forum, "title": "Forum", "engine": "brave"}, {"url": docs, "title": "Docs"}]}
    server.add("/search", Route(json.dumps(answer), content_type="application/json"))
    search = SearxngSearchProvider(server.url(""), timeout_seconds=5)
    engine = engine_for(sessionmaker, happy_chat(), search)
    try:
        contract = await engine.run(QUESTION)
    finally:
        await engine.aclose()
    searches = [r for r in server.requests if r["path"].startswith("/search?")]
    assert len(searches) == 2 and all("format=json" in r["path"] for r in searches)
    assert {s.domain for s in contract.sources} == {"docs.toolkit.test", "forum.toolkit.test"}
    assert contract.sources[0].domain == "docs.toolkit.test"  # primary source read first despite search rank 2
    assert contract.contradictions == [[0, 2]] and contract.status == "completed"


async def test_searxng_json_disabled_is_reported_once(sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer) -> None:
    server.add("/search", Route("<html>403 Forbidden</html>", status=403))
    search = SearxngSearchProvider(server.url(""), timeout_seconds=5)
    engine = engine_for(sessionmaker, happy_chat(), search)
    try:
        outcome = await engine.run_detailed(QUESTION)
    finally:
        await engine.aclose()
    assert outcome.contract.status == "failed"
    assert len([r for r in server.requests if r["path"].startswith("/search")]) == 1  # config error -> no retry per query
    events = await events_of(sessionmaker, outcome.run_id)
    finished = events[-1].payload
    assert finished["queries"][0]["error"] == "SEARCH_JSON_DISABLED"
    assert any(e["code"] == "SEARCH_JSON_DISABLED" for e in finished["errors"])


# ----------------------------------------------------------------------------------------------- decision API / worker
async def test_link_decision_marks_claims_and_emits_event(sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer) -> None:
    chat = happy_chat()
    # synthesis cites only claim 1, so claim 2 stays unused until an explicit decision links it
    chat.handlers["research_synthesis"] = lambda a, m, n: {"answer": "Toolkit 4.2 requires Python 3.10 or newer [1].", "used_claims": [1]}
    engine = engine_for(sessionmaker, chat, StaticSearchProvider(default=[url(server, "docs.toolkit.test", "/install")]))
    try:
        outcome = await engine.run_detailed(QUESTION)
        assert [c.used_for_decision for c in outcome.contract.claims] == [True, False]
        assert await engine.link_decision(outcome.run_id, [1, 1], "plan:v2:step-3") == 1
        with pytest.raises(ValidationFailed):
            await engine.link_decision(outcome.run_id, [7], "plan:v2:step-3")
        with pytest.raises(ValidationFailed):
            await engine.link_decision(outcome.run_id, [0], "  ")
    finally:
        await engine.aclose()
    db_claims = await claims_of(sessionmaker, outcome.run_id)
    assert db_claims[0][0].decision_ref == f"research_run:{outcome.run_id}"
    assert db_claims[1][0].used_for_decision and db_claims[1][0].decision_ref == "plan:v2:step-3"
    events = await events_of(sessionmaker, outcome.run_id)
    linked = [e for e in events if e.event_type == RESEARCH_DECISION_LINKED]
    assert linked[-1].payload["claims"] == [2] and linked[-1].payload["text"] == "Quellen verwendet für: plan:v2:step-3"


async def test_worker_callback_returns_cited_summary(sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer) -> None:
    engine = engine_for(sessionmaker, happy_chat(), static_search(server))
    try:
        text = await research_callback(engine)(QUESTION)
    finally:
        await engine.aclose()
    assert text.startswith("Research (completed): " + QUESTION)
    assert "[1] Toolkit 4.2 requires Python 3.10 or newer. (S1; confidence" in text
    assert "S1 Installing Toolkit 4.2" in text and "(official_docs)" in text
    assert len(format_for_worker((await _last_contract(sessionmaker)), max_chars=200)) <= 200


async def _last_contract(sm: async_sessionmaker[AsyncSession]) -> Any:
    async with sm() as s:
        run = (await s.execute(select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(1))).scalar_one()
        return await store.load_contract(s, run.id)


# ----------------------------------------------------------------------------------------------- robustness
async def test_concurrent_runs_are_isolated(sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer) -> None:
    engine = engine_for(sessionmaker, happy_chat(), static_search(server))
    try:
        a, b = await asyncio.gather(engine.run_detailed(QUESTION), engine.run_detailed(QUESTION + " Please check the docs."))
    finally:
        await engine.aclose()
    assert a.run_id != b.run_id
    for outcome in (a, b):
        assert outcome.contract.status == "completed" and len(outcome.contract.sources) == 3
        rows = await sources_of(sessionmaker, outcome.run_id)
        assert {str(r.id) for r in rows} == {s.source_id for s in outcome.contract.sources}
    assert not {s.source_id for s in a.contract.sources} & {s.source_id for s in b.contract.sources}


async def test_cancelled_run_is_marked_failed(sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer) -> None:
    server.add("/slow", Route(DOCS_PAGE, delay=3.0), host="docs.toolkit.test")
    engine = engine_for(sessionmaker, happy_chat(), StaticSearchProvider(default=[url(server, "docs.toolkit.test", "/slow")]))
    task = asyncio.create_task(engine.run_detailed(QUESTION))
    run_id: uuid.UUID | None = None
    for _ in range(100):
        await asyncio.sleep(0.05)
        async with sessionmaker() as s:
            ev = (
                await s.execute(
                    select(Event).where(Event.event_type == EventType.RESEARCH_QUERY_STARTED).order_by(Event.sequence.desc()).limit(1)
                )
            ).scalar_one_or_none()
        if ev is not None and ev.payload.get("query") and server.requests and server.requests[-1]["path"] == "/slow":
            run_id = uuid.UUID(ev.payload["research_run_id"])
            break
    assert run_id is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await engine.aclose()
    async with sessionmaker() as s:
        run = await s.get(ResearchRun, run_id)
    assert run is not None and run.status == "failed" and run.finished_at is not None
    last = (await events_of(sessionmaker, run_id))[-1]
    assert last.event_type == EventType.RESEARCH_FINISHED and last.payload["status"] == "failed" and last.severity == "error"


async def test_empty_question_is_rejected(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    engine = engine_for(sessionmaker, happy_chat(), StaticSearchProvider())
    with pytest.raises(ValidationFailed):
        await engine.run("  ")


async def test_secrets_in_question_and_urls_never_reach_events_db_or_prompts(
    sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer
) -> None:
    secret = "glpat-" + "A1b2C3d4E5f6G7h8I9j0K"
    server.add("/install", Route(DOCS_PAGE), host="docs.toolkit.test")
    leaky = url(server, "docs.toolkit.test", f"/install?token={secret}")
    chat = happy_chat()
    engine = engine_for(sessionmaker, chat, StaticSearchProvider(default=[leaky]))
    try:
        outcome = await engine.run_detailed(f"{QUESTION} (token {secret})")
    finally:
        await engine.aclose()
    assert secret not in outcome.contract.question and secret not in chat.prompts()
    assert all(secret not in s.url for s in outcome.contract.sources)
    rows = await sources_of(sessionmaker, outcome.run_id)
    assert rows and all(secret not in r.url for r in rows)
    for ev in await events_of(sessionmaker, outcome.run_id):
        assert secret not in json.dumps(ev.payload)


# ----------------------------------------------------------------------------------------------- wiring
def test_build_research_engine_from_config(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    cfg = load_config(Path(__file__).resolve().parents[2] / "config")
    engine = build_research_engine(sessionmaker, ScriptedChat(), cfg)
    assert isinstance(engine.search, SearxngSearchProvider) and engine.search.base_url == cfg.policies.research.searxng_url.rstrip("/")
    assert isinstance(engine.fetcher, HttpFetcher) and engine.fetcher.max_bytes == cfg.policies.research.max_fetch_bytes
    assert engine.models.fast.alias == cfg.models.by_role("fast").alias
    assert engine.models.deep.alias == cfg.models.by_role("planner").alias
    assert engine.planner.max_queries == min(cfg.policies.research.max_queries, 6)
    disabled = cfg.model_copy(deep=True)
    disabled.policies.research.search_provider = "none"
    assert isinstance(build_research_engine(sessionmaker, ScriptedChat(), disabled).search, DisabledSearchProvider)


@pytest.mark.live
async def test_live_research_against_target_hosts(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """BLOCKER-001: SearXNG on .225, LiteLLM/Ollama with fast-router + planner-gemma, outbound internet."""
    from hermclaw.models.gateway import LiteLLMGateway

    cfg = load_config()
    gateway = LiteLLMGateway.from_config(cfg, session_factory=sessionmaker)
    engine = build_research_engine(sessionmaker, gateway, cfg)
    try:
        contract = await engine.run("Which Python versions does SQLAlchemy 2.0 support?")
    finally:
        await engine.aclose()
        await gateway.aclose()
    assert contract.status in {"completed", "partial"} and contract.sources and contract.claims
    assert re.search(r"\[\d+\]", contract.synthesis)
