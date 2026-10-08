"""P15 failure behaviour: provider outages, missing workspaces, listing timeouts, concurrency, corrupt step data."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeExpansionRequest
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.scope.engine import ScopeEngine, ScopeEngineError, ScopeEngineSettings
from hermclaw.scope.expansion import ScopeExpansionHandler
from tests.integration.test_scope_support import (
    SM,
    FakeRepo,
    build_repo,
    events_for,
    hit,
    make_config,
    make_step,
    reload_step,
    scope_rows,
    workspace_for,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def repo(tmp_repo: Path) -> Path:
    return build_repo(tmp_repo)


class UnreadableRepo(FakeRepo):
    async def read(self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000) -> str:
        raise OSError("index unavailable")


async def test_repo_intelligence_outage_is_evidence_not_a_crash(sessionmaker: SM, repo: Path) -> None:
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo(fail=True))
    only_symbols = await make_step(sessionmaker, repo_hints=["slug", "user model definition"])
    d = await engine.create_scope(only_symbols.id, workspace_for(only_symbols.job_id, repo))
    assert d.status == "unavailable" and d.reason_code == "no_resolvable_scope"
    assert {h["status"] for h in d.evidence["hints"]} == {"error"}
    assert "index offline" in d.evidence["hints"][0]["reason"]

    mixed = await make_step(sessionmaker, repo_hints=["slug", "src/app/core.py"])
    d2 = await engine.create_scope(mixed.id, workspace_for(mixed.job_id, repo))
    assert d2.status == "active" and d2.contract is not None and d2.contract.target_paths == ["src/app/core.py"]


async def test_missing_workspace_raises_and_writes_nothing(sessionmaker: SM, repo: Path, tmp_path: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["README.md"])
    ws = workspace_for(step.job_id, repo)
    gone = WorkspaceHandle(
        id=ws.id,
        job_id=ws.job_id,
        path=tmp_path / "vanished",
        branch=ws.branch,
        base_branch="main",
        base_sha=ws.base_sha,
        repository_key="x",
    )
    with pytest.raises(ScopeEngineError) as exc:
        await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, gone)
    assert exc.value.code == "scope_workspace_missing"
    assert await scope_rows(sessionmaker, step.id) == []
    assert (await reload_step(sessionmaker, step.id)).current_scope_version is None


async def test_listing_timeout_raises(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["README.md"])
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo(), settings=ScopeEngineSettings(list_timeout_seconds=0))
    with pytest.raises(ScopeEngineError) as exc:
        await engine.create_scope(step.id, workspace_for(step.job_id, repo))
    assert exc.value.code == "scope_listing_timeout"
    assert await scope_rows(sessionmaker, step.id) == []


async def test_concurrent_scope_generation_allocates_unique_versions(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["src/app/core.py"])
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    ws = workspace_for(step.job_id, repo)
    decisions = await asyncio.gather(*(engine.create_scope(step.id, ws) for _ in range(5)))
    assert sorted(d.version for d in decisions) == [1, 2, 3, 4, 5]
    rows = await scope_rows(sessionmaker, step.id)
    assert [r.status for r in rows].count("active") == 1 and rows[-1].status == "active"
    assert (await reload_step(sessionmaker, step.id)).current_scope_version == 5
    assert len(await events_for(sessionmaker, step.id, EventType.SCOPE_CREATED)) == 5


async def test_concurrent_expansions_build_on_each_other(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["src/app/core.py"])
    ws = workspace_for(step.job_id, repo)
    await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    handler = ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo())
    reqs = [ScopeExpansionRequest(paths=[p], justification="mechanical sibling file") for p in ("src/app/a.py", "src/app/b.py")]
    results = await asyncio.gather(*(handler.handle(step.id, r, ws) for r in reqs))
    assert all(r.granted and r.changed for r in results)
    assert sorted(r.version or 0 for r in results) == [2, 3]
    rows = await scope_rows(sessionmaker, step.id)
    assert [r.status for r in rows] == ["superseded", "superseded", "active"]
    assert sorted(rows[-1].contract["allowed_new_paths"]) == ["src/app/a.py", "src/app/b.py"]


async def test_unreadable_files_mean_no_import_evidence(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["tests/test_core.py"])
    ws = workspace_for(step.job_id, repo)
    await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    req = ScopeExpansionRequest(paths=["src/app/core.py"], justification="the test imports this module")
    decision = await ScopeExpansionHandler(sessionmaker, make_config(), UnreadableRepo()).handle(step.id, req, ws)
    assert decision.needs_replan and decision.reason_code == "semantic_expansion"


async def test_corrupt_step_fields_are_ignored(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(
        sessionmaker,
        repo_hints=[42, None, "src/app/core.py", {"x": 1}],
        allowed_new_paths=["/abs/path.py", "src/app/ok.py"],
        forbidden_paths=["../../etc", 5],
        acceptance=[{"type": "absence"}, "nonsense", {"type": "presence", "path_glob": "/abs"}],
        constraints=[None, 3, "delete"],
    )
    d = await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, workspace_for(step.job_id, repo))
    assert d.status == "active" and d.contract is not None
    assert d.contract.target_paths == ["src/app/core.py"] and d.contract.allowed_new_paths == ["src/app/ok.py"]
    assert d.evidence["invalid_allowed_new_paths"][0]["path"] == "/abs/path.py"
    assert d.evidence["forbidden"]["invalid"][0]["path"] == "../../etc"


async def test_symbol_hits_outside_workspace_are_dropped(sessionmaker: SM, repo: Path) -> None:
    fake = FakeRepo(symbols={"slug": [hit("/etc/passwd", 0.99), hit("vendor/lib.py", 0.99), hit("build/out.js", 0.99)]})
    step = await make_step(sessionmaker, repo_hints=["slug"])
    d = await ScopeEngine(sessionmaker, make_config(), fake).create_scope(step.id, workspace_for(step.job_id, repo))
    assert d.status == "unavailable"
    notes = {r["path"]: r.get("note") for r in d.evidence["hints"][0]["hits"]}
    assert notes == {"/etc/passwd": "invalid path", "vendor/lib.py": "not in workspace", "build/out.js": "not in workspace"}
