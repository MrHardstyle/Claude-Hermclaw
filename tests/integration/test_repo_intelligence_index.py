"""P11 11.5/11.7/11.8/11.9/11.12: full index, incremental reindex by Git SHA, persistence (real PostgreSQL + pgvector,
real git CLI)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from hermclaw.contracts.events import EventType
from hermclaw.persistence.models import CodeChunk, CodeSymbol, Event, RepoIndexRun
from hermclaw.repo_intelligence import INDEX_VERSION, IndexTarget, RepoIndexer, RepoIntelConfig
from tests.integration.test_repo_intelligence_support import HashingEmbedder, build_fixture_repo, commit_all, git, write_files

pytestmark = pytest.mark.integration


def _key() -> str:
    return f"idx-{uuid.uuid4().hex[:10]}"


async def _rows(sessionmaker: Any, model: Any, key: str) -> list[Any]:
    async with sessionmaker() as s:
        return list((await s.execute(select(model).where(model.repository_key == key))).scalars().all())


async def _events(sessionmaker: Any, key: str) -> list[Event]:
    async with sessionmaker() as s:
        q = select(Event).where(Event.source_type == "repo_intelligence", Event.source_id == key).order_by(Event.sequence)
        return list((await s.execute(q)).scalars().all())


async def test_full_index_persists_symbols_imports_chunks_and_run(sessionmaker: Any, tmp_path: Path) -> None:
    repo = build_fixture_repo(tmp_path / "fx")
    head = git(repo, "rev-parse", "HEAD")
    emb = HashingEmbedder()
    key = _key()
    indexer = RepoIndexer(sessionmaker, embedder=emb)
    stats = await indexer.index(IndexTarget(root=repo, repository_key=key))

    assert stats.mode == "full" and stats.git_sha == head and stats.source == "git"
    assert stats.index_version == INDEX_VERSION
    assert stats.files_indexed > 20 and stats.symbols > 20 and stats.chunks > 10
    assert stats.embedded == stats.chunks and stats.embedding_pending == 0 and stats.embedding_error is None
    assert stats.files_skipped.get("sensitive") == 1  # deploy/id_rsa
    assert stats.files_skipped.get("binary") == 1  # assets/logo.png
    assert stats.files_skipped.get("excluded", 0) >= 2  # lockfiles

    symbols = await _rows(sessionmaker, CodeSymbol, key)
    assert {s.git_sha for s in symbols} == {head}
    by = {(s.path, s.kind, s.name): s for s in symbols}
    assert ("app/services/billing.py", "function", "calculate_invoice_total") in by
    assert by[("app/services/billing.py", "method", "render")].parent == "InvoiceFormatter"
    assert ("web/src/cart.ts", "class", "ShoppingCart") in by
    assert by[("web/src/cart.ts", "method", "addItem")].parent == "ShoppingCart"
    assert ("web/server.js", "function", "startServer") in by
    assert by[("php/src/Controller/ProfileController.php", "class", "ProfileController")].parent == "App\\Controller"
    assert ("php/src/helpers.php", "function", "render_profile") in by
    # routes and DB schema awareness as structural symbols
    assert ("app/main.py", "route", "GET /health") in by
    assert ("app/main.py", "route", "POST /invoices/{invoice_id}/total") in by
    assert ("web/server.js", "route", "GET /api/items") in by
    assert ("php/routes/web.php", "route", "GET /profile") in by
    assert ("php/public/index.php", "route", "POST /index.php") in by
    assert ("migrations/001_init.sql", "table", "customers") in by
    assert ("alembic/versions/a1b2c3_create_users.py", "table", "users") in by
    assert ("app/models.py", "table", "invoices") in by
    # call references are kept with the definition; module-level calls in a per-file "module" row
    assert "format_money" in by[("app/services/billing.py", "method", "render")].references
    assert "include_router" in by[("app/main.py", "module", "main.py")].references
    assert by[("app/main.py", "module", "main.py")].start_line == 1
    # dependency relations (11.7): imports resolved to workspace files
    imports = {(s.path, s.name): s.references[0]["resolved"] for s in symbols if s.kind == "import"}
    assert imports[("tests/test_billing.py", "app.services.billing")] == "app/services/billing.py"
    assert imports[("app/main.py", "app.services.billing")] == "app/services/billing.py"
    assert imports[("app/main.py", "fastapi")] is None
    assert imports[("web/server.js", "./lib/util")] == "web/lib/util.js"
    assert imports[("web/src/cart.ts", "../lib/util")] == "web/lib/util.js"
    assert imports[("php/public/index.php", "/../src/helpers.php")] == "php/src/helpers.php"
    # nested composer.json PSR-4 map: App\\ -> php/src/
    assert imports[("php/routes/web.php", "App\\Controller\\ProfileController")] == "php/src/Controller/ProfileController.php"

    # never indexed: secrets, binaries, ignored files
    paths = {s.path for s in symbols}
    chunks = await _rows(sessionmaker, CodeChunk, key)
    cpaths = {c.path for c in chunks}
    for forbidden in ("deploy/id_rsa", "assets/logo.png", "build/out.py", "debug.log", "web/package-lock.json"):
        assert forbidden not in paths and forbidden not in cpaths
    assert all(c.embedding is not None and c.embedding_model == emb.model_name for c in chunks)
    assert all(len(c.embedding) == 768 for c in chunks)
    billing = [c for c in chunks if c.path == "app/services/billing.py"]
    assert billing and any(c.symbol and "calculate_invoice_total" in c.symbol for c in billing)
    # the README secret never reaches the embedding model
    assert not any("sk-live-should-not-leak" in t for t in emb.texts)
    assert emb.contexts and all(c.purpose == "embedding" for c in emb.contexts)

    async with sessionmaker() as s:
        run = await s.get(RepoIndexRun, uuid.UUID(stats.run_id or ""))
    assert run is not None and run.status == "finished" and run.git_sha == head and run.base_index_sha is None
    assert run.inventory["routes"] and run.inventory["git"]["head"] == head
    assert run.stats["repository_key"] == key and run.stats["embedded"] == stats.chunks

    evs = await _events(sessionmaker, key)
    types = [e.event_type for e in evs]
    assert types[:2] == [EventType.REPO_INVENTORY_STARTED, EventType.REPO_INVENTORY_FINISHED]
    assert EventType.REPO_INDEX_UPDATED in types
    phases = [e.payload.get("phase") for e in evs if e.event_type == EventType.REPO_INDEX_UPDATED]
    assert phases == ["index", "embedding"]


async def test_incremental_reindex_only_touches_changed_files(sessionmaker: Any, tmp_path: Path) -> None:
    repo = build_fixture_repo(tmp_path / "fx")
    write_files(
        repo, {"app/report.py": "from app.services.discount import apply_discount\n\n\ndef report():\n    return apply_discount(1)\n"}
    )
    sha1 = commit_all(repo, "report")
    emb = HashingEmbedder()
    key = _key()
    indexer = RepoIndexer(sessionmaker, embedder=emb)
    target = IndexTarget(root=repo, repository_key=key)
    first = await indexer.index(target)
    assert first.mode == "full"
    before_chunks = {c.id: c for c in await _rows(sessionmaker, CodeChunk, key)}
    before_syms = {s.id: s for s in await _rows(sessionmaker, CodeSymbol, key)}
    untouched_chunk_ids = {i for i, c in before_chunks.items() if c.path == "app/services/billing.py"}
    untouched_sym_ids = {i for i, s in before_syms.items() if s.path == "web/src/cart.ts"}
    report_import = next(s for s in before_syms.values() if s.path == "app/report.py" and s.kind == "import")
    # discount.py does not exist yet: only the enclosing package resolves
    assert report_import.references[0]["resolved"] == "app/services/__init__.py"

    # modify one file, add one, delete one, commit
    (repo / "web/server.js").write_text((repo / "web/server.js").read_text() + "\nfunction stopServer(s) {\n  s.close();\n}\n")
    write_files(repo, {"app/services/discount.py": "def apply_discount(value: int) -> int:\n    return value - 1\n"})
    git(repo, "rm", "-q", "php/src/helpers.php")
    sha2 = commit_all(repo, "change")
    emb.texts.clear()
    emb.calls.clear()

    second = await indexer.index(target)
    assert second.mode == "incremental" and second.base_sha == sha1 and second.git_sha == sha2
    assert second.changed_paths == ["app/services/discount.py", "web/server.js"]
    assert second.deleted_paths == ["php/src/helpers.php"]
    assert second.files_indexed == 2 and second.files_deleted == 1

    after_chunks = {c.id: c for c in await _rows(sessionmaker, CodeChunk, key)}
    after_syms = {s.id: s for s in await _rows(sessionmaker, CodeSymbol, key)}
    # untouched rows are the very same rows (not re-chunked, not re-embedded), still stamped with the old SHA
    assert untouched_chunk_ids <= set(after_chunks)
    assert untouched_sym_ids <= set(after_syms)
    assert {after_chunks[i].git_sha for i in untouched_chunk_ids} == {sha1}
    # changed/new files carry the new SHA; the deleted file is gone
    assert {c.git_sha for c in after_chunks.values() if c.path in ("web/server.js", "app/services/discount.py")} == {sha2}
    assert not [c for c in after_chunks.values() if c.path == "php/src/helpers.php"]
    assert not [s for s in after_syms.values() if s.path == "php/src/helpers.php"]
    assert any(s.name == "stopServer" and s.path == "web/server.js" for s in after_syms.values())
    # only chunks of the changed files went to the embedding model
    assert emb.texts and all(("web/server.js" in t) or ("app/services/discount.py" in t) for t in emb.texts)
    # imports of untouched files are re-resolved against the new file set
    by_path_imports = {(s.path, s.name): s for s in after_syms.values() if s.kind == "import"}
    assert by_path_imports[("app/report.py", "app.services.discount")].references[0]["resolved"] == "app/services/discount.py"
    assert by_path_imports[("php/public/index.php", "/../src/helpers.php")].references[0]["resolved"] is None

    async with sessionmaker() as s:
        run = await s.get(RepoIndexRun, uuid.UUID(second.run_id or ""))
    assert run is not None and run.base_index_sha == sha1 and run.stats["mode"] == "incremental"

    # same SHA again: noop, no new run row
    third = await indexer.index(target)
    assert third.mode == "noop" and third.run_id == second.run_id
    async with sessionmaker() as s:
        runs = (await s.execute(select(RepoIndexRun).where(RepoIndexRun.stats["repository_key"].astext == key))).scalars().all()
    assert len(runs) == 2
    forced = await indexer.index(target, force_full=True)
    assert forced.mode == "full"


def _big_module(changed: bool) -> str:
    funcs = []
    for name in ("alpha_report", "beta_report", "gamma_report"):
        body = "".join(f"    total_{i} = value * {i}  # step {i} of the {name} computation\n" for i in range(14))
        tail = "    return total_0 + 1\n" if (changed and name == "beta_report") else "    return total_0\n"
        funcs.append(f"def {name}(value: int) -> int:\n{body}{tail}")
    return "\n\n".join(funcs)


async def test_unchanged_chunks_of_a_changed_file_reuse_vectors(sessionmaker: Any, tmp_path: Path) -> None:
    repo = build_fixture_repo(tmp_path / "fx")
    write_files(repo, {"app/reports.py": _big_module(False)})
    commit_all(repo, "reports")
    emb = HashingEmbedder()
    key = _key()
    indexer = RepoIndexer(sessionmaker, embedder=emb)
    target = IndexTarget(root=repo, repository_key=key)
    await indexer.index(target)
    report_chunks = [c for c in await _rows(sessionmaker, CodeChunk, key) if c.path == "app/reports.py"]
    assert len(report_chunks) == 3  # one symbol-aligned chunk per function
    write_files(repo, {"app/reports.py": _big_module(True)})
    commit_all(repo, "change beta")
    emb.texts.clear()
    stats = await indexer.index(target)
    assert stats.mode == "incremental" and stats.changed_paths == ["app/reports.py"]
    assert stats.chunks == 3 and stats.embeddings_reused == 2
    assert len(emb.texts) == 1 and "beta_report" in emb.texts[0]


async def test_incremental_falls_back_to_full_when_previous_sha_is_unknown_or_version_changed(sessionmaker: Any, tmp_path: Path) -> None:
    repo = build_fixture_repo(tmp_path / "fx")
    key = _key()
    indexer = RepoIndexer(sessionmaker)
    target = IndexTarget(root=repo, repository_key=key)
    first = await indexer.index(target)
    assert first.embedding_pending == 0 and first.embedding_model is None  # no embedder configured
    write_files(repo, {"app/extra.py": "def extra():\n    return 1\n"})
    commit_all(repo, "extra")
    # an index version bump forces a full rebuild
    async with sessionmaker() as s, s.begin():
        run = await s.get(RepoIndexRun, uuid.UUID(first.run_id or ""))
        assert run is not None
        run.stats = {**run.stats, "index_version": INDEX_VERSION - 1}
    second = await indexer.index(target)
    assert second.mode == "full"
    # the previous commit vanished from the clone (e.g. force-pushed history): full rebuild
    write_files(repo, {"app/extra2.py": "def extra2():\n    return 2\n"})
    commit_all(repo, "extra2")
    async with sessionmaker() as s, s.begin():
        await s.execute(update(RepoIndexRun).where(RepoIndexRun.id == uuid.UUID(second.run_id or "")).values(git_sha="f" * 40))
    third = await indexer.index(target)
    assert third.mode == "full" and third.base_sha is None
    # changed index-relevant configuration (e.g. more protected globs): full rebuild, the new secret glob applies
    write_files(repo, {"app/extra3.py": "def extra3():\n    return 3\n"})
    commit_all(repo, "extra3")
    stricter = RepoIndexer(sessionmaker, config=RepoIntelConfig(sensitive_globs=(*RepoIntelConfig().sensitive_globs, "app/extra*.py")))
    fourth = await stricter.index(target)
    assert fourth.mode == "full" and fourth.index_config != third.index_config
    assert not [s for s in await _rows(sessionmaker, CodeSymbol, key) if s.path.startswith("app/extra")]


async def test_concurrent_index_calls_are_serialised(sessionmaker: Any, tmp_path: Path) -> None:
    repo = build_fixture_repo(tmp_path / "fx")
    key = _key()
    a = RepoIndexer(sessionmaker)
    b = RepoIndexer(sessionmaker)  # a second indexer instance only shares the PostgreSQL advisory lock
    target = IndexTarget(root=repo, repository_key=key)
    results = await asyncio.gather(a.index(target), b.index(target), a.index(target))
    modes = sorted(r.mode for r in results)
    assert modes == ["full", "noop", "noop"]
    async with sessionmaker() as s:
        runs = (await s.execute(select(RepoIndexRun).where(RepoIndexRun.stats["repository_key"].astext == key))).scalars().all()
    assert len(runs) == 1
    syms = await _rows(sessionmaker, CodeSymbol, key)
    assert len({(x.path, x.kind, x.name, x.start_line) for x in syms}) == len(syms)  # no duplicate rows


async def test_non_git_workspace_is_indexed_from_the_working_tree(sessionmaker: Any, tmp_path: Path) -> None:
    root = build_fixture_repo(tmp_path / "plain", git_init=False)
    key = _key()
    indexer = RepoIndexer(sessionmaker, embedder=HashingEmbedder())
    target = IndexTarget(root=root, repository_key=key)
    first = await indexer.index(target)
    assert first.source == "worktree" and first.mode == "full" and first.git_sha.startswith("worktree:")
    syms = await _rows(sessionmaker, CodeSymbol, key)
    assert any(s.name == "calculate_invoice_total" for s in syms)
    assert not any(s.path == "build/out.py" for s in syms)  # .gitignore is honoured without git, too
    again = await indexer.index(target)
    assert again.mode == "noop"
    write_files(root, {"app/new_module.py": "def brand_new():\n    return 3\n"})
    changed = await indexer.index(target)
    assert changed.mode == "full" and changed.git_sha != first.git_sha  # no SHA to diff: full rebuild
    assert any(s.name == "brand_new" for s in await _rows(sessionmaker, CodeSymbol, key))


async def test_workspace_scoped_keys_reuse_vectors_and_purge(sessionmaker: Any, tmp_path: Path) -> None:
    repo = build_fixture_repo(tmp_path / "fx")
    base = f"repo-{uuid.uuid4().hex[:8]}"
    emb = HashingEmbedder()
    indexer = RepoIndexer(sessionmaker, embedder=emb)
    first = IndexTarget(root=repo, repository_key=f"{base}@ws:1", repository=base)
    s1 = await indexer.index(first)
    assert s1.embedded == s1.chunks and s1.repository == base
    emb.texts.clear()
    second = IndexTarget(root=repo, repository_key=f"{base}@ws:2", repository=base)
    assert await indexer.reuse_keys(second) == [first.repository_key]
    s2 = await indexer.index(second)
    assert s2.mode == "full" and s2.embeddings_reused == s2.chunks
    assert emb.texts == []  # every vector came from the sibling workspace index
    counts = await indexer.purge(first)
    assert counts["symbols"] > 0 and counts["chunks"] == s1.chunks
    assert await _rows(sessionmaker, CodeChunk, first.repository_key) == []
    assert len(await _rows(sessionmaker, CodeChunk, second.repository_key)) == s2.chunks
    assert (await indexer.latest_run(first.repository_key)) is None  # next index of the purged key is a full one
    assert (await indexer.index(first)).mode == "full"


async def test_embedding_batches_respect_batch_size(sessionmaker: Any, tmp_path: Path) -> None:
    repo = build_fixture_repo(tmp_path / "fx")
    emb = HashingEmbedder()
    indexer = RepoIndexer(sessionmaker, embedder=emb, config=RepoIntelConfig(embed_batch_size=4))
    stats = await indexer.index(IndexTarget(root=repo, repository_key=_key()))
    assert stats.embedded == stats.chunks
    assert emb.calls and max(emb.calls) <= 4 and sum(emb.calls) == stats.chunks
