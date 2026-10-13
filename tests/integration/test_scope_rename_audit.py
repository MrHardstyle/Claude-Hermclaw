"""Scope audit of real staged git renames read through the real ``WorkspaceGitReader`` (P15 15.9, real PostgreSQL).

A rename is judged by both of its paths: the origin must be a deletable target of the active scope version and the
destination must be creatable. Before ``GitStatusEntry.orig_path`` existed the reader dropped the origin, so every
staged rename was reported as an unexplained change ("without a known origin").
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.interfaces import GitStatusEntry, WorkspaceHandle
from hermclaw.gitops.reader import WorkspaceGitReader
from hermclaw.gitops.runner import GitRunner
from hermclaw.persistence.models import Step
from hermclaw.scope.audit import ScopeAuditor
from hermclaw.scope.engine import ScopeEngine
from tests.integration.test_scope_support import FakeRepo, build_repo, events_for, git, make_config, make_step, workspace_for

pytestmark = pytest.mark.integration
SM = async_sessionmaker[AsyncSession]


@pytest.fixture
def repo(tmp_repo: Path) -> Path:
    return build_repo(tmp_repo)


async def _rename_scope(sessionmaker: SM, repo: Path) -> tuple[Step, WorkspaceHandle]:
    """Scope: modify core.py, delete legacy/old_module.py (absence evidence), create below src/app/feature/."""
    step = await make_step(
        sessionmaker,
        repo_hints=["src/app/core.py"],
        allowed_new_paths=["src/app/feature/"],
        acceptance=[{"type": "absence", "path_glob": "legacy/old_module.py"}],
    )
    ws = workspace_for(step.job_id, repo)
    d = await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    assert d.contract is not None
    assert {"create", "delete"} <= set(d.contract.allowed_operations)
    assert "src/app/feature/" in d.contract.allowed_new_paths
    return step, ws


def _mv(repo: Path, src: str, dst: str) -> None:
    """A real staged rename (``git mv``), creating the destination directory first."""
    (repo / dst).parent.mkdir(parents=True, exist_ok=True)
    git(repo, "mv", src, dst)


async def _status(ws: WorkspaceHandle) -> list[GitStatusEntry]:
    return await WorkspaceGitReader(GitRunner(author_name="t", author_email="t@x")).status(ws)


async def test_reader_reports_the_origin_of_a_staged_rename(sessionmaker: SM, repo: Path) -> None:
    ws = workspace_for((await make_step(sessionmaker)).job_id, repo)
    _mv(repo, "legacy/old_module.py", "src/app/feature/old_module.py")
    entries = await _status(ws)
    renamed = [e for e in entries if e.status.startswith("R")]
    assert renamed == [GitStatusEntry(path="src/app/feature/old_module.py", status="R ", orig_path="legacy/old_module.py")]
    assert all(e.orig_path is None for e in entries if not e.status.startswith(("R", "C")))


async def test_permitted_rename_passes_the_audit(sessionmaker: SM, repo: Path) -> None:
    step, ws = await _rename_scope(sessionmaker, repo)
    _mv(repo, "legacy/old_module.py", "src/app/feature/old_module.py")
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(step.id, [e for e in await _status(ws) if e.status != "??"])
    assert report.violations == []
    assert report.ok and report.checked == 2
    assert report.allowed == [("legacy/old_module.py", "delete"), ("src/app/feature/old_module.py", "create")]
    assert await events_for(sessionmaker, step.id, EventType.SCOPE_VIOLATION) == []


async def test_rename_of_a_forbidden_origin_is_a_violation(sessionmaker: SM, repo: Path) -> None:
    step, ws = await _rename_scope(sessionmaker, repo)
    _mv(repo, ".env", "src/app/feature/env.py")
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(step.id, [e for e in await _status(ws) if e.status != "??"])
    assert [(v["path"], v["operation"]) for v in report.violations] == [(".env", "delete")]
    assert "forbidden" in report.violations[0]["reason"]
    assert report.allowed == [("src/app/feature/env.py", "create")]
    assert "known origin" not in str(report.violations)
    ev = await events_for(sessionmaker, step.id, EventType.SCOPE_VIOLATION)
    assert len(ev) == 1 and ev[0].payload["violations"][0]["path"] == ".env"


async def test_rename_of_a_target_that_is_not_designated_for_deletion_is_a_violation(sessionmaker: SM, repo: Path) -> None:
    step, ws = await _rename_scope(sessionmaker, repo)
    _mv(repo, "src/app/core.py", "src/app/feature/core.py")  # a target may be modified, not deleted
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(step.id, [e for e in await _status(ws) if e.status != "??"])
    assert [(v["path"], v["operation"]) for v in report.violations] == [("src/app/core.py", "delete")]
    assert "not designated" in report.violations[0]["reason"]
    assert report.allowed == [("src/app/feature/core.py", "create")]


async def test_rename_to_a_destination_outside_the_scope_is_a_violation(sessionmaker: SM, repo: Path) -> None:
    step, ws = await _rename_scope(sessionmaker, repo)
    _mv(repo, "legacy/old_module.py", "docs/old_module.py")
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(step.id, [e for e in await _status(ws) if e.status != "??"])
    assert report.allowed == [("legacy/old_module.py", "delete")]
    assert [(v["path"], v["operation"]) for v in report.violations] == [("docs/old_module.py", "create")]
    assert "outside allowed_new_paths" in report.violations[0]["reason"]


async def test_rename_with_worktree_changes_is_still_judged_by_both_paths(sessionmaker: SM, repo: Path) -> None:
    step, ws = await _rename_scope(sessionmaker, repo)
    _mv(repo, "legacy/old_module.py", "src/app/feature/old_module.py")
    (repo / "src/app/feature/old_module.py").write_text("OLD = 2\n", encoding="utf-8")
    (repo / "src/app/core.py").write_text("print('changed')\n", encoding="utf-8")
    entries = [e for e in await _status(ws) if e.status != "??"]
    assert {(e.path, e.status, e.orig_path) for e in entries} == {
        ("src/app/feature/old_module.py", "RM", "legacy/old_module.py"),
        ("src/app/core.py", " M", None),
    }
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(step.id, entries)
    assert report.ok, report.violations
    assert sorted(report.allowed) == [
        ("legacy/old_module.py", "delete"),
        ("src/app/core.py", "modify"),
        ("src/app/feature/old_module.py", "create"),
    ]
