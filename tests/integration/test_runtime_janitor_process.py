"""``hermclaw scheduler`` (:class:`SchedulerProcess`) runs the workspace janitor next to the disk monitor: an old
finished job's workspace (real clone + real index rows) is removed while the process runs, a recent one is kept, and
the janitor stops with the process."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.config import get_config
from hermclaw.core.settings import Settings
from hermclaw.persistence.models import CodeSymbol, Event, Job, Workspace
from hermclaw.repo_intelligence import RepoIntelConfig, RepoIntelligence
from hermclaw.runtime.main import SchedulerProcess
from hermclaw.runtime.services import ServiceOverrides
from hermclaw.scheduler.scheduler import SchedulerSettings
from tests.integration.runtime_stack import StackModels
from tests.integration.test_gitops_support import World, make_world

pytestmark = pytest.mark.integration
TERMINAL = ("succeeded", "failed", "cancelled")


@pytest.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path) -> World:
    async with sessionmaker() as s:  # the DB is shared per test session: neutralise other tests' open jobs
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        await s.commit()
    return await make_world(sessionmaker, tmp_path)


async def _finished_job(world: World, svc: RepoIntelligence, ago: timedelta) -> tuple[uuid.UUID, uuid.UUID, Path, str]:
    job_id = await world.job("finished job")
    ws = await world.engine.create_workspace(job_id, world.repo.id)
    handle = await world.engine.handle(ws.id)
    await svc.ensure_index(svc.target_of(handle))
    async with world.sessionmaker() as s:
        await s.execute(update(Job).where(Job.id == job_id).values(status="succeeded", finished_at=datetime.now(UTC) - ago))
        await s.commit()
    return job_id, ws.id, Path(handle.path), svc.index_key(handle)


async def _status(sm: async_sessionmaker[AsyncSession], ws_id: uuid.UUID) -> str:
    async with sm() as s:
        return str((await s.execute(select(Workspace.status).where(Workspace.id == ws_id))).scalar_one())


async def _symbols(sm: async_sessionmaker[AsyncSession], key: str) -> int:
    async with sm() as s:
        return int((await s.execute(select(func.count()).select_from(CodeSymbol).where(CodeSymbol.repository_key == key))).scalar_one())


async def test_scheduler_process_runs_and_stops_the_workspace_janitor(world: World) -> None:
    config = get_config()
    retention = config.policies.git.workspace_retention_hours
    assert retention > 0, "the shipped policy enables the retention"
    svc = RepoIntelligence(world.sessionmaker, config=RepoIntelConfig.from_hermclaw(config))  # same index keys as the runtime
    old_job, old_ws, old_path, old_key = await _finished_job(world, svc, timedelta(hours=retention + 1))
    new_job, new_ws, new_path, new_key = await _finished_job(world, svc, timedelta(hours=1))
    await svc.aclose()
    assert await _symbols(world.sessionmaker, old_key) > 0

    settings: dict[str, Any] = {
        "env": "test",
        "data_dir": world.settings.data_dir,  # same workspace root as the test's GitEngine
        "instance_id": f"test-{uuid.uuid4().hex[:8]}",
        "execution_mode": "local",
        "wake_on_lan": False,
        "scheduler_poll_seconds": 0.05,
    }
    proc = SchedulerProcess(
        config,
        Settings(**settings),
        overrides=ServiceOverrides(chat=StackModels(), research_enabled=False, gitlab=None, lease_keeper_interval=0.2),
        scheduler_settings=SchedulerSettings(poll_seconds=0.05, retry_backoff_seconds=(0,)),
    )
    task = asyncio.create_task(proc.run())
    try:
        async with asyncio.timeout(60):
            while await _status(world.sessionmaker, old_ws) != "cleaned":
                assert not task.done(), "the scheduler process ended early"
                await asyncio.sleep(0.05)
        assert proc.janitor is not None and proc.janitor.running
        assert proc.janitor.retention_hours == retention
    finally:
        proc.stop()
        rc = await asyncio.wait_for(task, timeout=60)
    assert rc == 0
    assert proc.janitor is not None and not proc.janitor.running, "the janitor stops with the process"
    assert not old_path.exists() and await _symbols(world.sessionmaker, old_key) == 0
    async with world.sessionmaker() as s:
        events = (
            (await s.execute(select(Event).where(Event.job_id == old_job, Event.event_type == EventType.WORKSPACE_CLEANED))).scalars().all()
        )
    assert len(events) == 1 and events[0].payload["workspace_id"] == str(old_ws)
    assert proc.services is not None and events[0].source_id == proc.services.holder_id
    # the recent job's workspace and index are untouched
    assert new_path.is_dir() and await _status(world.sessionmaker, new_ws) == "active"
    assert await _symbols(world.sessionmaker, new_key) > 0
    async with world.sessionmaker() as s:
        assert (
            await s.execute(select(Event).where(Event.job_id == new_job, Event.event_type == EventType.WORKSPACE_CLEANED))
        ).first() is None
