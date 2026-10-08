"""P15 15.7 scope expansion against real PostgreSQL + real git workspaces."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeExpansionRequest
from hermclaw.core.config import HermclawConfig
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.persistence.models import Step
from hermclaw.scope.engine import ScopeDecision, ScopeEngine, ScopeEngineSettings
from hermclaw.scope.expansion import ScopeExpansionHandler
from tests.integration.test_scope_support import (
    SM,
    FakeRepo,
    build_repo,
    events_for,
    make_config,
    make_step,
    reload_step,
    scope_rows,
    workspace_for,
    write,
)

pytestmark = pytest.mark.integration
WHY = "needed to finish the step goal"


@pytest.fixture
def repo(tmp_repo: Path) -> Path:
    return build_repo(tmp_repo)


async def _scoped_step(
    sessionmaker: SM, repo: Path, *, cfg: HermclawConfig | None = None, **fields: Any
) -> tuple[Step, WorkspaceHandle, ScopeDecision]:
    cfg = cfg or make_config()
    fields.setdefault("repo_hints", ["src/app/core.py"])
    step = await make_step(sessionmaker, **fields)
    ws = workspace_for(step.job_id, repo)
    decision = await ScopeEngine(sessionmaker, cfg, FakeRepo()).create_scope(step.id, ws)
    return step, ws, decision


def _req(*paths: str, ops: list[str] | None = None) -> ScopeExpansionRequest:
    return ScopeExpansionRequest(paths=list(paths), operations=ops or ["modify"], justification=WHY)


async def test_mechanical_test_of_target_is_granted(sessionmaker: SM, repo: Path) -> None:
    step, ws, d = await _scoped_step(sessionmaker, repo)
    assert d.version == 1
    attempt = uuid.uuid4()
    fake = FakeRepo()
    handler = ScopeExpansionHandler(sessionmaker, make_config(), fake)
    decision = await handler.handle(step.id, _req("tests/test_core.py"), ws, attempt_id=attempt)

    assert decision.granted and decision.changed and decision.classification == "mechanical"
    assert decision.version == 2 and decision.previous_version == 1
    assert decision.contract is not None and decision.contract.source == "runtime_expansion"
    assert decision.contract.target_paths == ["src/app/core.py", "tests/test_core.py"]
    assert decision.paths[0].signals == {"test_of": "src/app/core.py"}

    rows = await scope_rows(sessionmaker, step.id)
    assert [(r.version, r.status, r.contract["source"]) for r in rows] == [
        (1, "superseded", "planner_and_repo_intelligence"),
        (2, "active", "runtime_expansion"),
    ]
    assert rows[1].evidence["expanded_from"] == 1 and rows[1].evidence["added"]["target_paths"] == ["tests/test_core.py"]
    assert rows[0].evidence["expansion_requests"][0]["granted_version"] == 2
    assert (await reload_step(sessionmaker, step.id)).current_scope_version == 2

    requested = await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANSION_REQUESTED)
    expanded = await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANDED)
    assert len(requested) == 1 and requested[0].attempt_id == attempt and requested[0].payload["outcome"] == "granted"
    assert len(expanded) == 1 and expanded[0].payload["version"] == 2 and expanded[0].payload["previous_version"] == 1

    guard = await ScopeEngine(sessionmaker, make_config(), fake).guard_for(step.id)
    assert guard.allowed("tests/test_core.py", "modify")


async def test_mechanical_import_dependency_is_granted(sessionmaker: SM, repo: Path) -> None:
    step, ws, _ = await _scoped_step(sessionmaker, repo, repo_hints=["tests/test_core.py"])
    fake = FakeRepo()
    decision = await ScopeExpansionHandler(sessionmaker, make_config(), fake).handle(step.id, _req("src/app/core.py"), ws)
    assert decision.granted and decision.classification == "mechanical"
    assert decision.paths[0].signals["imported_by"] == "tests/test_core.py"
    assert "from app.core import run" in decision.paths[0].signals["evidence"]
    assert ("read", "tests/test_core.py") in fake.calls  # evidence comes from the RepoContextProvider


async def test_mechanical_same_directory_create(sessionmaker: SM, repo: Path) -> None:
    step, ws, _ = await _scoped_step(sessionmaker, repo)
    decision = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, _req("src/app/extra.py"), ws)
    assert decision.granted and decision.contract is not None
    assert decision.paths[0].operation == "create" and "treated as create" in decision.paths[0].reason
    assert decision.paths[0].signals == {"same_directory_as": "src/app/core.py", "language": "python"}
    assert decision.contract.allowed_new_paths == ["src/app/extra.py"]
    assert decision.contract.allowed_operations == ["create", "modify"]


async def test_semantic_expansion_needs_replan(sessionmaker: SM, repo: Path) -> None:
    step, ws, _ = await _scoped_step(sessionmaker, repo)
    handler = ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo())
    for paths in (("src/lib/util.py",), ("tests/test_misc.py",), ("src/app/helpers.py", "src/lib/util.py")):
        decision = await handler.handle(step.id, _req(*paths), ws)
        assert decision.needs_replan and not decision.changed, paths
        assert decision.reason_code == "semantic_expansion" and decision.classification == "semantic"
        assert decision.version == 1 and decision.contract is not None and decision.contract.version == 1
    rows = await scope_rows(sessionmaker, step.id)
    assert [(r.version, r.status) for r in rows] == [(1, "active")]
    assert [r["outcome"] for r in rows[0].evidence["expansion_requests"]] == ["needs_replan"] * 3
    assert len(await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANSION_REQUESTED)) == 3
    assert await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANDED) == []


async def test_delete_is_always_semantic(sessionmaker: SM, repo: Path) -> None:
    step, ws, _ = await _scoped_step(sessionmaker, repo)
    decision = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(
        step.id, _req("src/app/helpers.py", ops=["delete"]), ws
    )
    assert decision.needs_replan and decision.paths[0].operation == "delete"
    assert "semantic" in decision.paths[0].reason


@pytest.mark.parametrize(
    "path,code",
    [
        (".env", "forbidden_paths"),
        ("src/*.py", "invalid_paths"),
        ("../outside.py", "invalid_paths"),
        ("src/app", "invalid_paths"),
        ("build/out.js", "invalid_paths"),  # exists on disk but git-ignored
        ("src/app/core.py/inner.py", "invalid_paths"),
    ],
)
async def test_invalid_or_forbidden_paths_are_rejected(sessionmaker: SM, repo: Path, path: str, code: str) -> None:
    step, ws, _ = await _scoped_step(sessionmaker, repo)
    decision = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, _req(path, "src/app/x.py"), ws)
    assert decision.outcome == "rejected" and decision.reason_code == code
    assert [(r.version, r.status) for r in await scope_rows(sessionmaker, step.id)] == [(1, "active")]
    ev = await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANSION_REQUESTED)
    assert len(ev) == 1 and ev[0].severity == "warning" and ev[0].payload["outcome"] == "rejected"


async def test_step_forbidden_path_is_rejected(sessionmaker: SM, repo: Path) -> None:
    step, ws, _ = await _scoped_step(sessionmaker, repo, forbidden_paths=["src/app/helpers.py"])
    decision = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, _req("src/app/helpers.py"), ws)
    assert decision.outcome == "rejected" and decision.reason_code == "forbidden_paths"


async def test_already_in_scope_is_a_noop_grant(sessionmaker: SM, repo: Path) -> None:
    step, ws, _ = await _scoped_step(sessionmaker, repo)
    decision = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, _req("src/app/core.py"), ws)
    assert decision.granted and not decision.changed and decision.reason_code == "already_in_scope" and decision.version == 1
    assert len(await scope_rows(sessionmaker, step.id)) == 1
    assert len(await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANSION_REQUESTED)) == 1


async def test_expansion_limit_per_step(sessionmaker: SM, repo: Path) -> None:
    step, ws, _ = await _scoped_step(sessionmaker, repo)
    handler = ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo(), settings=ScopeEngineSettings(max_expansions_per_step=3))
    for i in range(3):
        d = await handler.handle(step.id, _req(f"src/app/extra_{i}.py"), ws)
        assert d.granted and d.version == i + 2
    d4 = await handler.handle(step.id, _req("src/app/extra_3.py"), ws)
    assert d4.needs_replan and d4.reason_code == "expansion_limit_reached" and d4.version == 4
    rows = await scope_rows(sessionmaker, step.id)
    assert [r.status for r in rows] == ["superseded", "superseded", "superseded", "active"]
    assert rows[-1].contract["allowed_new_paths"] == ["src/app/extra_0.py", "src/app/extra_1.py", "src/app/extra_2.py"]
    assert len(await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANDED)) == 3
    assert len(await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANSION_REQUESTED)) == 4


async def test_expansion_respects_policy_caps(sessionmaker: SM, repo: Path) -> None:
    cfg = make_config(max_new_paths=1)
    step, ws, _ = await _scoped_step(sessionmaker, repo, cfg=cfg, allowed_new_paths=["src/app/one.py"])
    decision = await ScopeExpansionHandler(sessionmaker, cfg, FakeRepo()).handle(step.id, _req("src/app/two.py"), ws)
    assert decision.needs_replan and decision.reason_code == "scope_caps_exceeded"


async def test_no_active_scope_is_rejected(sessionmaker: SM, repo: Path) -> None:
    handler = ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo())
    bare = await make_step(sessionmaker, repo_hints=["README.md"])
    d = await handler.handle(bare.id, _req("src/app/core.py"), workspace_for(bare.job_id, repo))
    assert d.outcome == "rejected" and d.reason_code == "no_active_scope" and d.contract is None
    assert len(await events_for(sessionmaker, bare.id, EventType.SCOPE_EXPANSION_REQUESTED)) == 1

    step, ws, decision = await _scoped_step(sessionmaker, repo, repo_hints=["nothing/here/*.py"])
    assert decision.status == "unavailable"
    d2 = await handler.handle(step.id, _req("src/app/core.py"), ws)
    assert d2.outcome == "rejected" and "unavailable" in d2.reason
    rows = await scope_rows(sessionmaker, step.id)
    assert [(r.version, r.status) for r in rows] == [(1, "unavailable")]
    assert rows[0].evidence["expansion_requests"][0]["outcome"] == "rejected"


async def test_test_importing_target_is_mechanical(sessionmaker: SM, repo: Path) -> None:
    write(repo, "tests/integration/check_flow.py", "from app.core import run\n")
    step, ws, _ = await _scoped_step(sessionmaker, repo)
    decision = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(
        step.id, _req("tests/integration/check_flow.py"), ws
    )
    assert decision.granted and decision.paths[0].signals["test_imports"] == "src/app/core.py"


async def test_justification_secrets_are_redacted(sessionmaker: SM, repo: Path) -> None:
    step, ws, _ = await _scoped_step(sessionmaker, repo)
    req = ScopeExpansionRequest(paths=["src/lib/util.py"], justification="needs token=supersecretvalue123 to call api")
    await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, req, ws)
    rows = await scope_rows(sessionmaker, step.id)
    ev = await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANSION_REQUESTED)
    assert "supersecretvalue123" not in str(rows[0].evidence) and "supersecretvalue123" not in str(ev[0].payload)
