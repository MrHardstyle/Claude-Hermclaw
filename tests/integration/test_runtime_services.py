"""Composition root (``hermclaw.runtime.services``) and the scheduler process (``hermclaw.runtime.main``).

A real job runs through :class:`SchedulerProcess` exactly as ``hermclaw scheduler`` wires it – GitEngine,
RepoIntelligence (lexical), ScopeEngine/Auditor, ToolEngine, ContextBuilder, VerifierAdapter with the local sandbox,
WorkspaceReviewer, ResourceManager (model leases), Planner/Replanner, RuntimeJobDriver, Scheduler, worker registry and
offline monitor. Only the models are scripted (tests/integration/runtime_stack.py)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from hermclaw.core.config import get_config
from hermclaw.core.settings import Settings
from hermclaw.persistence.models import Job, ResourceLease, Step, Worker
from hermclaw.runtime.main import SchedulerProcess, check_database
from hermclaw.runtime.services import ServiceOverrides, build_services, resource_holder_id
from hermclaw.scheduler.scheduler import SchedulerSettings
from hermclaw.workers.errors import WorkerUnreachable
from tests.integration.runtime_stack import StackModels, checked_plan
from tests.integration.test_gitops_support import World, git, make_world

pytestmark = pytest.mark.asyncio(loop_scope="session")
TERMINAL = ("succeeded", "failed", "cancelled")


@pytest.fixture
async def world(sessionmaker: Any, tmp_path: Path) -> World:
    async with sessionmaker() as s:  # the DB is shared per test session: neutralise other tests' open jobs
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        await s.commit()
    return await make_world(sessionmaker, tmp_path)


def stack_settings(world: World, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "data_dir": world.settings.data_dir,
        "instance_id": f"test-{uuid.uuid4().hex[:8]}",
        "execution_mode": "local",
        "wake_on_lan": False,
        "scheduler_poll_seconds": 0.05,
    }
    base.update(kw)
    return Settings(**base)


def overrides(models: StackModels) -> ServiceOverrides:
    return ServiceOverrides(chat=models, research_enabled=False, gitlab=None, lease_keeper_interval=0.2)


async def _submit(world: World) -> uuid.UUID:
    async with world.sessionmaker() as s:
        job = Job(title="Set VALUE to two", prompt="Change VALUE in src/module.py to 2.", repository_id=world.repo.id)
        s.add(job)
        await s.commit()
        return job.id


async def _wait_terminal(world: World, job_id: uuid.UUID, timeout: float = 90) -> Job:
    async with asyncio.timeout(timeout):
        while True:
            async with world.sessionmaker() as s:
                job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
            if job.status in TERMINAL:
                return job
            await asyncio.sleep(0.1)


async def test_build_services_wires_every_handler_and_the_execution_mode(world: World) -> None:
    cfg = get_config()
    services = build_services(cfg, stack_settings(world), world.sessionmaker, overrides=overrides(StackModels()))
    assert set(services.handled_kinds) >= {
        "implement",
        "documentation",
        "test",
        "verify",
        "review",
        "inventory",
        "discover",
        "image",
        "video",
    }
    assert "research" not in services.handled_kinds, "research disabled → no research handler"
    assert services.execution.mode == "local" and services.holder_id.startswith("scheduler@test-")
    prod = Settings(env="production", data_dir=world.settings.data_dir, instance_id="orchestrator-225", wake_on_lan=False)
    assert build_services(cfg, prod, world.sessionmaker, overrides=overrides(StackModels())).execution.mode == "remote"
    assert resource_holder_id(prod) == "scheduler@orchestrator-225", "stable across restarts of the same instance"
    await services.aclose()


async def test_remote_execution_without_a_ready_worker_is_reported_not_run_locally(world: World) -> None:
    cfg = get_config()
    prod = Settings(env="production", data_dir=world.settings.data_dir, instance_id=f"t-{uuid.uuid4().hex[:6]}", wake_on_lan=False)
    services = build_services(cfg, prod, world.sessionmaker, overrides=overrides(StackModels()))
    ws = await world.engine.create_workspace(await world.job(), world.repo.id)
    with pytest.raises(WorkerUnreachable):
        await services.execution.executor(await world.engine.handle(ws), None)
    await services.aclose()


async def test_scheduler_process_runs_a_job_end_to_end(world: World) -> None:
    models = StackModels([checked_plan()])
    settings = stack_settings(world)
    proc = SchedulerProcess(
        get_config(),
        settings,
        overrides=overrides(models),
        scheduler_settings=SchedulerSettings(poll_seconds=0.05, retry_backoff_seconds=(0,)),
    )
    assert await check_database(world.sessionmaker), "migrated schema"
    jid = await _submit(world)
    task = asyncio.create_task(proc.run())
    try:
        job = await _wait_terminal(world, jid)
    finally:
        proc.stop()
        rc = await asyncio.wait_for(task, timeout=60)
    assert rc == 0
    assert job.status == "succeeded", (job.error_code, job.error_message)
    assert models.review_calls >= 1 and "coder-main" in models.aliases
    async with world.sessionmaker() as s:
        steps = {r.step_key: r for r in (await s.execute(select(Step).where(Step.job_id == jid))).scalars()}
        leases = (
            (
                await s.execute(
                    select(ResourceLease).where(ResourceLease.owner_job_id == jid, ResourceLease.state.in_(["active", "preempting"]))
                )
            )
            .scalars()
            .all()
        )
        workers = {w.id for w in (await s.execute(select(Worker))).scalars()}
    assert steps["S001"].result["commit"] and steps["S002"].result["verification_run_id"] and steps["S003"].result["verdict"] == "pass"
    assert leases == [], "model leases are released"
    assert {"exec-222", "model-224"} <= workers, "workers registered from hosts.yaml at start"
    branch = git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare).strip().lstrip("* ").strip()
    assert git("show", f"{branch}:src/module.py", cwd=world.upstream.bare) == "VALUE = 2"
