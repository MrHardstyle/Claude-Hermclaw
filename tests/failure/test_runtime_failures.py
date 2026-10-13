"""P35 failure tests at runtime level: planner timeout (35.1), DB reconnect (35.7), full disk warning (35.9).

The other P35 scenarios are covered by the component failure suites (mapping in TEST_MATRIX.md)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import select, update

from hermclaw.core.config import get_config
from hermclaw.core.errors import ModelTimeout
from hermclaw.persistence.models import Event, Job
from hermclaw.runtime.health import DiskMonitor
from hermclaw.runtime.main import SchedulerProcess
from hermclaw.scheduler.scheduler import Scheduler, SchedulerSettings
from tests.conftest import ADMIN_URL
from tests.integration.runtime_stack import StackModels, checked_plan
from tests.integration.test_gitops_support import World, make_world
from tests.integration.test_runtime_driver import Chat, _driver, _job, _run
from tests.integration.test_runtime_services import overrides, stack_settings
from tests.integration.test_scheduler import FakeDriver, FakeHandler, StepSpec

pytestmark = pytest.mark.asyncio(loop_scope="session")
TERMINAL = ("succeeded", "failed", "cancelled")
GB = 1024**3


@pytest.fixture
async def world(sessionmaker: Any, tmp_path: Path) -> World:
    async with sessionmaker() as s:
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        await s.commit()
    return await make_world(sessionmaker, tmp_path)


# ----------------------------------------------------------------------------------------------- 35.1
async def test_planner_timeout_fails_preparation_with_a_timeout_code(world: World, tmp_path: Path) -> None:
    """Gemma (and its gateway-side fallback) time out on every attempt: the job fails cleanly, nothing runs."""
    timeout = ModelTimeout("planner-gemma did not answer within 60s")
    chat = Chat([timeout, timeout, timeout, timeout])
    jid = await _job(world)
    jobs = await _run(world, _driver(world, chat, tmp_path, triage_enabled=False), [], [jid])
    assert jobs[jid].status == "failed", jobs[jid].status
    assert "TIMEOUT" in (jobs[jid].error_code or "") or "timeout" in (jobs[jid].error_message or "").lower(), (
        jobs[jid].error_code,
        jobs[jid].error_message,
    )


# ----------------------------------------------------------------------------------------------- 35.9
class FakeDisk:
    def __init__(self, free_gb: float) -> None:
        self.free_gb = free_gb

    def __call__(self, _path: Path) -> tuple[int, int, int]:
        free = int(self.free_gb * GB)
        return (100 * GB, 100 * GB - free, free)


async def _disk_events(sm: Any, source: str) -> list[Event]:
    async with sm() as s:
        return list(
            (await s.execute(select(Event).where(Event.event_type == "system.disk", Event.source_id == source).order_by(Event.sequence)))
            .scalars()
            .all()
        )


async def test_disk_monitor_warns_once_per_level_change_and_reports_recovery(sessionmaker: Any, tmp_path: Path) -> None:
    disk = FakeDisk(50)
    source = f"disk-{uuid.uuid4().hex[:6]}"
    mon = DiskMonitor(
        sessionmaker, [tmp_path / "missing" / "workspaces"], warn_free_mb=10_240, critical_free_mb=2_048, usage=disk, source_id=source
    )
    await mon.check_once()
    assert mon.admission_block() is None and await _disk_events(sessionmaker, source) == []
    disk.free_gb = 5
    await mon.check_once()
    await mon.check_once()  # unchanged level: no second event
    disk.free_gb = 1
    await mon.check_once()
    assert mon.admission_block() is not None and "MB free" in (mon.admission_block() or "")
    disk.free_gb = 40
    await mon.check_once()
    events = await _disk_events(sessionmaker, source)
    assert [e.payload["level"] for e in events] == ["warning", "critical", "ok"]
    assert [e.severity for e in events] == ["warning", "error", "info"]
    assert mon.admission_block() is None
    with pytest.raises(ValueError):
        DiskMonitor(sessionmaker, [tmp_path], warn_free_mb=1, critical_free_mb=2)


class _Services:
    def __init__(self, mon: DiskMonitor) -> None:
        self.mon = mon

    def admission_block(self) -> str | None:
        return self.mon.admission_block()


async def test_critical_disk_pauses_job_admission_until_space_returns(sessionmaker: Any, tmp_path: Path) -> None:
    async with sessionmaker() as s:
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        job = Job(title="disk", prompt="p")
        s.add(job)
        await s.commit()
    disk = FakeDisk(1)
    mon = DiskMonitor(sessionmaker, [tmp_path], usage=disk, source_id=f"disk-{uuid.uuid4().hex[:6]}")
    await mon.check_once()
    driver = FakeDriver(sessionmaker, plans={job.id: [StepSpec("S001")]})
    sched = Scheduler(
        sessionmaker, get_config(), [FakeHandler({"test"})], driver, settings=SchedulerSettings(poll_seconds=0.01), services=_Services(mon)
    )
    for _ in range(5):
        await sched.tick()
    async with sessionmaker() as s:
        assert (await s.execute(select(Job.status).where(Job.id == job.id))).scalar_one() == "queued", "no preparation while critical"
    disk.free_gb = 60
    await mon.check_once()
    async with asyncio.timeout(30):
        while True:
            await sched.tick()
            async with sessionmaker() as s:
                status = (await s.execute(select(Job.status).where(Job.id == job.id))).scalar_one()
            if status in TERMINAL and not sched._job_tasks:
                break
            await asyncio.sleep(0.02)
    await sched.wait_idle()
    assert status == "succeeded"


# ----------------------------------------------------------------------------------------------- 35.7
def _kill_connections(db_name: str) -> int:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (db_name,)
        ).fetchall()
    return len(rows)


async def test_db_connections_killed_mid_job_are_reestablished_and_the_job_finishes(world: World) -> None:
    models = StackModels([checked_plan()])
    proc = SchedulerProcess(
        get_config(),
        stack_settings(world),
        overrides=overrides(models),
        scheduler_settings=SchedulerSettings(poll_seconds=0.05, retry_backoff_seconds=(0,)),
    )
    db_name = proc.engine.url.database or ""
    async with world.sessionmaker() as s:
        job = Job(title="Set VALUE to two", prompt="Change VALUE in src/module.py to 2.", repository_id=world.repo.id)
        s.add(job)
        await s.commit()
    task = asyncio.create_task(proc.run())
    killed = 0
    try:
        async with asyncio.timeout(120):
            while True:
                async with world.sessionmaker() as s:
                    status = (await s.execute(select(Job.status).where(Job.id == job.id))).scalar_one()
                if status in TERMINAL:
                    break
                if status in ("planning", "running") and killed < 3:
                    killed += await asyncio.to_thread(_kill_connections, db_name)  # every backend incl. the scheduler's pool
                    await asyncio.sleep(0.5)
                await asyncio.sleep(0.1)
    finally:
        proc.stop()
        rc = await asyncio.wait_for(task, timeout=60)
    assert killed >= 1, "connections were actually terminated during the job"
    async with world.sessionmaker() as s:
        job_row = (await s.execute(select(Job).where(Job.id == job.id))).scalar_one()
    assert job_row.status == "succeeded", (job_row.error_code, job_row.error_message)
    assert rc == 0
