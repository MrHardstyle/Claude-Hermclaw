"""Verifier failure behaviour: git/executor outages, hangs, check crashes, verifier crash, persistence failure,
cancellation, unsafe files (Bauplan failure tests: "verifier crash", "secret detection")."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.persistence.models import CommandRun, Event, VerificationCheckRow, VerificationRun
from hermclaw.tools.snapshot import AuditOutcome, Violation, WorkspaceTracker
from hermclaw.verifier import Verifier
from hermclaw.verifier import checks as generic
from hermclaw.verifier import commands as commands_mod
from hermclaw.verifier import engine as engine_mod
from tests.integration.test_verifier_support import (
    BrokenGitReader,
    HangingExecutor,
    RaisingExecutor,
    by_name,
    handle,
    init_repo,
    make_job_step,
    make_verifier,
    scope,
    step,
    write,
)

pytestmark = pytest.mark.integration


async def _run(sm: Any, repo: Path, st: Any, verifier: Verifier, **kw: Any) -> Any:
    job_id, step_id = await make_job_step(sm, kind=st.kind)
    return await verifier.run(st, handle(repo, job_id), job_id=job_id, step_id=step_id, **kw), job_id, step_id


async def _run_row(sm: Any, run_id: Any) -> VerificationRun:
    async with sm() as s:
        row: VerificationRun | None = await s.get(VerificationRun, run_id)
        assert row is not None
        return row


async def test_git_reader_outage_gives_error_run(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    v = make_verifier(sessionmaker, git_reader=BrokenGitReader())
    outcome, _job, step_id = await _run(sessionmaker, repo, step("documentation"), v)
    assert not outcome.report.passed and outcome.status == "error"
    assert by_name(outcome.report)["changed_files"].status == "error"
    row = await _run_row(sessionmaker, outcome.run_id)
    assert row.status == "error" and not row.passed and row.finished_at is not None
    async with sessionmaker() as s:
        finished = (
            await s.execute(select(Event).where(Event.step_id == step_id, Event.event_type == EventType.VERIFIER_FINISHED))
        ).scalar_one()
        assert finished.severity == "error" and finished.payload["status"] == "error"


async def test_executor_outage_is_an_error_and_redacted(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"tests/test_x.py": "def test_x():\n    pass\n"})
    write(repo, "a.php", "<?php echo 1;\n")
    executor = RaisingExecutor()
    st = step(
        scope_contract=scope(allowed_new_paths=["*.php"]),
        acceptance=[{"type": "command", "command": "true"}, {"type": "test", "command": "pytest", "framework": "pytest"}],
    )
    outcome, _job, step_id = await _run(sessionmaker, repo, st, make_verifier(sessionmaker, executor))
    names = by_name(outcome.report)
    assert names["acceptance[0]:command"].status == "error"
    assert names["acceptance[1]:test"].status == "error"
    assert names["syntax:a.php"].status == "error"
    assert names["test_evidence"].status == "fail"
    assert executor.calls == 3
    async with sessionmaker() as s:
        rows = (
            (await s.execute(select(VerificationCheckRow).where(VerificationCheckRow.verification_run_id == outcome.run_id)))
            .scalars()
            .all()
        )
        cmds = (await s.execute(select(CommandRun).where(CommandRun.step_id == step_id))).scalars().all()
        dumped = repr([(r.message, r.evidence) for r in rows]) + repr([(c.stderr_excerpt, c.command) for c in cmds])
        assert "supersecretvalue123" not in dumped
        assert len(cmds) == 3 and all(c.exit_code is None for c in cmds)


async def test_hanging_executor_is_bounded(sessionmaker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands_mod, "EXECUTOR_GRACE_SECONDS", 0)
    repo = init_repo(tmp_path / "repo")
    st = step(
        "documentation", acceptance=[{"type": "command", "command": "true", "timeout_seconds": 1}, {"type": "diff", "allow_empty": True}]
    )
    outcome, *_ = await asyncio.wait_for(_run(sessionmaker, repo, st, make_verifier(sessionmaker, HangingExecutor())), 30)
    check = by_name(outcome.report)["acceptance[0]:command"]
    assert check.status == "error" and "did not answer" in check.message


async def test_crashing_check_group_does_not_abort_the_run(sessionmaker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("scanner bug")

    monkeypatch.setattr(generic, "secret_checks", boom)
    repo = init_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    write(repo, "a.py", "x = 2\n")
    outcome, *_ = await _run(
        sessionmaker, repo, step("documentation", scope_contract=scope(target_paths=["a.py"])), make_verifier(sessionmaker)
    )
    names = by_name(outcome.report)
    assert names["secrets"].status == "error" and "scanner bug" in names["secrets"].message
    assert names["scope"].status == "pass" and names["syntax:a.py"].status == "pass"
    assert not outcome.report.passed and outcome.status == "error"


async def test_verifier_crash_is_persisted_as_error(sessionmaker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def crash(*_a: Any, **_k: Any) -> Any:
        raise ValueError("unexpected state")

    monkeypatch.setattr(Verifier, "_evaluate", crash)
    repo = init_repo(tmp_path / "repo")
    outcome, *_ = await _run(sessionmaker, repo, step("documentation"), make_verifier(sessionmaker))
    assert outcome.status == "error" and [c.name for c in outcome.report.checks] == ["verifier"]
    assert "verifier crashed: ValueError: unexpected state" in outcome.report.checks[0].message
    row = await _run_row(sessionmaker, outcome.run_id)
    assert row.status == "error" and not row.passed


async def test_persistence_failure_marks_the_run_as_error(sessionmaker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken_finish(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("db went away")

    monkeypatch.setattr(engine_mod, "finish_run", broken_finish)
    repo = init_repo(tmp_path / "repo")
    job_id, step_id = await make_job_step(sessionmaker, kind="documentation")
    v = make_verifier(sessionmaker)
    with pytest.raises(ConnectionError):
        await v.run(step("documentation"), handle(repo, job_id), job_id=job_id, step_id=step_id)
    async with sessionmaker() as s:
        row = (await s.execute(select(VerificationRun).where(VerificationRun.step_id == step_id))).scalar_one()
        assert row.status == "error" and not row.passed and "could not be persisted" in (row.summary or "")


async def test_cancellation_marks_the_run_as_error(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    job_id, step_id = await make_job_step(sessionmaker, kind="documentation")
    v = make_verifier(sessionmaker, HangingExecutor())
    task = asyncio.create_task(
        v.run(
            step("documentation", acceptance=[{"type": "command", "command": "sleep 100"}]),
            handle(repo, job_id),
            job_id=job_id,
            step_id=step_id,
        )
    )
    for _ in range(100):
        await asyncio.sleep(0.05)
        async with sessionmaker() as s:
            if (await s.execute(select(VerificationRun).where(VerificationRun.step_id == step_id))).scalar_one_or_none() is not None:
                break
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessionmaker() as s:
        row = (await s.execute(select(VerificationRun).where(VerificationRun.step_id == step_id))).scalar_one()
        assert row.status == "error" and not row.passed and "cancelled" in (row.summary or "")
    assert len(v._locks) == 0


async def test_missing_workspace(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    job_id, step_id = await make_job_step(sessionmaker, kind="documentation")
    ws = handle(repo, job_id)
    import shutil

    shutil.rmtree(repo)
    outcome = await make_verifier(sessionmaker).run(step("documentation"), ws, job_id=job_id, step_id=step_id)
    assert outcome.status == "error" and by_name(outcome.report)["workspace"].status == "error"


async def test_snapshot_or_restore_failure_blocks(sessionmaker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = init_repo(tmp_path / "repo")
    st = step("documentation", acceptance=[{"type": "command", "command": "true"}, {"type": "diff", "allow_empty": True}])

    async def no_snapshot(self: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(WorkspaceTracker, "snapshot", no_snapshot)
    outcome, *_ = await _run(sessionmaker, repo, st, make_verifier(sessionmaker))
    check = by_name(outcome.report)["workspace_integrity"]
    assert check.status == "error" and check.blocking and "disk full" in check.message
    assert not outcome.report.passed
    monkeypatch.undo()

    async def stuck(self: Any, before: Any, decide: Any) -> AuditOutcome:
        return AuditOutcome(violations=[Violation("src/x.py", "modify", "r", reverted=False)])

    monkeypatch.setattr(WorkspaceTracker, "audit", stuck)
    outcome2, *_ = await _run(sessionmaker, repo, st, make_verifier(sessionmaker))
    check2 = by_name(outcome2.report)["workspace_integrity"]
    assert check2.status == "error" and check2.evidence["path"] == "src/x.py"


async def test_unsafe_and_unreadable_files_are_skipped_safely(sessionmaker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generic, "MAX_SYNTAX_FILE_BYTES", 1024)
    outside = tmp_path / "outside.py"
    outside.write_text("def broken(:\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    os.symlink(outside, repo / "link.py")
    write(repo, "big.py", "x = 1\n" * 1000)
    write(repo, "blob.py", b"\x00\x01\x02 binary")
    st = step("documentation", scope_contract=scope(allowed_new_paths=["*.py"]))
    outcome, *_ = await _run(sessionmaker, repo, st, make_verifier(sessionmaker))
    names = by_name(outcome.report)
    assert names["syntax:link.py"].status == "skip" and "symlink" in names["syntax:link.py"].message
    assert names["syntax:big.py"].status == "skip" and "larger than" in names["syntax:big.py"].message
    assert names["syntax:blob.py"].status == "skip" and "binary" in names["syntax:blob.py"].message
    assert names["secrets"].evidence["skipped"] == {"blob.py": "binary file", "link.py": "symlink"}


async def test_artifact_lookup_failure_is_an_error(sessionmaker: Any, tmp_path: Path) -> None:
    async def broken(*_a: Any) -> list[Any]:
        raise TimeoutError("artifact store down")

    repo = init_repo(tmp_path / "repo")
    st = step("image", acceptance=[{"type": "artifact", "kind": "image"}])
    outcome, *_ = await _run(sessionmaker, repo, st, make_verifier(sessionmaker), artifacts=broken)
    check = by_name(outcome.report)["acceptance[0]:artifact"]
    assert check.status == "error" and "artifact store down" in check.message


async def test_content_search_has_a_total_byte_budget(sessionmaker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermclaw.verifier import evidence as evidence_mod

    monkeypatch.setattr(evidence_mod, "MAX_SEARCH_TOTAL_BYTES", 10)
    repo = init_repo(tmp_path / "repo", {"a.txt": "needle in the first file\n", "b.txt": "needle in the second file\n"})
    st = step(
        "documentation", acceptance=[{"type": "absence", "path_glob": "*.txt", "pattern": "needle"}, {"type": "diff", "allow_empty": True}]
    )
    outcome, *_ = await _run(sessionmaker, repo, st, make_verifier(sessionmaker))
    check = by_name(outcome.report)["acceptance[0]:absence"]
    assert check.status == "fail" and check.evidence["files_searched"] == 1
    assert "budget" in check.evidence["skipped"]["b.txt"]


async def test_nul_bytes_and_non_utf8_file_names_are_persisted(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    weird = os.fsdecode(b"bad\xffname.py")
    (repo / weird).write_bytes(b"x = (\n")
    st = step(
        "documentation",
        scope_contract=scope(allowed_new_paths=["*.txt"]),
        acceptance=[{"type": "command", "command": "printf 'a\\0b'; exit 1"}],
    )
    outcome, *_ = await _run(sessionmaker, repo, st, make_verifier(sessionmaker))
    assert not outcome.report.passed
    row = await _run_row(sessionmaker, outcome.run_id)
    assert row.status == "failed" and row.changed_files == ["bad\\udcffname.py"]
    async with sessionmaker() as s:
        rows = (
            (await s.execute(select(VerificationCheckRow).where(VerificationCheckRow.verification_run_id == outcome.run_id)))
            .scalars()
            .all()
        )
        names = {r.name for r in rows}
        assert "syntax:bad\\udcffname.py" in names and "scope" in names
        cmd = next(r for r in rows if r.name == "acceptance[0]:command")
        assert "a\\x00b" in cmd.evidence["output"]


async def test_side_effects_are_reverted_even_when_cancelled_mid_command(sessionmaker: Any, tmp_path: Path) -> None:
    from tests.integration.test_verifier_support import WorkspaceShellExecutor

    repo = init_repo(tmp_path / "repo", {"a.txt": "original\n"})
    job_id, step_id = await make_job_step(sessionmaker, kind="documentation")
    st = step(
        "documentation",
        acceptance=[{"type": "command", "command": "echo tampered >> a.txt; touch new.txt; sleep 30", "timeout_seconds": 60}],
    )
    task = asyncio.create_task(
        make_verifier(sessionmaker, WorkspaceShellExecutor()).run(st, handle(repo, job_id), job_id=job_id, step_id=step_id)
    )
    for _ in range(200):
        await asyncio.sleep(0.05)
        if (repo / "new.txt").exists():
            break
    assert (repo / "new.txt").exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (repo / "a.txt").read_text() == "original\n"
    assert not (repo / "new.txt").exists()
    row = await _run_row_for_step(sessionmaker, step_id)
    assert row.status == "error"


async def _run_row_for_step(sm: Any, step_id: Any) -> VerificationRun:
    async with sm() as s:
        row: VerificationRun = (await s.execute(select(VerificationRun).where(VerificationRun.step_id == step_id))).scalar_one()
        return row
