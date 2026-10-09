"""P11 11.10: Reciprocal Rank Fusion over lexical/symbol/structural/semantic/test-reference/dependency signals."""

from __future__ import annotations

import math

from hermclaw.repo_intelligence.config import RepoIntelConfig
from hermclaw.repo_intelligence.dependencies import DependencyGraph
from hermclaw.repo_intelligence.query import analyze
from hermclaw.repo_intelligence.ranking import (
    dependency_signal,
    fuse,
    lexical_signal,
    name_subject,
    query_definitions,
    referencing_tests_signal,
    semantic_signal,
    structural_signal,
    symbol_signal,
)
from hermclaw.repo_intelligence.schemas import ChunkHit, ImportRecord, LexicalHit, Route, SignalHit, SymbolRecord


def _h(path: str, score: float, **kw: object) -> SignalHit:
    return SignalHit(path=path, score=score, **kw)  # type: ignore[arg-type]


def test_rrf_math_ties_and_normalisation() -> None:
    cfg = RepoIntelConfig(rrf_k=10, signal_weights={"lexical": 1.0, "symbol": 2.0}, signal_relative_floors={})
    fused = fuse({"lexical": [_h("a", 5), _h("b", 5), _h("c", 1)], "symbol": [_h("c", 3)]}, cfg)
    by = {h.path: h for h in fused}
    # competition ranking: a and b tie at rank 1, c is rank 3 lexically and rank 1 by symbol
    assert by["a"].ranks == {"lexical": 1} and by["b"].ranks == {"lexical": 1} and by["c"].ranks == {"lexical": 3, "symbol": 1}
    assert math.isclose(by["a"].rrf, 1.0 / 11, rel_tol=1e-6)
    assert math.isclose(by["c"].rrf, 1.0 / 13 + 2.0 / 11, rel_tol=1e-6)
    ideal = 1.0 / 11 + 2.0 / 11
    assert math.isclose(by["c"].score, round(by["c"].rrf / ideal, 6), rel_tol=1e-5)
    assert [h.path for h in fused] == ["c", "a", "b"]  # deterministic tie order by path
    assert all(0 < h.score <= 1 for h in fused)
    assert by["c"].signals == {"lexical": 1.0, "symbol": 3.0}
    # a file first in every active signal scores exactly 1.0
    top = fuse({"lexical": [_h("x", 2)], "symbol": [_h("x", 1)]}, cfg)[0]
    assert top.score == 1.0


def test_files_matching_several_signals_rank_first() -> None:
    cfg = RepoIntelConfig()
    signals = {
        "lexical": [_h("only_lexical.py", 10.0), _h("multi.py", 9.0)],
        "symbol": [_h("only_symbol.py", 4.0), _h("multi.py", 3.5)],
        "semantic": [_h("only_semantic.py", 1.9), _h("multi.py", 1.8)],
        "structural": [_h("multi.py", 2.0)],
    }
    fused = fuse(signals, cfg)
    assert fused[0].path == "multi.py"
    assert set(fused[0].signals) == {"lexical", "symbol", "semantic", "structural"}
    assert fused[0].reasons[0].startswith("symbol#2")


def test_relative_floor_drops_incidental_matches_before_fusion() -> None:
    cfg = RepoIntelConfig()
    fused = fuse({"lexical": [_h("strong.py", 100.0), _h("weak.py", 3.0)], "structural": [_h("weak.py", 2.0)]}, cfg)
    by = {h.path: h for h in fused}
    assert "lexical" not in by["weak.py"].signals  # 3% of the best lexical score is noise
    no_floor = fuse({"lexical": [_h("strong.py", 100.0), _h("weak.py", 3.0)]}, RepoIntelConfig(signal_relative_floors={}))
    assert {h.path for h in no_floor} == {"strong.py", "weak.py"}


def test_boost_scales_supporting_evidence() -> None:
    cfg = RepoIntelConfig(signal_relative_floors={})
    full = fuse({"lexical": [_h("a", 1)], "test_reference": [_h("a", 1, boost=1.0)]}, cfg)[0]
    half = fuse({"lexical": [_h("a", 1)], "test_reference": [_h("a", 1, boost=0.5)]}, cfg)[0]
    assert half.rrf < full.rrf and math.isclose(full.rrf - half.rrf, 0.5 * cfg.weight("test_reference") / (cfg.rrf_k + 1), rel_tol=1e-6)


def test_lexical_signal_weights_identifiers_coverage_and_word_boundaries() -> None:
    q = analyze("calculate_invoice_total tax")
    hits = [
        LexicalHit(path="impl.py", line=6, text="def calculate_invoice_total(amounts):", matches=["calculate_invoice_total"]),
        LexicalHit(path="impl.py", line=7, text="    return total * TAX", matches=["TAX"]),
        LexicalHit(path="other.py", line=1, text="taxonomy = 1", matches=["tax"]),
    ]
    sig = {h.path: h for h in lexical_signal(hits, q, n_files=50)}
    assert sig["impl.py"].score > sig["other.py"].score
    assert sig["impl.py"].start_line == 3 and sig["impl.py"].end_line == 9  # best line +- context
    assert lexical_signal([], q, n_files=10) == []


