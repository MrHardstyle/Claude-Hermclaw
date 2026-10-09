"""P11 11.4/11.8-11.11 + Phase F: the RepoIntelligence facade (RepoContextProvider) against real PostgreSQL/pgvector,
real git and ripgrep."""

from __future__ import annotations

import math
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.core.errors import HermclawError
from hermclaw.core.interfaces import RepoContextProvider
from hermclaw.models.protocols import CallContext
from hermclaw.persistence.models import CodeChunk, Event
from hermclaw.repo_intelligence import RepoIntelConfig, RepoIntelligence
from hermclaw.repo_intelligence.chunking import query_text
from hermclaw.repo_intelligence.embeddings import semantic_search
from tests.integration.test_repo_intelligence_support import HashingEmbedder, build_fixture_repo, commit_all, workspace_handle, write_files

pytestmark = pytest.mark.integration


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    return build_fixture_repo(tmp_path / "fx")


async def _events(sessionmaker: Any, source_id: str, event_type: str) -> list[Event]:
    async with sessionmaker() as s:
        q = select(Event).where(Event.source_id == source_id, Event.event_type == event_type).order_by(Event.sequence)
        return list((await s.execute(q)).scalars().all())


async def test_service_implements_the_repo_context_provider_protocol(sessionmaker: Any) -> None:
    svc = RepoIntelligence(sessionmaker)
    assert isinstance(svc, RepoContextProvider)


async def test_inventory_summary_reports_inventory_and_index(sessionmaker: Any, fixture_repo: Path) -> None:
    svc = RepoIntelligence(sessionmaker, embedder=HashingEmbedder())
    ws = workspace_handle(fixture_repo)
    summary = await svc.inventory_summary(ws)
    assert summary["root"] == "fx"
    assert set(summary["primary_languages"]) >= {"python", "javascript", "php"}
    assert any(r.startswith("GET /health -> app/main.py") for r in summary["routes"])
    assert {f["name"] for f in summary["tests"]["frameworks"]} >= {"pytest", "jest", "phpunit"}
    assert summary["git"]["branch"] == "main" and summary["git"]["dirty"] is False
    assert summary["index"]["available"] is True and summary["index"]["git_sha"] == summary["git"]["head"]
    assert summary["index"]["embedding_pending"] == 0
    assert "sk-live-should-not-leak" not in str(summary)
    # the workspace has its own index key (concurrent jobs on other commits never share rows)
    key = svc.index_key(ws)
    assert key.startswith(ws.repository_key + "@ws:")
    await svc.aclose()


async def test_fusion_search_prefers_files_matching_several_signals(sessionmaker: Any, fixture_repo: Path) -> None:
    svc = RepoIntelligence(sessionmaker, embedder=HashingEmbedder())
    ws = workspace_handle(fixture_repo)
    hits = await svc.search(ws, "calculate invoice total with tax", k=8)
    assert hits, "no hits"
    top = hits[0]
    assert top.path == "app/services/billing.py"
    assert {"lexical", "symbol", "structural", "semantic", "test_reference"} <= set(top.signals)
    assert "calculate_invoice_total" in top.snippet
    assert 0 < top.score <= 1.0
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    # files hit by fewer signals rank below the multi-signal file
    single = [h for h in hits if len(h.signals) == 1]
    assert all(h.score < top.score for h in single)
    assert all(not h.path.startswith(("deploy/", "build/")) for h in hits)

    bound = svc.bind(ws)
    outcome = await svc.search_files(bound.target, "calculate invoice total with tax", k=8)
    structural = outcome.per_signal["structural"]["app/services/billing.py"]
    assert structural.detail == "function calculate_invoice_total used by 2 file(s)"  # app/main.py + tests/test_billing.py
    file_hits = await bound.search("ShoppingCart addItem", k=5)
    assert file_hits[0].path == "web/src/cart.ts"
    assert file_hits[0].signals.get("symbol", 0) > 0 and file_hits[0].ranks["symbol"] == 1
    route_hits = await bound.search("which handler serves /api/items", k=5)
    assert route_hits[0].path == "web/server.js" and "structural" in route_hits[0].signals

    evs = await _events(sessionmaker, svc.index_key(ws), EventType.REPO_SEARCH_EXECUTED)
    fusion = [e for e in evs if e.payload["kind"] == "fusion"]
    assert len(fusion) == 4 and fusion[0].payload["top"][0]["path"] == "app/services/billing.py"
    assert fusion[0].payload["signals"]["semantic"] > 0 and fusion[0].payload["degraded"] == []
    await svc.aclose()


