"""P34 recovery: the real scheduler process (``SchedulerProcess`` in a child interpreter) is killed with SIGKILL in
the middle of a job and restarted with the same instance id.

Scenario A (34.1–34.7): killed while the coder works on S001 (the edit is in the workspace, uncommitted; the coder's
model lease is held). After the restart the lost attempt is marked ``WORKER_LOST``, the lease of the dead
incarnation is released, the step re-runs on the intact workspace, verification + commit happen exactly once and
the job succeeds; the restarted process stops gracefully on SIGTERM.

Scenario B: killed while Gemma plans (preparation phase) – preparation is re-entrant and the job succeeds.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from hermclaw.persistence.models import Event, Job, ResourceLease, Step, StepAttempt, Workspace
from tests.integration.test_gitops_support import World, git, make_world

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.slow]
ROOT = Path(__file__).resolve().parents[2]
TERMINAL = ("succeeded", "failed", "cancelled")


@pytest.fixture
async def world(sessionmaker: Any, tmp_path: Path) -> World:
    async with sessionmaker() as s:
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        await s.commit()
    return await make_world(sessionmaker, tmp_path)


class Child:
    def __init__(self, world: World, instance: str, log: Path, *args: str) -> None:
        self.world, self.instance, self.log, self.args = world, instance, log, args
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> Child:
        env = {**os.environ, "PYTHONPATH": str(ROOT), "HERMCLAW_ENV": "test"}
        self._fh = self.log.open("wb")
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.integration.scheduler_child",
            "--data-dir",
            str(self.world.settings.data_dir),
            "--instance",
            self.instance,
            *self.args,
            cwd=str(ROOT),
            env=env,
            stdout=self._fh,
            stderr=asyncio.subprocess.STDOUT,
        )
        return self

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")[-4000:]

    async def wait_for(self, marker: Path, timeout: float = 120) -> None:
        assert self.proc is not None
        async with asyncio.timeout(timeout):
            while not marker.exists():
                if self.proc.returncode is not None:
                    raise AssertionError(f"child exited early ({self.proc.returncode}):\n{self.output()}")
                await asyncio.sleep(0.1)

    async def kill(self) -> None:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGKILL)
        await self.proc.wait()
        self._fh.close()

    async def terminate(self, timeout: float = 90) -> int:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGTERM)
        rc = await asyncio.wait_for(self.proc.wait(), timeout=timeout)
        self._fh.close()
        return rc


async def _submit(world: World) -> uuid.UUID:
    async with world.sessionmaker() as s:
        job = Job(title="Set VALUE to two", prompt="Change VALUE in src/module.py to 2.", repository_id=world.repo.id)
        s.add(job)
        await s.commit()
        return job.id


async def _wait_terminal(world: World, job_id: uuid.UUID, child: Child, timeout: float = 150) -> Job:
    try:
        async with asyncio.timeout(timeout):
            while True:
                async with world.sessionmaker() as s:
                    job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
                if job.status in TERMINAL:
                    return job
                await asyncio.sleep(0.2)
    except TimeoutError as exc:
        raise AssertionError(f"job did not finish; child output:\n{child.output()}") from exc


def _branch_commits(world: World) -> list[str]:
    branch = git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare).strip().lstrip("* ").strip()
    return git("log", "--format=%s", f"main..{branch}", cwd=world.upstream.bare).splitlines()


async def test_kill_during_coder_step_restart_recovers_and_finishes(world: World, tmp_path: Path) -> None:
    instance = f"recovery-{uuid.uuid4().hex[:6]}"
    marker = tmp_path / "coder-hang"
    jid = await _submit(world)
    first = await Child(world, instance, tmp_path / "child1.log", "--plan", "--hang-marker", str(marker), "--hang-on", "coder").start()
    await first.wait_for(marker)
    # state at the moment of the crash: S001 in flight, edit in the workspace, coder model lease held
    async with world.sessionmaker() as s:
        s1 = (await s.execute(select(Step).where(Step.job_id == jid, Step.step_key == "S001"))).scalar_one()
        ws = (await s.execute(select(Workspace).where(Workspace.job_id == jid))).scalar_one()
        held = (
            (await s.execute(select(ResourceLease).where(ResourceLease.owner_job_id == jid, ResourceLease.state == "active")))
            .scalars()
            .all()
        )
    assert s1.status in ("running", "leased", "verifying"), s1.status
    assert Path(ws.path, "src/module.py").read_text(encoding="utf-8").strip() == "VALUE = 2", "edit applied before the crash"
    assert held and all(lease.holder == f"scheduler@{instance}" for lease in held), "coder lease of the dead incarnation"
    await first.kill()

    second = await Child(world, instance, tmp_path / "child2.log").start()
    try:
        job = await _wait_terminal(world, jid, second)
    finally:
        rc = await second.terminate()
    assert job.status == "succeeded", (job.error_code, job.error_message, second.output())
    assert rc == 0, second.output()
    async with world.sessionmaker() as s:
        attempts = (
            (
                await s.execute(
                    select(StepAttempt).where(StepAttempt.step_id == s1.id).order_by(StepAttempt.attempt_no, StepAttempt.started_at)
                )
            )
            .scalars()
            .all()
        )
        leases = (await s.execute(select(ResourceLease).where(ResourceLease.owner_job_id == jid))).scalars().all()
        ws_after = (await s.execute(select(Workspace).where(Workspace.job_id == jid))).scalar_one()
        steps = {r.step_key: r.status for r in (await s.execute(select(Step).where(Step.job_id == jid))).scalars()}
    assert attempts[0].status == "lost" and attempts[0].error_code == "WORKER_LOST", [(a.status, a.error_code) for a in attempts]
    assert attempts[-1].status == "completed" or attempts[-1].outcome == "completed", [(a.status, a.outcome) for a in attempts]
    assert all(lease.state not in ("active", "preempting") for lease in leases), [(lease.state, lease.holder) for lease in leases]
    assert steps == {"S001": "completed", "S002": "completed", "S003": "completed"}
    assert ws_after.path == ws.path, "the same workspace is reused (no re-clone)"
    commits = _branch_commits(world)
    assert len(commits) == 1, f"exactly one verified commit for S001, got {commits}"


async def test_kill_during_planning_is_reentrant(world: World, tmp_path: Path) -> None:
    instance = f"recovery-{uuid.uuid4().hex[:6]}"
    marker = tmp_path / "plan-hang"
    jid = await _submit(world)
    first = await Child(world, instance, tmp_path / "child1.log", "--plan", "--hang-marker", str(marker), "--hang-on", "plan").start()
    await first.wait_for(marker)
    async with world.sessionmaker() as s:
        status = (await s.execute(select(Job.status).where(Job.id == jid))).scalar_one()
    assert status in ("planning", "inventory", "discovering", "queued", "preparing"), status
    await first.kill()
    second = await Child(world, instance, tmp_path / "child2.log", "--plan").start()
    try:
        job = await _wait_terminal(world, jid, second)
    finally:
        await second.terminate()
    assert job.status == "succeeded", (job.error_code, job.error_message, second.output())
    async with world.sessionmaker() as s:
        recovered = (await s.execute(select(Event).where(Event.job_id == jid, Event.event_type == "job.recovered"))).scalars().all()
    assert recovered, "the restarted scheduler records the recovery of the preparation phase"
    assert len(_branch_commits(world)) == 1
