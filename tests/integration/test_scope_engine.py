"""P15 ScopeEngine against real PostgreSQL + real git workspaces (15.1-15.6, 15.8)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from hermclaw.contracts.events import EventType
from hermclaw.core.errors import ConflictError, NotFoundError
from hermclaw.scope.engine import ScopeEngine, ScopeEngineSettings
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
)

pytestmark = pytest.mark.integration


@pytest.fixture
def repo(tmp_repo: Path) -> Path:
    return build_repo(tmp_repo)


async def test_paths_globs_directories_and_symbols_resolve(sessionmaker, repo: Path) -> None:
    fake = FakeRepo(
        symbols={"slug": [hit("src/app/helpers.py", 0.97, symbol=1.0), hit("src/app/core.py", 0.4)]},
        texts={"user model definition": [hit("src/app/models.py", 0.93), hit("docs/guide.md", 0.5)]},
    )
    step = await make_step(
        sessionmaker,
        repo_hints=["src/app/core.py", "src/web/*.ts", "docs/", "slug", "user model definition", "src/app/core.py:3-5"],
        allowed_new_paths=["src/app/new_feature.py"],
    )
    engine = ScopeEngine(sessionmaker, make_config(), fake)
    decision = await engine.create_scope(step.id, workspace_for(step.job_id, repo))

    assert decision.status == "active" and decision.runnable and decision.version == 1
    c = decision.contract
    assert c is not None
    assert c.source == "planner_and_repo_intelligence" and c.strict_target_paths is True and c.version == 1
    assert c.target_paths == [
        "src/app/core.py",
        "src/web/fmt.ts",
        "src/web/index.ts",
        "docs/guide.md",
        "src/app/helpers.py",
        "src/app/models.py",
    ]
    assert "src/app/core.py" in c.target_paths and c.target_paths.count("src/app/core.py") == 1
    assert c.allowed_new_paths == ["src/app/new_feature.py"]
    assert c.allowed_operations == ["create", "modify"]
    assert ".git/**" in c.forbidden_paths and "**/.env" in c.forbidden_paths

    hints = {h["hint"]: h for h in decision.evidence["hints"]}
    assert hints["slug"]["kind"] == "symbol" and hints["slug"]["status"] == "resolved"
    scores = {r["path"]: r["score"] for r in hints["slug"]["hits"]}
    assert scores == {"src/app/helpers.py": 0.97, "src/app/core.py": 0.4}
    assert hints["user model definition"]["kind"] == "text"
    assert hints["docs/"]["kind"] == "directory" and hints["docs/"]["targets"] == ["docs/guide.md"]
    assert hints["src/app/core.py"]["status"] == "resolved"  # ":3-5" suffix stripped
    assert ("find_symbol", "slug") in fake.calls and ("search", "user model definition") in fake.calls

    rows = await scope_rows(sessionmaker, step.id)
    assert [(r.version, r.status) for r in rows] == [(1, "active")]
    assert rows[0].contract["target_paths"] == c.target_paths
    assert rows[0].evidence["workspace"]["listing"] == "git"
    assert (await reload_step(sessionmaker, step.id)).current_scope_version == 1
    created = await events_for(sessionmaker, step.id, EventType.SCOPE_CREATED)
    assert len(created) == 1 and created[0].payload["version"] == 1 and created[0].payload["target_count"] == 6


async def test_low_confidence_and_ambiguous_hits_are_not_targets(sessionmaker, repo: Path) -> None:
    fake = FakeRepo(
        symbols={"run": [hit("src/app/core.py", 0.5)]},
        texts={
            "helper functions": [hit(p, 0.95) for p in ("src/app/core.py", "src/app/helpers.py", "src/app/models.py", "src/lib/util.py")],
            "UnknownThing": [hit("src/lib/util.py", 0.8)],  # 0.8 passes the symbol but not the search threshold
        },
    )
    step = await make_step(sessionmaker, repo_hints=["run", "helper functions", "README.md", "UnknownThing"])
    engine = ScopeEngine(sessionmaker, make_config(), fake, settings=ScopeEngineSettings(max_files_per_hint=3))
    decision = await engine.create_scope(step.id, workspace_for(step.job_id, repo))
    assert decision.contract is not None
    assert decision.contract.target_paths == ["README.md"]
    hints = {h["hint"]: h for h in decision.evidence["hints"]}
    assert hints["run"]["status"] == "unresolved" and hints["run"]["hits"][0]["score"] == 0.5
    assert hints["helper functions"]["status"] == "ambiguous" and hints["helper functions"]["targets"] == []
    # symbol without symbol hits falls back to search with the stricter threshold
    assert ("find_symbol", "UnknownThing") in fake.calls and ("search", "UnknownThing") in fake.calls
    assert hints["UnknownThing"]["status"] == "unresolved" and "0.85" in hints["UnknownThing"]["reason"]
    assert ("search", "run") not in fake.calls


async def test_forbidden_paths_are_excluded_with_evidence(sessionmaker, repo: Path) -> None:
    step = await make_step(
        sessionmaker,
        repo_hints=["src/app/*.py", ".env", "config/settings.yaml"],
        forbidden_paths=["src/app/models.py", "config/"],
        allowed_new_paths=["secrets.pem", "src/app/extra.py"],
    )
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    decision = await engine.create_scope(step.id, workspace_for(step.job_id, repo))
    c = decision.contract
    assert c is not None
    assert c.target_paths == ["src/app/__init__.py", "src/app/core.py", "src/app/helpers.py"]
    assert c.allowed_new_paths == ["src/app/extra.py"]
    assert "src/app/models.py" in c.forbidden_paths and "config/" in c.forbidden_paths and "**/*.pem" in c.forbidden_paths
    excluded = {(e["path"], e["list"]) for e in decision.evidence["excluded"]}
    assert ("src/app/models.py", "target_paths") in excluded
    assert (".env", "target_paths") in excluded
    assert ("config/settings.yaml", "target_paths") in excluded
    assert ("secrets.pem", "allowed_new_paths") in excluded
    assert decision.evidence["forbidden"]["step"] == ["src/app/models.py", "config/"]


async def test_new_hinted_paths_only_for_creating_kinds(sessionmaker, repo: Path) -> None:
    hints = ["src/app/brand_new.py", "src/newpkg/", "src/app/core.py/inner.py"]
    impl = await make_step(sessionmaker, repo_hints=hints)
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    decision = await engine.create_scope(impl.id, workspace_for(impl.job_id, repo))
    assert decision.contract is not None
    assert decision.contract.allowed_new_paths == ["src/app/brand_new.py", "src/newpkg/"]
    assert decision.contract.allowed_operations == ["create"]
    statuses = {h["hint"]: (h["status"], h["reason"]) for h in decision.evidence["hints"]}
    assert statuses["src/app/core.py/inner.py"][0] == "unresolved" and "parent" in statuses["src/app/core.py/inner.py"][1]

    review = await make_step(sessionmaker, kind="review", capability="review", repo_hints=hints)
    ro = await engine.create_scope(review.id, workspace_for(review.job_id, repo))
    assert ro.status == "active" and ro.contract is not None
    assert ro.contract.allowed_operations == [] and ro.contract.target_paths == [] and ro.evidence["read_only"] is True


async def test_bare_file_name_resolves_unique_basename(sessionmaker, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["helpers.py", "test_core.py"])
    decision = await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, workspace_for(step.job_id, repo))
    assert decision.contract is not None
    assert decision.contract.target_paths == ["src/app/helpers.py", "tests/test_core.py"]


async def test_delete_only_from_absence_evidence_or_explicit_constraint(sessionmaker, repo: Path) -> None:
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    plain = await make_step(sessionmaker, repo_hints=["legacy/old_module.py"])
    d0 = await engine.create_scope(plain.id, workspace_for(plain.job_id, repo))
    assert d0.contract is not None and "delete" not in d0.contract.allowed_operations

    absent = await make_step(
        sessionmaker,
        repo_hints=["src/app/core.py"],
        acceptance=[
            {"type": "absence", "path_glob": "legacy/old*.py"},
            {"type": "absence", "path_glob": "src/app/core.py", "pattern": "TODO"},  # content absence: no delete
            {"type": "presence", "path_glob": "src/app/replacement.py"},
        ],
    )
    d1 = await engine.create_scope(absent.id, workspace_for(absent.job_id, repo))
    assert d1.contract is not None
    assert d1.contract.allowed_operations == ["create", "modify", "delete"]
    assert d1.contract.target_paths == ["src/app/core.py", "legacy/old_module.py", "legacy/older_module.py"]
    assert d1.contract.allowed_new_paths == ["src/app/replacement.py"]
    assert {d["path"] for d in d1.evidence["delete_paths"]} == {"legacy/old_module.py", "legacy/older_module.py"}

    constrained = await make_step(sessionmaker, repo_hints=["src/app/core.py"], constraints=["Delete legacy/old_module.py once unused"])
    d2 = await engine.create_scope(constrained.id, workspace_for(constrained.job_id, repo))
    assert d2.contract is not None and "delete" in d2.contract.allowed_operations
    assert d2.evidence["delete_paths"] == [{"path": "legacy/old_module.py", "sources": ["constraint:delete"]}]

    negated = await make_step(sessionmaker, repo_hints=["src/app/core.py"], constraints=["Do not delete legacy/old_module.py"])
    d3 = await engine.create_scope(negated.id, workspace_for(negated.job_id, repo))
    assert d3.contract is not None and "delete" not in d3.contract.allowed_operations

    forbidden_delete = await make_step(
        sessionmaker, repo_hints=["src/app/core.py"], constraints=["delete .env"], acceptance=[{"type": "absence", "path_glob": ".env"}]
    )
    d4 = await engine.create_scope(forbidden_delete.id, workspace_for(forbidden_delete.job_id, repo))
    assert d4.contract is not None and "delete" not in d4.contract.allowed_operations and ".env" not in d4.contract.target_paths


async def test_unavailable_when_nothing_resolves(sessionmaker, repo: Path) -> None:
    before_head = git(repo, "rev-parse", "HEAD")
    before_status = git(repo, "status", "--porcelain")
    step = await make_step(sessionmaker, repo_hints=["does/not/exist/*.py", "NoSuchSymbol"], kind="implement")
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    decision = await engine.create_scope(step.id, workspace_for(step.job_id, repo))

    assert decision.status == "unavailable" and not decision.runnable and decision.contract is None
    assert decision.reason_code == "no_resolvable_scope"
    assert {h["hint"] for h in decision.evidence["hints"]} == {"does/not/exist/*.py", "NoSuchSymbol"}
    rows = await scope_rows(sessionmaker, step.id)
    assert [(r.version, r.status) for r in rows] == [(1, "unavailable")]
    assert rows[0].contract["allowed_operations"] == [] and rows[0].reason == decision.reason
    assert rows[0].evidence["unavailable_reason"]["code"] == "no_resolvable_scope"
    ev = await events_for(sessionmaker, step.id, EventType.SCOPE_UNAVAILABLE)
    assert len(ev) == 1 and ev[0].severity == "warning" and ev[0].payload["reason_code"] == "no_resolvable_scope"
    assert not await events_for(sessionmaker, step.id, EventType.SCOPE_CREATED)
    # no current contract, deny-all guard, nothing restored/committed in the workspace
    assert await engine.current_contract(step.id) is None
    guard = await engine.guard_for(step.id)
    assert not guard.allowed("src/app/core.py", "modify")
    assert git(repo, "rev-parse", "HEAD") == before_head
    assert git(repo, "status", "--porcelain") == before_status


async def test_caps_are_never_truncated(sessionmaker, repo: Path) -> None:
    engine = ScopeEngine(sessionmaker, make_config(max_target_paths=3, max_new_paths=1), FakeRepo())
    many = await make_step(sessionmaker, repo_hints=["src/**/*.py"])
    d = await engine.create_scope(many.id, workspace_for(many.job_id, repo))
    assert d.status == "unavailable" and d.reason_code == "too_many_target_paths"
    assert d.evidence["caps"]["target_count"] == 5 and len(d.evidence["targets"]) == 5

    new = await make_step(sessionmaker, repo_hints=["README.md"], allowed_new_paths=["a.py", "b.py"])
    d2 = await engine.create_scope(new.id, workspace_for(new.job_id, repo))
    assert d2.status == "unavailable" and d2.reason_code == "too_many_new_paths"


async def test_versioning_supersedes_previous_active(sessionmaker, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["src/app/c*.py"])
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    ws = workspace_for(step.job_id, repo)
    d1 = await engine.create_scope(step.id, ws)
    d2 = await engine.create_scope(step.id, ws)
    assert (d1.version, d2.version) == (1, 2)
    assert d2.contract is not None and d2.contract.version == 2
    rows = await scope_rows(sessionmaker, step.id)
    assert [(r.version, r.status) for r in rows] == [(1, "superseded"), (2, "active")]
    assert (await reload_step(sessionmaker, step.id)).current_scope_version == 2
    current = await engine.current_contract(step.id)
    assert current is not None and current.version == 2

    # the glob no longer matches (file removed) -> v3 unavailable supersedes the active v2
    (repo / "src/app/core.py").unlink()
    d3 = await engine.create_scope(step.id, ws)
    assert d3.status == "unavailable" and d3.version == 3
    rows = await scope_rows(sessionmaker, step.id)
    assert [(r.version, r.status) for r in rows] == [(1, "superseded"), (2, "superseded"), (3, "unavailable")]
    assert await engine.current_contract(step.id) is None
    created = await events_for(sessionmaker, step.id, EventType.SCOPE_CREATED)
    assert [e.payload["previous_version"] for e in created] == [None, 1]


async def test_closed_missing_and_mismatched_steps(sessionmaker, repo: Path) -> None:
    engine = ScopeEngine(sessionmaker, make_config(), FakeRepo())
    with pytest.raises(NotFoundError):
        await engine.create_scope(uuid.uuid4(), workspace_for(uuid.uuid4(), repo))
    done = await make_step(sessionmaker, repo_hints=["README.md"], status="completed")
    with pytest.raises(ConflictError):
        await engine.create_scope(done.id, workspace_for(done.job_id, repo))
    old = await make_step(sessionmaker, repo_hints=["README.md"], superseded=True)
    with pytest.raises(ConflictError):
        await engine.create_scope(old.id, workspace_for(old.job_id, repo))
    other = await make_step(sessionmaker, repo_hints=["README.md"])
    with pytest.raises(ConflictError):
        await engine.create_scope(other.id, workspace_for(uuid.uuid4(), repo))
    assert await scope_rows(sessionmaker, other.id) == []


async def test_secrets_in_hints_are_redacted_in_evidence(sessionmaker, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["README.md", "password=hunter2secret"])
    decision = await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, workspace_for(step.job_id, repo))
    rows = await scope_rows(sessionmaker, step.id)
    dumped = str(rows[0].evidence)
    assert "hunter2secret" not in dumped and "hunter2secret" not in str(decision.evidence)
