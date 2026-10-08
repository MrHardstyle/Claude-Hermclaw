"""P15 regression tests for the scope-engine review fixes (real PostgreSQL + real git workspaces)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeExpansionRequest
from hermclaw.core.errors import ConflictError
from hermclaw.core.interfaces import GitStatusEntry, RepoHit, WorkspaceHandle
from hermclaw.persistence.models import Step
from hermclaw.scope.audit import ScopeAuditor
from hermclaw.scope.engine import ScopeEngine, ScopeEngineSettings
from hermclaw.scope.expansion import ScopeExpansionHandler
from hermclaw.scope.guard import ScopeGuard
from tests.integration.test_scope_support import (
    FakeRepo,
    build_repo,
    events_for,
    git,
    hit,
    make_config,
    make_step,
    reload_step,
    scope_rows,
    workspace_for,
    write,
)

pytestmark = pytest.mark.integration
SM = async_sessionmaker[AsyncSession]
WHY = "needed to finish the step goal"


@pytest.fixture
def repo(tmp_repo: Path) -> Path:
    return build_repo(tmp_repo)


def _req(*paths: str, ops: list[str] | None = None) -> ScopeExpansionRequest:
    return ScopeExpansionRequest.model_validate({"paths": list(paths), "operations": ops or ["modify"], "justification": WHY})


# ============================================================================================= engine
async def test_location_suffixes_root_and_implausible_hints(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(
        sessionmaker,
        repo_hints=[".", "src/app/core.py:5", "tests/test_core.py::test_run", "docs/guide.md#L1-L3", "src/app/new.py::Thing"],
    )
    d = await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, workspace_for(step.job_id, repo))
    assert d.contract is not None
    assert d.contract.target_paths == ["src/app/core.py", "tests/test_core.py", "docs/guide.md"]
    assert d.contract.allowed_new_paths == []  # neither "." nor "src/app/new.py::Thing" may become creatable
    statuses = {h["hint"]: h["status"] for h in d.evidence["hints"]}
    assert statuses["."] == "invalid"
    assert statuses["src/app/core.py"] == "resolved" and statuses["tests/test_core.py"] == "resolved"
    excluded = {e["path"]: e["reason"] for e in d.evidence["excluded"]}
    assert "not a plausible file path" in excluded["src/app/new.py::Thing"]


async def test_unbounded_creation_patterns_are_excluded(sessionmaker: SM, repo: Path) -> None:
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    step = await make_step(sessionmaker, repo_hints=[], allowed_new_paths=["**", "**/*.py", "*/tests/", "src/app/feature/", "*.md"])
    d = await engine.create_scope(step.id, workspace_for(step.job_id, repo))
    assert d.contract is not None and d.contract.allowed_new_paths == ["src/app/feature/", "*.md"]
    excluded = {e["path"] for e in d.evidence["excluded"] if e["reason"].startswith("unbounded")}
    assert excluded == {"**", "**/*.py", "*/tests/"}
    guard = ScopeGuard(d.contract, make_config().policies.scope)
    assert guard.allowed("src/app/feature/x.py", "create") and not guard.allowed("lib/anything.py", "create")

    only_unbounded = await make_step(sessionmaker, repo_hints=[], allowed_new_paths=["**/*"])
    d2 = await engine.create_scope(only_unbounded.id, workspace_for(only_unbounded.job_id, repo))
    assert d2.status == "unavailable" and d2.reason_code == "no_resolvable_scope"


@dataclass
class _ClosingRepo(FakeRepo):
    """Closes the step (as a concurrent replanner would) while the engine waits for repository intelligence."""

    sm: SM | None = None
    step_id: uuid.UUID | None = None

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]:
        assert self.sm is not None
        async with self.sm() as s, s.begin():
            await s.execute(update(Step).where(Step.id == self.step_id).values(superseded=True))
        return [hit("src/app/helpers.py", 0.95)]


async def test_step_closed_during_generation_writes_no_scope(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["slug"])
    fake = _ClosingRepo(sm=sessionmaker, step_id=step.id)
    with pytest.raises(ConflictError) as exc:
        await ScopeEngine(sessionmaker, make_config(), fake).create_scope(step.id, workspace_for(step.job_id, repo))
    assert exc.value.code == "scope_step_closed"
    assert await scope_rows(sessionmaker, step.id) == []
    assert await events_for(sessionmaker, step.id) == []
    assert (await reload_step(sessionmaker, step.id)).current_scope_version is None


@dataclass
class _HangingRepo(FakeRepo):
    delay: float = 30.0
    started: list[str] = field(default_factory=list)

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]:
        self.started.append(name)
        await asyncio.sleep(self.delay)
        return []

    async def search(self, workspace: WorkspaceHandle, query: str, *, k: int = 20) -> list[RepoHit]:
        self.started.append(query)
        await asyncio.sleep(self.delay)
        return []

    async def read(self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000) -> str:
        await asyncio.sleep(self.delay)
        return ""


async def test_hung_repository_intelligence_times_out_as_evidence(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["SomeSymbol", "user login flow"], allowed_new_paths=["docs/new.md"])
    fake = _HangingRepo()
    engine = ScopeEngine(sessionmaker, make_config(), fake, settings=ScopeEngineSettings(repo_timeout_seconds=0.05))
    d = await asyncio.wait_for(engine.create_scope(step.id, workspace_for(step.job_id, repo)), 10)
    assert d.status == "active" and d.contract is not None and d.contract.allowed_new_paths == ["docs/new.md"]
    records = {h["hint"]: h for h in d.evidence["hints"]}
    assert records["SomeSymbol"]["status"] == "error" and "timed out" in records["SomeSymbol"]["reason"]
    assert records["user login flow"]["status"] == "error"


async def test_unavailable_version_is_superseded_with_provenance(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["src/app/feat_*.py"])
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    ws = workspace_for(step.job_id, repo)
    d1 = await engine.create_scope(step.id, ws)
    assert d1.status == "unavailable"

    write(repo, "src/app/feat_login.py", "def login():\n    return True\n")  # e.g. a prerequisite step created it
    d2 = await engine.create_scope(step.id, ws)
    assert d2.status == "active" and d2.contract is not None and d2.contract.target_paths == ["src/app/feat_login.py"]
    d3 = await engine.create_scope(step.id, ws)
    assert d3.version == 3

    rows = await scope_rows(sessionmaker, step.id)
    assert [(r.version, r.status) for r in rows] == [(1, "superseded"), (2, "superseded"), (3, "active")]
    assert rows[0].evidence["superseded"]["previous_status"] == "unavailable" and rows[0].evidence["superseded"]["by_version"] == 2
    assert rows[0].evidence["unavailable_reason"]["code"] == "no_resolvable_scope"  # evidence preserved
    assert rows[1].evidence["superseded"]["previous_status"] == "active" and rows[1].evidence["superseded"]["by_version"] == 3
    assert "superseded" not in rows[2].evidence


async def test_remove_from_constraint_does_not_grant_delete(sessionmaker: SM, repo: Path) -> None:
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    edit = await make_step(sessionmaker, repo_hints=["src/app/core.py"], constraints=["remove src/app/helpers.py from the build manifest"])
    d1 = await engine.create_scope(edit.id, workspace_for(edit.job_id, repo))
    assert d1.contract is not None and "delete" not in d1.contract.allowed_operations
    assert d1.contract.target_paths == ["src/app/core.py"]

    drop = await make_step(sessionmaker, repo_hints=["src/app/core.py"], constraints=["remove legacy/old_module.py from the repository"])
    d2 = await engine.create_scope(drop.id, workspace_for(drop.job_id, repo))
    assert d2.contract is not None and "delete" in d2.contract.allowed_operations
    assert d2.evidence["delete_paths"] == [{"path": "legacy/old_module.py", "sources": ["constraint:delete"]}]


# ============================================================================================= audit
async def _delete_scoped(sessionmaker: SM, repo: Path) -> tuple[Step, WorkspaceHandle]:
    step = await make_step(
        sessionmaker, repo_hints=["src/app/core.py"], acceptance=[{"type": "absence", "path_glob": "legacy/old_module.py"}]
    )
    ws = workspace_for(step.job_id, repo)
    d = await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    assert d.contract is not None and "delete" in d.contract.allowed_operations
    return step, ws


async def test_delete_is_limited_to_designated_paths(sessionmaker: SM, repo: Path) -> None:
    step, ws = await _delete_scoped(sessionmaker, repo)
    auditor = ScopeAuditor(sessionmaker, make_config())
    report = await auditor.audit_changes(step.id, [("legacy/old_module.py", "delete"), ("src/app/core.py", "delete")])
    assert report.allowed == [("legacy/old_module.py", "delete")]
    assert [(v["path"], v["operation"]) for v in report.violations] == [("src/app/core.py", "delete")]
    assert "not designated" in report.violations[0]["reason"]
    assert len(await events_for(sessionmaker, step.id, EventType.SCOPE_VIOLATION)) == 1

    # a mechanical expansion keeps the designation (expansions never grant deletions themselves)
    granted = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, _req("tests/test_core.py"), ws)
    assert granted.granted and granted.version == 2
    rows = await scope_rows(sessionmaker, step.id)
    assert [d["path"] for d in rows[1].evidence["delete_paths"]] == ["legacy/old_module.py"]
    after = await auditor.audit_changes(step.id, [("legacy/old_module.py", "delete"), ("tests/test_core.py", "delete")])
    assert after.scope_version == 2 and after.allowed == [("legacy/old_module.py", "delete")]
    assert [v["path"] for v in after.violations] == ["tests/test_core.py"]


async def test_rename_without_origin_fails_closed(sessionmaker: SM, repo: Path) -> None:
    step, _ = await _delete_scoped(sessionmaker, repo)
    # a real staged rename of a forbidden file; hermclaw.gitops.reader.GitReader.status reports only the new path
    git(repo, "mv", ".env", "src/app/core_env.py")
    out = git(repo, "status", "--porcelain", "--no-renames", "--untracked-files=all")
    assert "D  .env" in out  # what actually happened: the forbidden .env was deleted
    entries = [GitStatusEntry("src/app/core_env.py", "R ")]
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(step.id, entries)
    assert not report.ok and report.checked == 2
    reasons = {(v["path"], v["operation"]): v["reason"] for v in report.violations}
    assert "without a known origin" in reasons[("src/app/core_env.py", "delete")]
    assert ("src/app/core_env.py", "create") in reasons  # creating outside allowed_new_paths is a violation too
    ev = await events_for(sessionmaker, step.id, EventType.SCOPE_VIOLATION)
    assert ev[-1].payload["violation_count"] == 2


async def test_rename_with_arrow_origin_audits_the_source_deletion(sessionmaker: SM, repo: Path) -> None:
    step, _ = await _delete_scoped(sessionmaker, repo)
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(
        step.id, [GitStatusEntry("legacy/old_module.py -> src/app/core.py", "R ")]
    )
    # the source deletion is audited (designated legacy file is allowed); the scope grants no "create" at all
    assert report.checked == 2 and report.allowed == [("legacy/old_module.py", "delete")]
    assert [(v["path"], v["operation"]) for v in report.violations] == [("src/app/core.py", "create")]
    assert "operation 'create' not allowed" in report.violations[0]["reason"]

    other = await ScopeAuditor(sessionmaker, make_config()).audit_status(
        step.id, [GitStatusEntry("src/app/models.py -> src/app/core.py", "R ")]
    )
    violating = {(v["path"], v["operation"]) for v in other.violations}
    assert ("src/app/models.py", "delete") in violating  # not a target: the rename source is never silently lost


# ============================================================================================= expansion
async def test_expansion_for_closed_step_is_rejected(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["src/app/core.py"])
    ws = workspace_for(step.job_id, repo)
    await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    async with sessionmaker() as s, s.begin():
        await s.execute(update(Step).where(Step.id == step.id).values(status="completed"))
    d = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, _req("tests/test_core.py"), ws)
    assert d.outcome == "rejected" and d.reason_code == "step_closed" and not d.changed
    rows = await scope_rows(sessionmaker, step.id)
    assert [(r.version, r.status) for r in rows] == [(1, "active")]
    assert rows[0].evidence["expansion_requests"][-1]["reason_code"] == "step_closed"
    requested = await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANSION_REQUESTED)
    assert len(requested) == 1 and requested[0].severity == "warning"
    assert await events_for(sessionmaker, step.id, EventType.SCOPE_EXPANDED) == []


async def test_bracket_file_names_are_granted_as_literal_files(sessionmaker: SM, repo: Path) -> None:
    write(repo, "src/web/[id].ts", "export const id = 1;\n")
    write(repo, "src/web/i.ts", "export const i = 1;\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "routes")
    step = await make_step(sessionmaker, repo_hints=["src/web/index.ts"])
    ws = workspace_for(step.job_id, repo)
    await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    handler = ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo())

    wildcard = await handler.handle(step.id, _req("src/web/*.ts"), ws)
    assert wildcard.outcome == "rejected" and wildcard.reason_code == "invalid_paths"

    d = await handler.handle(step.id, _req("src/web/[id].ts"), ws)
    assert d.granted and d.classification == "mechanical" and d.contract is not None
    assert d.paths[0].signals.get("same_directory_as") == "src/web/index.ts"
    assert "src/web/[[]id].ts" in d.contract.target_paths
    guard = ScopeGuard(d.contract, make_config().policies.scope)
    assert guard.allowed("src/web/[id].ts", "modify")
    assert not guard.allowed("src/web/i.ts", "modify") and not guard.allowed("src/web/d.ts", "modify")

    again = await handler.handle(step.id, _req("src/web/[id].ts"), ws)
    assert again.granted and not again.changed and again.reason_code == "already_in_scope"


async def test_escaped_scope_files_count_for_classification(sessionmaker: SM, repo: Path) -> None:
    write(repo, "pages/[slug].ts", "import { helper } from './helper';\nexport default helper;\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pages")
    step = await make_step(sessionmaker, repo_hints=["pages/[slug].ts"])
    ws = workspace_for(step.job_id, repo)
    d0 = await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    assert d0.contract is not None and d0.contract.target_paths == ["pages/[[]slug].ts"]
    d = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, _req("pages/helper.ts", ops=["create"]), ws)
    assert d.granted and d.paths[0].signals.get("imported_by") == "pages/[slug].ts"


async def test_hung_reads_do_not_block_expansion(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["src/app/core.py"])
    ws = workspace_for(step.job_id, repo)
    await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    handler = ScopeExpansionHandler(sessionmaker, make_config(), _HangingRepo(), settings=ScopeEngineSettings(repo_timeout_seconds=0.05))
    # src/lib/util.py is only related through import evidence, which cannot be read in time -> semantic
    d = await asyncio.wait_for(handler.handle(step.id, _req("src/lib/util.py"), ws), 10)
    assert d.needs_replan and d.reason_code == "semantic_expansion"


async def test_implausible_expansion_paths_are_rejected(sessionmaker: SM, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["src/app/core.py"])
    ws = workspace_for(step.job_id, repo)
    await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    d = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, _req("src/app/new.py:12", ops=["create"]), ws)
    assert d.outcome == "rejected" and d.reason_code == "invalid_paths"
    assert "not a plausible file path" in d.paths[0].reason
    assert [r.version for r in await scope_rows(sessionmaker, step.id)] == [1]


async def test_engine_works_with_expire_on_commit_sessions(engine: AsyncEngine, sessionmaker: SM, repo: Path) -> None:
    expiring: SM = async_sessionmaker(engine, expire_on_commit=True, class_=AsyncSession)
    step = await make_step(sessionmaker, repo_hints=["src/app/core.py"])
    ws = workspace_for(step.job_id, repo)
    d = await ScopeEngine(expiring, make_config(), FakeRepo()).create_scope(step.id, ws)
    assert d.status == "active" and d.evidence["targets"][0]["path"] == "src/app/core.py"
    granted = await ScopeExpansionHandler(expiring, make_config(), FakeRepo()).handle(step.id, _req("tests/test_core.py"), ws)
    assert granted.granted and granted.version == 2
    report = await ScopeAuditor(expiring, make_config()).audit_changes(step.id, [("tests/test_core.py", "modify")])
    assert report.ok and report.scope_version == 2