def test_symbol_and_structural_signals() -> None:
    q = analyze("ShoppingCart.addItem for /api/items and the payments table")
    syms = [
        SymbolRecord(
            name="addItem", kind="method", language="typescript", path="cart.ts", start_line=11, end_line=13, parent="ShoppingCart"
        ),
        SymbolRecord(name="ShoppingCart", kind="class", language="typescript", path="cart.ts", start_line=8, end_line=18),
        SymbolRecord(name="add", kind="function", language="python", path="other.py", start_line=1, end_line=2),
        SymbolRecord(name="import", kind="import", language="python", path="imp.py", start_line=1, end_line=1),
    ]
    sym = {h.path: h for h in symbol_signal(syms, q)}
    assert set(sym) == {"cart.ts"} and sym["cart.ts"].start_line == 11 and "ShoppingCart.addItem" in sym["cart.ts"].detail
    routes = [Route(method="GET", path="/api/items", file="server.js", line=7), Route(method="GET", path="/health", file="main.py", line=3)]
    tables = [SymbolRecord(name="payments", kind="table", language="sql", path="db/001.sql", start_line=5, end_line=9)]
    st = {h.path: h for h in structural_signal(["src/cart/items.ts", "README.md"], q, routes=routes, tables=tables)}
    assert st["server.js"].start_line == 7 and st["server.js"].detail == "route GET /api/items"
    assert st["db/001.sql"].detail == "table payments"
    assert "src/cart/items.ts" in st and "README.md" not in st and "main.py" not in st


def test_structural_signal_counts_files_using_matching_definitions() -> None:
    q = analyze("invoice total")
    impl = SymbolRecord(name="invoice_total", kind="function", language="python", path="billing.py", start_line=4, end_line=9)
    other = SymbolRecord(name="unrelated", kind="function", language="python", path="x.py", start_line=1, end_line=2)
    route = SymbolRecord(name="GET /invoice", kind="route", language="python", path="api.py", start_line=1, end_line=1)
    assert query_definitions([other, route, impl], q) == [impl]
    sig = {h.path: h for h in structural_signal([], q, referenced=[(impl, 7), (other, 0)])}
    assert set(sig) == {"billing.py"} and sig["billing.py"].score == 5.0  # capped at 5 using files
    assert (sig["billing.py"].start_line, sig["billing.py"].detail) == (4, "function invoice_total used by 7 file(s)")


def test_semantic_signal_drops_non_evidence() -> None:
    chunks = [
        ChunkHit(path="a.py", start_line=1, end_line=5, score=0.8, distance=0.2),
        ChunkHit(path="a.py", start_line=9, end_line=12, score=0.6, distance=0.4),
        ChunkHit(path="b.py", start_line=1, end_line=5, score=0.5, distance=0.5),
        ChunkHit(path="c.py", start_line=1, end_line=5, score=0.1, distance=0.9),
        ChunkHit(path="d.py", start_line=1, end_line=5, score=-0.2, distance=1.2),
    ]
    sig = {h.path: h for h in semantic_signal(chunks, min_similarity=0.05, relative_floor=0.5)}
    assert set(sig) == {"a.py", "b.py"} and sig["a.py"].start_line == 1 and math.isclose(sig["a.py"].score, 1.8)
    assert len(semantic_signal(chunks)) == 3  # no floors: every neighbour with similarity > 0


def test_test_reference_signal() -> None:
    graph = DependencyGraph.from_imports(
        {
            "tests/test_billing.py": [ImportRecord(module="app.billing", resolved="app/billing.py")],
            "app/api.py": [ImportRecord(module="x", resolved="app/billing.py")],
        }
    )
    tests = ["tests/test_billing.py", "tests/test_cart.py", "spec/orders.spec.ts"]
    sig = {
        h.path: h
        for h in referencing_tests_signal(
            ["app/billing.py", "app/cart.py", "src/orders.ts", "app/api.py", "tests/test_billing.py"],
            graph,
            tests,
            relevant_tests=["tests/test_billing.py"],
            symbol_mentions={"app/api.py": ["tests/test_cart.py"]},
            relevance={"app/billing.py": 1.0, "app/cart.py": 0.5, "src/orders.ts": 1.0, "app/api.py": 1.0},
        )
    }
    assert sig["app/billing.py"].score == 2.0  # imported by a relevant test (counts double)
    assert sig["app/cart.py"].score == 0.75 * 0.5 and sig["app/cart.py"].boost == 0.5  # named test, weak candidate
    assert sig["src/orders.ts"].score == 0.75  # orders.spec.ts naming convention
    assert sig["app/api.py"].score == 0.5  # a test mentions its symbols
    assert "tests/test_billing.py" not in sig  # tests themselves are not candidates of this signal
    assert name_subject("php/tests/UserControllerTest.php") == "usercontroller" and name_subject("tests/conftest.py") == ""


def test_dependency_signal() -> None:
    graph = DependencyGraph()
    graph.add("a.py", "b.py")
    graph.add("c.py", "a.py")
    graph.add("d.py", "e.py")
    sig = {h.path: h for h in dependency_signal([("a.py", 1.0), ("d.py", 0.5)], graph)}
    assert sig["b.py"].score == 1.0 and sig["c.py"].score == 1.0 and sig["e.py"].score == 0.5
    assert sig["b.py"].detail == "near a.py" and "a.py" not in sig
