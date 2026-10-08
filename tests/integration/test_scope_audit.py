"""P15 15.9 scope audit against real PostgreSQL + real git status."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeExpansionRequest
from hermclaw.core.interfaces import GitStatusEntry
from hermclaw.scope.audit import ScopeAuditor
from hermclaw.scope.engine import ScopeEngine
from hermclaw.scope.expansion import ScopeExpansionHandler
from tests.integration.test_scope_support import (
    FakeRepo,
    build_repo,
    events_for,
    git,
    make_config,
    make_step,
    scope_rows,
    workspace_for,
    write,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def repo(tmp_repo: Path) -> Path:
    return build_repo(tmp_repo)


async def _scoped(sessionmaker, repo: Path, **fields):
    fields.setdefault("repo_hints", ["src/app/core.py"])
    fields.setdefault("allowed_new_paths", ["tests/test_new.py"])
    step = await make_step(sessionmaker, **fields)
    ws = workspace_for(step.job_id, repo)
    decision = await ScopeEngine(sessionmaker, make_config(), FakeRepo()).create_scope(step.id, ws)
    return step, ws, decision


async def test_allowed_changes_pass_and_are_recorded(sessionmaker, repo: Path) -> None:
    step, _, _ = await _scoped(sessionmaker, repo)
    auditor = ScopeAuditor(sessionmaker, make_config())
    report = await auditor.audit_changes(
        step.id, [("src/app/core.py", "modify"), ("tests/test_new.py", "create"), ("src/app/core.py", "modify")]
    )
    assert report.ok and report.checked == 2 and report.scope_version == 1 and report.scope_status == "active"
    assert report.allowed == [("src/app/core.py", "modify"), ("tests/test_new.py", "create")]
    rows = await scope_rows(sessionmaker, step.id)
    audits = rows[0].evidence["audits"]
    assert len(audits) == 1 and audits[0]["ok"] is True and audits[0]["checked"] == 2
    assert rows[0].evidence["last_audit_ok"] is True
    assert await events_for(sessionmaker, step.id, EventType.SCOPE_VIOLATION) == []


async def test_violations_emit_event_and_persist(sessionmaker, repo: Path) -> None:
    step, _, _ = await _scoped(sessionmaker, repo)
    auditor = ScopeAuditor(sessionmaker, make_config())
    changes = [
        ("src/app/core.py", "modify"),
        ("src/lib/util.py", "modify"),
        (".env", "modify"),
        ("src/app/core.py", "delete"),
        ("../escape.py", "create"),
        ("src/app/other.py", "create"),
    ]
    report = await auditor.audit_changes(step.id, changes, phase="post_attempt")  # type: ignore[arg-type]
    assert not report.ok and report.checked == 6
    reasons = {(v["path"], v["operation"]): v["reason"] for v in report.violations}
    assert set(reasons) == {
        ("src/lib/util.py", "modify"),
        (".env", "modify"),
        ("src/app/core.py", "delete"),
        ("../escape.py", "create"),
        ("src/app/other.py", "create"),
    }
    assert "forbidden" in reasons[(".env", "modify")]
    assert "not allowed" in reasons[("src/app/core.py", "delete")]
    assert "not a target path" in reasons[("src/lib/util.py", "modify")]
    assert "outside allowed_new_paths" in reasons[("src/app/other.py", "create")]

    ev = await events_for(sessionmaker, step.id, EventType.SCOPE_VIOLATION)
    assert len(ev) == 1 and ev[0].severity == "error"
    assert ev[0].payload["violation_count"] == 5 and ev[0].payload["scope_version"] == 1 and ev[0].payload["phase"] == "post_attempt"
    rows = await scope_rows(sessionmaker, step.id)
    assert rows[0].evidence["audits"][-1]["violation_count"] == 5 and rows[0].evidence["last_audit_ok"] is False
    assert rows[0].evidence["hints"]  # generation evidence preserved next to the audit


async def test_audit_of_real_git_status(sessionmaker, repo: Path) -> None:
    step, _, _ = await _scoped(sessionmaker, repo)
    write(repo, "src/app/core.py", "def run():\n    return 'x'\n")
    write(repo, "tests/test_new.py", "def test_new():\n    assert True\n")
    write(repo, "src/lib/util.py", "def unrelated():\n    return 2\n")
    git(repo, "rm", "-q", "docs/guide.md")
    out = git(repo, "status", "--porcelain", "--untracked-files=all")
    entries = [GitStatusEntry(line[3:], line[:2]) for line in out.splitlines()]
    report = await ScopeAuditor(sessionmaker, make_config()).audit_status(step.id, entries)
    assert ("src/app/core.py", "modify") in report.allowed and ("tests/test_new.py", "create") in report.allowed
    violating = {(v["path"], v["operation"]) for v in report.violations}
    assert ("src/lib/util.py", "modify") in violating and ("docs/guide.md", "delete") in violating
    assert ("notes/untracked.md", "create") in violating  # untracked file of the fixture is outside scope too


async def test_audit_without_scope_denies_everything(sessionmaker, repo: Path) -> None:
    step = await make_step(sessionmaker, repo_hints=["src/app/core.py"])
    report = await ScopeAuditor(sessionmaker, make_config()).audit_changes(step.id, [("src/app/core.py", "modify")])
    assert not report.ok and report.scope_version is None and report.scope_status is None
    assert "no active scope" in report.violations[0]["reason"]
    assert len(await events_for(sessionmaker, step.id, EventType.SCOPE_VIOLATION)) == 1


async def test_audit_of_unavailable_scope_records_into_that_version(sessionmaker, repo: Path) -> None:
    step, _, decision = await _scoped(sessionmaker, repo, repo_hints=["missing/*.py"], allowed_new_paths=[])
    assert decision.status == "unavailable"
    report = await ScopeAuditor(sessionmaker, make_config()).audit_changes(step.id, [("src/app/core.py", "modify")])
    assert not report.ok and report.scope_status == "unavailable" and report.scope_version == 1
    rows = await scope_rows(sessionmaker, step.id)
    assert rows[0].evidence["audits"][0]["ok"] is False and rows[0].evidence["unavailable_reason"]["code"] == "no_resolvable_scope"


async def test_audit_uses_newest_version_after_expansion(sessionmaker, repo: Path) -> None:
    step, ws, _ = await _scoped(sessionmaker, repo)
    auditor = ScopeAuditor(sessionmaker, make_config())
    before = await auditor.audit_changes(step.id, [("tests/test_core.py", "modify")])
    assert not before.ok and before.scope_version == 1
    req = ScopeExpansionRequest(paths=["tests/test_core.py"], justification="update the test of the target")
    granted = await ScopeExpansionHandler(sessionmaker, make_config(), FakeRepo()).handle(step.id, req, ws)
    assert granted.granted and granted.version == 2
    after = await auditor.audit_changes(step.id, [("tests/test_core.py", "modify")])
    assert after.ok and after.scope_version == 2
    rows = await scope_rows(sessionmaker, step.id)
    assert len(rows[0].evidence["audits"]) == 1 and len(rows[1].evidence["audits"]) == 1


async def test_audit_history_is_bounded(sessionmaker, repo: Path) -> None:
    step, _, _ = await _scoped(sessionmaker, repo)
    auditor = ScopeAuditor(sessionmaker, make_config())
    for i in range(22):
        await auditor.audit_changes(step.id, [("src/app/core.py", "modify")], phase=f"turn-{i}")
    rows = await scope_rows(sessionmaker, step.id)
    audits = rows[0].evidence["audits"]
    assert len(audits) == 20 and audits[0]["phase"] == "turn-2" and audits[-1]["phase"] == "turn-21"
