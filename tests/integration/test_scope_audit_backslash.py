"""Scope audit of real file names that contain a backslash (BUG-057, P15 15.9, real git + PostgreSQL).

Git reports real file names. A shell command may create the root-level file ``src\\app\\core.py`` - a single file whose
name contains backslashes, not ``src/app/core.py``. Scope paths are POSIX and the path normalisation reads ``\\`` as a
separator (for contract/tool input), so the audit must never judge such a real name through that normalisation: it
is a violation (fail closed), reported under its literal name. gitops staging refuses these names as well.
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

CORE_LOOKALIKE = "src\\app\\core.py"  # one root-level file; its canonical look-alike src/app/core.py is a target
FEATURE_LOOKALIKE = "src\\app\\feature\\new.py"  # look-alike of a path below allowed_new_paths
LEGACY_LOOKALIKE = "legacy\\old_module.py"  # look-alike of the designated deletion legacy/old_module.py


@pytest.fixture
def repo(tmp_repo: Path) -> Path:
    return build_repo(tmp_repo)


async def _scoped(sessionmaker: SM, repo: Path) -> tuple[Step, WorkspaceHandle]:
    """Scope: modify src/app/core.py, delete legacy/old_module.py (absence evidence), create below src/app/feature/."""
    step = await make_step(
        sessionmaker,
        repo_hints=["src/app/core.py"],
        allowed_new_paths=["src/app/feature/"],
        acceptance=[{"type": "absence", "path_glob": "legacy/old_module.py"}],
    )
    ws = workspace_for(step.job_id, repo)
    d = await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    assert d.contract is not None
    assert "src/app/core.py" in d.contract.target_paths and "src/app/feature/" in d.contract.allowed_new_paths
    assert "delete" in d.contract.allowed_operations
    return step, ws


async def _status(ws: WorkspaceHandle) -> list[GitStatusEntry]:
    return await WorkspaceGitReader(GitRunner(author_name="t", author_email="t@x")).status(ws)


async def test_untracked_backslash_names_are_violations_under_their_literal_name(sessionmaker: SM, repo: Path) -> None:
    step, ws = await _scoped(sessionmaker, repo)
    (repo / CORE_LOOKALIKE).write_text("print('not the target')\n", encoding="utf-8")
    (repo / FEATURE_LOOKALIKE).write_text("NEW = 1\n", encoding="utf-8")
    (repo / "src/app/core.py").write_text("print('changed')\n", encoding="utf-8")  # a real, permitted change
    assert not (repo / "src/app/feature").exists()  # the look-alikes really are root-level files

    entries = [e for e in await _status(ws) if e.path != "notes/untracked.md"]  # build_repo's own untracked file
    assert {(e.path, e.status) for e in entries} == {
        (CORE_LOOKALIKE, "??"),
        (FEATURE_LOOKALIKE, "??"),
        ("src/app/core.py", " M"),
    }
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(step.id, entries)

    assert report.allowed == [("src/app/core.py", "modify")]
    assert sorted((v["path"], v["operation"]) for v in report.violations) == [
        (CORE_LOOKALIKE, "create"),
        (FEATURE_LOOKALIKE, "create"),
    ]
    assert all("backslash" in v["reason"] for v in report.violations)
    assert report.checked == 3 and not report.ok

    ev = await events_for(sessionmaker, step.id, EventType.SCOPE_VIOLATION)
    assert len(ev) == 1
    assert sorted(v["path"] for v in ev[0].payload["violations"]) == sorted([CORE_LOOKALIKE, FEATURE_LOOKALIKE])


async def test_staged_rename_from_a_backslash_name_is_judged_by_its_literal_origin(sessionmaker: SM, repo: Path) -> None:
    # a committed root-level file whose name looks like the designated deletion legacy/old_module.py
    (repo / LEGACY_LOOKALIKE).write_text("OLD = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "backslash file")
    step, ws = await _scoped(sessionmaker, repo)
    (repo / "src/app/feature").mkdir(parents=True)
    git(repo, "mv", LEGACY_LOOKALIKE, "src/app/feature/old_module.py")
    assert (repo / "legacy/old_module.py").exists()  # the designated file itself was not touched

    entries = [e for e in await _status(ws) if e.status != "??"]
    assert entries == [GitStatusEntry(path="src/app/feature/old_module.py", status="R ", orig_path=LEGACY_LOOKALIKE)]
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(step.id, entries)

    # judged as legacy/old_module.py the deletion would pass (it is designated); the real name is a violation
    assert report.allowed == [("src/app/feature/old_module.py", "create")]
    assert [(v["path"], v["operation"]) for v in report.violations] == [(LEGACY_LOOKALIKE, "delete")]
    assert "backslash" in report.violations[0]["reason"]


async def test_audit_changes_refuses_backslash_names_even_with_a_permissive_scope(sessionmaker: SM, repo: Path) -> None:
    step, _ = await _scoped(sessionmaker, repo)
    report = await ScopeAuditor(sessionmaker, make_config()).audit_changes(
        step.id, [(CORE_LOOKALIKE, "modify"), ("src/app/core.py", "modify")]
    )
    assert report.allowed == [("src/app/core.py", "modify")]
    assert [(v["path"], v["operation"]) for v in report.violations] == [(CORE_LOOKALIKE, "modify")]
