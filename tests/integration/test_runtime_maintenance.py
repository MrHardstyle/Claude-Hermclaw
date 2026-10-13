"""Workspace retention (``hermclaw.runtime.maintenance.WorkspaceJanitor``) against a real GitEngine workspace
(real git clones under the engine's workspace root) and a real RepoIntelligence index (``code_symbols`` /
``code_chunks`` rows of the workspace-scoped index key) in PostgreSQL."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.config import GitPolicy, PoliciesConfig
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.persistence.models import CodeChunk, CodeSymbol, Event, GitOperation, Job, Workspace
from hermclaw.repo_intelligence.service import RepoIntelligence
from hermclaw.runtime.maintenance import KEEP_JOB_ACTIVE, KEEP_JOB_LOCKED, KEEP_UNPUSHED, WorkspaceJanitor
from tests.integration.test_gitops_support import World, make_world

pytestmark = pytest.mark.integration

RETENTION_H = 72
OLD = timedelta(hours=RETENTION_H + 28)
RECENT = timedelta(hours=1)


@dataclass
class Case:
    job_id: uuid.UUID
    workspace_id: uuid.UUID
    handle: WorkspaceHandle
    index_key: str


@pytest.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path) -> World:
    return await make_world(sessionmaker, tmp_path)


@pytest.fixture
async def repo_intel(sessionmaker: async_sessionmaker[AsyncSession]) -> Any:
    svc = RepoIntelligence(sessionmaker, emit_events=False)
    yield svc
    await svc.aclose()


def _now() -> datetime:
    return datetime.now(UTC)


async def _case(
    world: World,
    svc: RepoIntelligence,
    *,
    status: str,
    finished_ago: timedelta | None,
    updated_ago: timedelta | None = None,
    index: bool = True,
) -> Case:
    """A job with a real workspace clone (and, by default, a real index of it), then moved to ``status``."""
    job_id = await world.job(f"retention {status}")
    ws = await world.engine.create_workspace(job_id, world.repo.id)
    handle = await world.engine.handle(ws.id)
    if index:
        await svc.ensure_index(svc.target_of(handle))
    now = _now()
    values: dict[str, Any] = {"status": status, "finished_at": now - finished_ago if finished_ago is not None else None}
    if updated_ago is not None:
        values["updated_at"] = now - updated_ago
    async with world.sessionmaker() as s:
        await s.execute(update(Job).where(Job.id == job_id).values(**values))
        await s.commit()
    return Case(job_id, ws.id, handle, svc.index_key(handle))


async def _index_rows(sm: async_sessionmaker[AsyncSession], key: str) -> tuple[int, int]:
    async with sm() as s:
        n_sym = (await s.execute(select(func.count()).select_from(CodeSymbol).where(CodeSymbol.repository_key == key))).scalar_one()
        n_chunk = (await s.execute(select(func.count()).select_from(CodeChunk).where(CodeChunk.repository_key == key))).scalar_one()
    return int(n_sym), int(n_chunk)


async def _ws_status(sm: async_sessionmaker[AsyncSession], ws_id: uuid.UUID) -> str:
    async with sm() as s:
        return str((await s.execute(select(Workspace.status).where(Workspace.id == ws_id))).scalar_one())


async def _cleaned_events(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> list[Event]:
    async with sm() as s:
        q = select(Event).where(Event.job_id == job_id, Event.event_type == EventType.WORKSPACE_CLEANED).order_by(Event.sequence)
        return list((await s.execute(q)).scalars())


def _janitor(world: World, repo: object | None, **kw: Any) -> WorkspaceJanitor:
    kw.setdefault("policy", GitPolicy(workspace_retention_hours=RETENTION_H))
    return WorkspaceJanitor(world.sessionmaker, world.engine, repo, **kw)


async def _assert_kept(world: World, case: Case, status: str = "active") -> None:
    assert Path(case.handle.path).is_dir(), "workspace directory is kept"
    assert await _ws_status(world.sessionmaker, case.workspace_id) == status
    n_sym, n_chunk = await _index_rows(world.sessionmaker, case.index_key)
    assert n_sym > 0 and n_chunk > 0, "workspace index is kept"
    assert await _cleaned_events(world.sessionmaker, case.job_id) == []


async def _assert_cleaned(world: World, case: Case) -> Event:
    assert not Path(case.handle.path).exists(), "workspace directory removed"
    assert await _ws_status(world.sessionmaker, case.workspace_id) == "cleaned"
    assert await _index_rows(world.sessionmaker, case.index_key) == (0, 0), "code_symbols/code_chunks of the workspace key dropped"
    events = await _cleaned_events(world.sessionmaker, case.job_id)
    assert len(events) == 1
    return events[0]


async def test_old_terminal_jobs_are_cleaned_recent_and_active_jobs_are_kept(world: World, repo_intel: RepoIntelligence) -> None:
    svc = repo_intel
    succeeded = await _case(world, svc, status="succeeded", finished_ago=OLD)
    failed = await _case(world, svc, status="failed", finished_ago=OLD)
    # a terminal row without finished_at falls back to updated_at
    cancelled = await _case(world, svc, status="cancelled", finished_ago=None, updated_ago=OLD)
    recent = await _case(world, svc, status="succeeded", finished_ago=RECENT)
    running = await _case(world, svc, status="running", finished_ago=None, updated_ago=OLD)
    # an API retry moves a failed job back to queued; its old finished_at must not make the workspace a candidate
    retried = await _case(world, svc, status="queued", finished_ago=OLD, updated_ago=OLD)
    paused = await _case(world, svc, status="paused", finished_ago=None, updated_ago=OLD)
    for c in (succeeded, failed, cancelled):
        n_sym, n_chunk = await _index_rows(world.sessionmaker, c.index_key)
        assert n_sym > 0 and n_chunk > 0, "precondition: the workspace is indexed"

    report = await _janitor(world, svc).run_once()

    assert report.cleaned_ids == {succeeded.workspace_id, failed.workspace_id, cancelled.workspace_id}
    assert report.failed == {}
    for c, job_status in ((succeeded, "succeeded"), (failed, "failed"), (cancelled, "cancelled")):
        ev = await _assert_cleaned(world, c)
        assert ev.source_type == "runtime"
        assert ev.payload["workspace_id"] == str(c.workspace_id) and ev.payload["job_id"] == str(c.job_id)
        assert ev.payload["job_status"] == job_status and ev.payload["retention_hours"] == RETENTION_H
        assert ev.payload["status_before"] == "active" and ev.payload["path"] == str(c.handle.path)
        assert ev.payload["index"]["symbols"] > 0 and ev.payload["index"]["chunks"] > 0
        async with world.sessionmaker() as s:
            ops = (await s.execute(select(GitOperation).where(GitOperation.workspace_id == c.workspace_id))).scalars().all()
        assert [o.status for o in ops if o.operation == "workspace.cleanup"] == ["ok"], "the cleanup is audited by the Git engine"
    for c in (recent, running, retried, paused):
        await _assert_kept(world, c)
    # the repository mirror and the other jobs' workspaces stay untouched; a second pass has nothing left to do
    again = await _janitor(world, svc).run_once()
    assert again.cleaned == [] and again.failed == {}
    for c in (recent, running, retried, paused):
        await _assert_kept(world, c)


async def test_recent_job_is_cleaned_once_the_retention_has_passed(world: World, repo_intel: RepoIntelligence) -> None:
    case = await _case(world, repo_intel, status="succeeded", finished_ago=RECENT)
    assert (await _janitor(world, repo_intel).run_once()).cleaned == []
    await _assert_kept(world, case)
    later = _now() + timedelta(hours=RETENTION_H)  # the clock moved on past the retention
    report = await _janitor(world, repo_intel, now=lambda: later).run_once()
    assert report.cleaned_ids == {case.workspace_id}
    await _assert_cleaned(world, case)


async def test_retention_zero_keeps_workspaces_forever(world: World, repo_intel: RepoIntelligence) -> None:
    case = await _case(world, repo_intel, status="succeeded", finished_ago=timedelta(days=365))
    janitor = _janitor(world, repo_intel, policy=GitPolicy(workspace_retention_hours=0))
    assert not janitor.enabled
    report = await janitor.run_once()
    assert report.cleaned == [] and report.kept == {} and report.failed == {}
    janitor.start()
    assert not janitor.running, "no loop is started when the retention is disabled"
    await janitor.stop()
    await _assert_kept(world, case)


async def test_default_policy_comes_from_the_git_engine(
    world: World, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    assert WorkspaceJanitor(sessionmaker, world.engine).retention_hours == GitPolicy().workspace_retention_hours == 72
    other = await make_world(sessionmaker, tmp_path / "w2", policies=PoliciesConfig(git=GitPolicy(workspace_retention_hours=5)))
    assert WorkspaceJanitor(sessionmaker, other.engine).retention_hours == 5


async def test_unpushed_verified_commits_are_never_discarded(world: World, repo_intel: RepoIntelligence) -> None:
    keep = await _case(world, repo_intel, status="failed", finished_ago=OLD)
    gone = await _case(world, repo_intel, status="failed", finished_ago=OLD)
    async with world.sessionmaker() as s:
        await s.execute(update(Workspace).where(Workspace.id.in_([keep.workspace_id, gone.workspace_id])).values(status="committed"))
        await s.commit()
    # a committed workspace whose directory is already gone has nothing left to keep
    shutil.rmtree(gone.handle.path)
    report = await _janitor(world, repo_intel).run_once()
    assert report.kept.get(keep.workspace_id) == KEEP_UNPUSHED
    assert report.cleaned_ids == {gone.workspace_id}
    await _assert_kept(world, keep, status="committed")
    ev = await _assert_cleaned(world, gone)
    assert ev.payload["status_before"] == "committed"


async def test_errors_are_reported_and_do_not_stop_the_pass(world: World, repo_intel: RepoIntelligence) -> None:
    broken = await _case(world, repo_intel, status="failed", finished_ago=OLD + timedelta(hours=2), index=False)
    good = await _case(world, repo_intel, status="succeeded", finished_ago=OLD)
    # a tampered row: below the workspace root but not a job workspace path -> the Git engine refuses it
    async with world.sessionmaker() as s:
        stray = str(world.engine.workspaces_root / "stray")
        await s.execute(update(Workspace).where(Workspace.id == broken.workspace_id).values(path=stray))
        await s.commit()
    janitor = _janitor(world, repo_intel, retry_after_seconds=3600)
    report = await janitor.run_once()
    assert set(report.failed) == {broken.workspace_id} and "WorkspacePathViolation" in report.failed[broken.workspace_id]
    assert report.cleaned_ids == {good.workspace_id}
    await _assert_cleaned(world, good)
    assert await _ws_status(world.sessionmaker, broken.workspace_id) == "active"
    # a failed workspace backs off instead of being retried on every pass
    assert (await janitor.run_once()).failed == {}
    retry_now = _janitor(world, repo_intel, retry_after_seconds=0)
    assert set((await retry_now.run_once()).failed) == {broken.workspace_id}


class _FailingIndex:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def drop_workspace_index(self, handle: WorkspaceHandle) -> dict[str, int]:
        self.calls.append(handle.id)
        raise RuntimeError("index database unavailable")


async def test_index_drop_failure_or_missing_provider_still_cleans(world: World, repo_intel: RepoIntelligence) -> None:
    a = await _case(world, repo_intel, status="succeeded", finished_ago=OLD)
    failing = _FailingIndex()
    report = await _janitor(world, failing).run_once()
    assert report.cleaned_ids == {a.workspace_id} and failing.calls == [a.workspace_id]
    assert report.cleaned[0].index_error and "index database unavailable" in report.cleaned[0].index_error
    events = await _cleaned_events(world.sessionmaker, a.job_id)
    assert len(events) == 1 and "index database unavailable" in events[0].payload["index_error"] and events[0].payload["index"] is None
    assert not Path(a.handle.path).exists()
    # a provider without drop_workspace_index (e.g. a stub repository context) is fine as well
    b = await _case(world, repo_intel, status="succeeded", finished_ago=OLD)
    report = await _janitor(world, object()).run_once()
    assert report.cleaned_ids == {b.workspace_id} and report.cleaned[0].index is None
    assert (await _cleaned_events(world.sessionmaker, b.job_id))[0].payload["index"] is None


async def test_job_row_locked_by_a_control_request_is_skipped(world: World, repo_intel: RepoIntelligence) -> None:
    case = await _case(world, repo_intel, status="failed", finished_ago=OLD)
    janitor = _janitor(world, repo_intel)
    async with world.sessionmaker() as holder, holder.begin():
        # e.g. POST /api/jobs/{id}/retry holds the job row FOR UPDATE while it moves the job back to queued
        await holder.execute(select(Job).where(Job.id == case.job_id).with_for_update())
        report = await asyncio.wait_for(janitor.run_once(), timeout=30)
        assert report.kept.get(case.workspace_id) == KEEP_JOB_LOCKED and report.cleaned == []
        await holder.execute(update(Job).where(Job.id == case.job_id).values(status="queued"))
    # the retry committed: the job is active again and its workspace is never touched
    report = await janitor.run_once()
    assert report.cleaned == []
    await _assert_kept(world, case)


async def test_active_again_between_selection_and_lock_is_kept(world: World, repo_intel: RepoIntelligence) -> None:
    """The candidate query and the locked re-check are separate statements: a job that became active in between
    (retry) is re-checked under the row lock and kept."""
    case = await _case(world, repo_intel, status="failed", finished_ago=OLD)
    janitor = _janitor(world, repo_intel)
    original = janitor._candidates

    async def candidates_then_retry(cutoff: datetime) -> list[tuple[uuid.UUID, uuid.UUID]]:
        rows = await original(cutoff)
        async with world.sessionmaker() as s:
            await s.execute(update(Job).where(Job.id == case.job_id).values(status="queued"))
            await s.commit()
        return rows

    janitor._candidates = candidates_then_retry  # type: ignore[method-assign]
    report = await janitor.run_once()
    assert report.kept.get(case.workspace_id) == KEEP_JOB_ACTIVE and report.cleaned == []
    await _assert_kept(world, case)


async def test_start_stop_loop_cleans_periodically(world: World, repo_intel: RepoIntelligence) -> None:
    janitor = _janitor(world, repo_intel, interval_seconds=0.05)
    janitor.start()
    try:
        assert janitor.running
        case = await _case(world, repo_intel, status="cancelled", finished_ago=OLD)  # appears while the loop runs
        async with asyncio.timeout(30):
            while await _ws_status(world.sessionmaker, case.workspace_id) != "cleaned":
                await asyncio.sleep(0.05)
        await _assert_cleaned(world, case)
        janitor.start()  # idempotent
        assert janitor.running
    finally:
        await janitor.stop()
    assert not janitor.running


async def test_loop_survives_a_failing_pass(world: World, repo_intel: RepoIntelligence) -> None:
    janitor = _janitor(world, repo_intel, interval_seconds=0.02)
    calls = 0
    original = janitor.run_once

    async def flaky() -> Any:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("database restarting")
        return await original()

    janitor.run_once = flaky  # type: ignore[method-assign]
    case = await _case(world, repo_intel, status="succeeded", finished_ago=OLD)
    janitor.start()
    try:
        async with asyncio.timeout(30):
            while await _ws_status(world.sessionmaker, case.workspace_id) != "cleaned":
                await asyncio.sleep(0.05)
        assert janitor.running and calls >= 3
    finally:
        await janitor.stop()


async def test_concurrent_janitors_clean_a_workspace_once(world: World, repo_intel: RepoIntelligence) -> None:
    case = await _case(world, repo_intel, status="succeeded", finished_ago=OLD)
    reports = await asyncio.gather(_janitor(world, repo_intel).run_once(), _janitor(world, repo_intel).run_once())
    assert sum(len(r.cleaned) for r in reports) == 1
    assert all(r.failed == {} for r in reports)
    await _assert_cleaned(world, case)


async def test_workspaces_outside_the_engine_root_are_ignored(world: World, repo_intel: RepoIntelligence, tmp_path: Path) -> None:
    case = await _case(world, repo_intel, status="succeeded", finished_ago=OLD)
    # another engine (different data dir) never sees this workspace as a candidate
    other = await make_world(world.sessionmaker, tmp_path / "other")
    report = await WorkspaceJanitor(other.sessionmaker, other.engine, repo_intel, policy=GitPolicy(workspace_retention_hours=1)).run_once()
    assert case.workspace_id not in report.cleaned_ids and case.workspace_id not in report.failed
    await _assert_kept(world, case)


async def test_crash_right_after_the_directory_removal_leaves_no_orphaned_index(world: World, repo_intel: RepoIntelligence) -> None:
    """The index is derived data and is dropped *before* the directory: a process that dies between the two steps
    must not leave ``code_symbols``/``code_chunks`` rows of a ``cleaned`` workspace behind (cleaned workspaces are
    never candidates again, so such rows would stay forever)."""
    case = await _case(world, repo_intel, status="succeeded", finished_ago=OLD)
    real_cleanup = world.engine.cleanup

    async def cleanup_then_die(ws: Any, *, force: bool = False) -> Any:
        await real_cleanup(ws, force=force)
        raise RuntimeError("scheduler process killed")

    world.engine.cleanup = cleanup_then_die  # type: ignore[method-assign]
    try:
        report = await _janitor(world, repo_intel).run_once()
    finally:
        world.engine.cleanup = real_cleanup  # type: ignore[method-assign]
    assert set(report.failed) == {case.workspace_id}
    assert await _ws_status(world.sessionmaker, case.workspace_id) == "cleaned" and not Path(case.handle.path).exists()
    assert await _index_rows(world.sessionmaker, case.index_key) == (0, 0), "no orphaned index rows"
    # nothing is left for a later pass
    assert (await _janitor(world, repo_intel, retry_after_seconds=0).run_once()).cleaned == []