async def test_semantic_search_orders_by_pgvector_cosine_distance(sessionmaker: Any, fixture_repo: Path) -> None:
    emb = HashingEmbedder()
    svc = RepoIntelligence(sessionmaker, embedder=emb)
    bound = svc.open(fixture_repo, f"sem-{uuid.uuid4().hex[:8]}")
    await bound.index()
    hits = await bound.semantic("shopping cart checkout label", k=5)
    assert hits and hits[0].path == "web/src/cart.ts"
    distances = [h.distance for h in hits]
    assert distances == sorted(distances)
    # the distances are pgvector cosine distances of the stored vectors
    qvec = emb.vector(query_text("shopping cart checkout label", svc.cfg))
    async with sessionmaker() as s:
        rows = (await s.execute(select(CodeChunk).where(CodeChunk.repository_key == bound.target.repository_key))).scalars().all()
    by_range = {(r.path, r.start_line, r.end_line): r for r in rows}
    for h in hits:
        r = by_range[(h.path, h.start_line, h.end_line)]
        cos = sum(a * b for a, b in zip(qvec, r.embedding, strict=True))
        assert math.isclose(h.distance, 1.0 - cos, abs_tol=1e-4)
        assert math.isclose(h.score, cos, abs_tol=1e-4)
    # HNSW path (approximate scan forced by a low exact-scan threshold) returns the same nearest neighbour
    async with sessionmaker() as s:
        approx = await semantic_search(
            s,
            emb,
            bound.target.repository_key,
            "shopping cart checkout label",
            k=3,
            cfg=RepoIntelConfig(semantic_exact_scan_max_rows=0),
            ctx=CallContext(purpose="embedding"),
        )
    assert approx and approx[0].path == "web/src/cart.ts"
    # another model never reads vectors of this one
    other = HashingEmbedder(model_name="other-model")
    async with sessionmaker() as s:
        assert (
            await semantic_search(s, other, bound.target.repository_key, "cart", k=3, cfg=svc.cfg, ctx=CallContext(purpose="embedding"))
            == []
        )
    await svc.aclose()


async def test_find_symbol_handles_qualified_names_and_languages(sessionmaker: Any, fixture_repo: Path) -> None:
    svc = RepoIntelligence(sessionmaker)
    ws = workspace_handle(fixture_repo)
    hits = await svc.find_symbol(ws, "ShoppingCart.addItem")
    assert hits[0].path == "web/src/cart.ts" and hits[0].start_line == 11 and hits[0].end_line == 13
    php = await svc.find_symbol(ws, "ProfileController::show")
    assert php[0].path == "php/src/Controller/ProfileController.php" and php[0].start_line == 6
    py = await svc.find_symbol(ws, "calculate_invoice_total()")
    assert py[0].path == "app/services/billing.py" and "def calculate_invoice_total" in py[0].snippet
    tables = await svc.find_symbol(ws, "customers")
    assert tables[0].path == "migrations/001_init.sql"
    fuzzy = await svc.find_symbol(ws, "invoice_tot")  # no exact match: substring matches
    assert fuzzy and fuzzy[0].path in ("app/services/billing.py", "app/main.py")
    assert await svc.find_symbol(ws, "definitely_not_defined_anywhere") == []
    await svc.aclose()


async def test_targeted_read_and_protection(sessionmaker: Any, fixture_repo: Path) -> None:
    svc = RepoIntelligence(sessionmaker)
    ws = workspace_handle(fixture_repo)
    text = await svc.read(ws, "app/services/billing.py", 6, 8)
    assert text.splitlines() == [
        "def calculate_invoice_total(amounts: list[int]) -> float:",
        "    subtotal = sum(amounts)",
        "    return round(subtotal * (1 + TAX_RATE), 2)",
    ]
    clipped = await svc.read(ws, "app/services/billing.py", 1, None, max_chars=40)
    assert len(clipped) <= 40
    for bad in ("deploy/id_rsa", "../outside.txt", "/etc/passwd", ".git/config", "assets/logo.png", "missing.py"):
        with pytest.raises(HermclawError):
            await svc.read(ws, bad)
    res = await svc.bind(ws).read("web/server.js", 7, 7)
    assert res.numbered().startswith("7 | app.get('/api/items'")
    await svc.aclose()


async def test_context_for_returns_relevant_redacted_snippets_within_budget(sessionmaker: Any, fixture_repo: Path) -> None:
    write_files(
        fixture_repo,
        {
            "app/settings.py": "DB_PASSWORD = 'hunter2-very-secret'\npassword = 'hunter2-very-secret'\n\ndef invoice_settings():\n    return {}\n"
        },
    )
    commit_all(fixture_repo, "settings")
    svc = RepoIntelligence(sessionmaker, embedder=HashingEmbedder())
    ws = workspace_handle(fixture_repo)
    budget = 1_500
    snippets = await svc.context_for(ws, "fix the invoice total calculation and its password settings", budget_chars=budget)
    paths = [s.path for s in snippets]
    assert "app/services/billing.py" in paths[:3], paths
    assert len(set(paths)) >= 2, paths  # the budget is shared between files
    assert sum(len(s.snippet) for s in snippets) <= budget
    assert all(s.snippet.strip() for s in snippets)
    joined = "\n".join(s.snippet for s in snippets)
    assert "hunter2-very-secret" not in joined
    assert len({(s.path, s.start_line) for s in snippets}) == len(snippets)
    big = await svc.bind(ws).context_for("calculate invoice total", budget=24_000)
    assert any("calculate_invoice_total" in s.snippet for s in big)
    await svc.aclose()


async def test_uncommitted_changes_are_visible_through_the_overlay(sessionmaker: Any, fixture_repo: Path) -> None:
    svc = RepoIntelligence(sessionmaker)
    ws = workspace_handle(fixture_repo)
    assert await svc.find_symbol(ws, "refund_payment") == []
    # the coder edits files without committing: a new function, a deleted file
    path = fixture_repo / "app/services/billing.py"
    path.write_text(path.read_text() + "\n\ndef refund_payment(amount: int) -> int:\n    return -amount\n")
    (fixture_repo / "web/lib/util.js").unlink()
    hits = await svc.find_symbol(ws, "refund_payment")
    assert hits and hits[0].path == "app/services/billing.py" and hits[0].start_line == 20
    found = await svc.search(ws, "refund payment amount", k=5)
    assert found[0].path == "app/services/billing.py"
    assert not [h for h in await svc.search(ws, "slugify text", k=10) if h.path == "web/lib/util.js"]
    inv = await svc.inventory(svc.target_of(ws))
    assert inv.git.dirty and "web/lib/util.js" not in {f.path for f in inv.files}
    await svc.aclose()


async def test_grep_and_find_files_record_redacted_events(sessionmaker: Any, fixture_repo: Path) -> None:
    svc = RepoIntelligence(sessionmaker)
    bound = svc.open(fixture_repo, f"grep-{uuid.uuid4().hex[:8]}")
    res = await bound.grep("slugify", word=True)
    assert res.engine == "ripgrep"
    assert {h.path for h in res.hits} == {"web/lib/util.js", "web/server.js", "web/src/cart.ts"}
    regex = await bound.grep(r"^def calculate_\w+\(", regex=True)
    assert [(h.path, h.line) for h in regex.hits] == [("app/services/billing.py", 6)]
    await bound.grep("password=hunter2-should-be-redacted")
    files = await bound.find_files("ProfileController")
    assert files[0].path == "php/src/Controller/ProfileController.php"
    evs = await _events(sessionmaker, bound.target.repository_key, EventType.REPO_SEARCH_EXECUTED)
    assert [e.payload["kind"] for e in evs] == ["lexical", "lexical", "lexical", "filename"]
    assert "hunter2-should-be-redacted" not in str([e.payload for e in evs])
    await svc.aclose()


async def test_drop_workspace_index_removes_only_that_workspace(sessionmaker: Any, fixture_repo: Path) -> None:
    svc = RepoIntelligence(sessionmaker)
    ws1, ws2 = workspace_handle(fixture_repo, key="shared-repo"), workspace_handle(fixture_repo, key="shared-repo")
    await svc.find_symbol(ws1, "health")
    await svc.find_symbol(ws2, "health")
    counts = await svc.drop_workspace_index(ws1)
    assert counts["symbols"] > 0
    async with sessionmaker() as s:
        left = (await s.execute(select(CodeChunk.repository_key).where(CodeChunk.repository_key.like("shared-repo@ws:%")).distinct())).all()
    assert [r[0] for r in left] == [svc.index_key(ws2)]
    shared = RepoIntelligence(sessionmaker, config=RepoIntelConfig(index_scope="repository"))
    assert shared.index_key(ws1) == "shared-repo"
    assert await shared.drop_workspace_index(ws1) == {"symbols": 0, "chunks": 0}
    await svc.aclose()
