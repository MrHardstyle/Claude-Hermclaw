"""P10 10.9 idle sleep hooks against PostgreSQL: decisions, execution, races and failure handling."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import WorkerHeartbeat
from hermclaw.core.config import HostConfig, HostsConfig, WakeOnLanConfig, load_config
from hermclaw.persistence.models import Event, Job, ResourceLease, ResourceRequest, Step, StepAttempt, WakeEvent, Worker
from hermclaw.wol import IdleReason, IdleSleepPolicy, IdleSleepSettings, SleepResult
from hermclaw.wol.idle import worker_resource_names
from hermclaw.workers.registry import WorkerRegistry

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
CMD = "sudo /usr/bin/systemctl suspend"
LATER = timedelta(hours=2)


class Runner:
    """Test double of the SSH admin tool (RemoteCommandRunner)."""

    def __init__(self, result: tuple[int, str, str] = (0, "", ""), *, exc: Exception | None = None, delay: float = 0.0) -> None:
        self.result, self.exc, self.delay = result, exc, delay
        self.calls: list[tuple[str, str, float]] = []

    async def run(self, host_id: str, command: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append((host_id, command, timeout))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result


def host(worker_id: str, *, command: str | None = CMD, wol: bool = True, labels: dict[str, str] | None = None) -> HostConfig:
    return HostConfig(
        id=worker_id,
        address="127.0.0.1",
        role="execution_worker",
        worker_kind="execution",
        idle_sleep_command=command,
        wake_on_lan=WakeOnLanConfig(enabled=wol, mac="52:54:00:00:00:01" if wol else ""),
        labels=labels or {},
    )


def wid() -> str:
    return f"idle-{uuid.uuid4().hex[:10]}"


async def make_ready(sm: async_sessionmaker[AsyncSession], cfg: HostConfig, *, state: WorkerState = WorkerState.ready, **hb: Any) -> None:
    async with sm() as s:
        await WorkerRegistry().register_from_config(s, HostsConfig(hosts=[cfg]))
        await WorkerRegistry().ingest_heartbeat(
            s,
            WorkerHeartbeat(
                worker_id=cfg.id,
                hostname=cfg.id,
                kind=WorkerKind.execution,
                state=state,
                worker_version="0.1.0rc1",
                capabilities=["sandbox"],
                sent_at=datetime.now(UTC),
                **hb,
            ),
        )
        await s.commit()


def policy(
    sm: async_sessionmaker[AsyncSession], hosts: Sequence[HostConfig], runner: Runner, *, shift: timedelta = LATER, **kw: Any
) -> IdleSleepPolicy:
    settings = kw.pop("settings", IdleSleepSettings(idle_after_minutes=30, command_timeout_seconds=0.5))
    return IdleSleepPolicy(sm, HostsConfig(hosts=list(hosts)), runner, settings=settings, now=lambda: datetime.now(UTC) + shift, **kw)


async def state_of(sm: async_sessionmaker[AsyncSession], worker_id: str) -> str:
    async with sm() as s:
        row = await s.get(Worker, worker_id)
        assert row is not None
        return row.state


async def sleep_rows(sm: async_sessionmaker[AsyncSession], worker_id: str) -> list[str]:
    async with sm() as s:
        rows = await s.execute(
            select(WakeEvent.status).where(WakeEvent.worker_id == worker_id, WakeEvent.stage == "sleep").order_by(WakeEvent.created_at)
        )
        return list(rows.scalars())


async def events(sm: async_sessionmaker[AsyncSession], worker_id: str, event_type: str) -> list[Event]:
    async with sm() as s:
        stmt = select(Event).where(Event.source_id == worker_id, Event.event_type == event_type).order_by(Event.sequence)
        return list((await s.execute(stmt)).scalars())


async def add_job_step(sm: async_sessionmaker[AsyncSession], worker_id: str, *, status: str = "running") -> tuple[uuid.UUID, uuid.UUID]:
    async with sm() as s:
        job = Job(title="t", prompt="p", status="running")
        s.add(job)
        await s.flush()
        step = Step(
            job_id=job.id,
            step_key="S1",
            title="s",
            kind="implement",
            capability="sandbox",
            goal="g",
            status=status,
            assigned_worker_id=worker_id,
        )
        s.add(step)
        await s.commit()
        return job.id, step.id


async def add_lease(sm: async_sessionmaker[AsyncSession], resource: str, *, state: str = "active") -> uuid.UUID:
    async with sm() as s:
        lease = ResourceLease(
            resource=resource,
            resource_group=resource,
            owner_kind="exec",
            holder="test",
            priority=50,
            state=state,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        s.add(lease)
        await s.commit()
        return lease.id


# ============================================================================================== decisions
async def test_configuration_gates(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    a, b, c, d = wid(), wid(), wid(), wid()
    hosts = [host(a, command=None), host(b, command="systemctl suspend\nrm -rf /"), host(c, wol=False), host(d)]
    for h in hosts:
        await make_ready(sessionmaker, h)
    p = policy(sessionmaker, hosts, Runner())
    assert (await p.evaluate(a)).reason == IdleReason.not_configured
    assert (await p.evaluate("unknown-host")).reason == IdleReason.not_configured
    assert (await p.evaluate(b)).reason == IdleReason.invalid_command
    assert (await p.evaluate(c)).reason == IdleReason.wol_disabled  # never sleep what cannot be woken
    ok = await p.evaluate(d)
    assert ok.should_sleep and ok.reason == IdleReason.idle_timeout and ok.idle_seconds and ok.idle_seconds > 3600
    lax = policy(sessionmaker, hosts, Runner(), settings=IdleSleepSettings(require_wake_on_lan=False))
    assert (await lax.evaluate(c)).should_sleep
    assert [h.id for h in p.candidates()] == [b, c, d]


async def test_not_registered_and_state_gates(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    unreg, busy, active = wid(), wid(), wid()
    await make_ready(sessionmaker, host(busy), state=WorkerState.busy)
    await make_ready(sessionmaker, host(active), active_job="job-1", active_step="step-1")
    p = policy(sessionmaker, [host(unreg), host(busy), host(active)], Runner())
    assert (await p.evaluate(unreg)).reason == IdleReason.not_registered
    d = await p.evaluate(busy)
    assert d.reason == IdleReason.state_not_ready and d.state == "busy"
    assert (await p.evaluate(active)).reason == IdleReason.worker_reports_active_work


async def test_active_work_blocks_sleep(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    w = wid()
    res = f"code-executor-{uuid.uuid4().hex[:6]}"
    cfg = host(w, labels={"lease_resources": f" {res} , other-res"})
    await make_ready(sessionmaker, cfg)
    p = policy(sessionmaker, [cfg], Runner())
    assert (await p.evaluate(w)).should_sleep

    job_id, step_id = await add_job_step(sessionmaker, w, status="verifying")
    d = await p.evaluate(w)
    assert d.reason == IdleReason.active_steps and d.active_steps == 1
    async with sessionmaker() as s:
        step = await s.get(Step, step_id)
        assert step is not None
        step.status = "completed"
        s.add(StepAttempt(step_id=step_id, job_id=job_id, attempt_no=1, status="running", worker_id=w))
        await s.commit()
    d = await p.evaluate(w)
    assert d.reason == IdleReason.active_attempts and d.active_attempts == 1
    async with sessionmaker() as s:
        attempt = (await s.execute(select(StepAttempt).where(StepAttempt.step_id == step_id))).scalar_one()
        attempt.status, attempt.finished_at = "completed", datetime.now(UTC)
        await s.commit()

    lease_id = await add_lease(sessionmaker, res)
    d = await p.evaluate(w)
    assert d.reason == IdleReason.active_leases and d.active_leases == 1
    async with sessionmaker() as s:
        lease = await s.get(ResourceLease, lease_id)
        assert lease is not None
        lease.state, lease.released_at = "released", datetime.now(UTC)
        s.add(
            ResourceRequest(
                resource="other-res", owner_kind="exec", holder="t", priority=50, state="waiting", expires_at=datetime.now(UTC) + LATER * 2
            )
        )
        await s.commit()
    d = await p.evaluate(w)
    assert d.reason == IdleReason.pending_requests and d.pending_requests == 1
    async with sessionmaker() as s:
        for req in (await s.execute(select(ResourceRequest).where(ResourceRequest.resource == "other-res"))).scalars():
            req.state = "cancelled"
        await s.commit()
    assert (await p.evaluate(w)).should_sleep


async def test_recent_activity_postpones_sleep(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    w = wid()
    cfg = host(w)
    await make_ready(sessionmaker, cfg)
    p = policy(sessionmaker, [cfg], Runner(), shift=timedelta(0))  # real time: became ready just now
    d = await p.evaluate(w)
    assert not d.should_sleep and d.reason == IdleReason.recently_active
    assert d.idle_seconds is not None and d.idle_seconds < 60 and d.last_activity_at is not None
    # shifted clock (2 h later) is still within a 3 h idle threshold
    long_p = policy(sessionmaker, [cfg], Runner(), settings=IdleSleepSettings(idle_after_minutes=180))
    assert (await long_p.evaluate(w)).reason == IdleReason.recently_active


def test_worker_resource_names_from_models_and_labels() -> None:
    cfg = load_config(ROOT / "config")
    names = worker_resource_names(cfg, "model-224")
    assert {"model-224", "large-model-224", "small-model-224"} <= names
    hosts = HostsConfig(hosts=[host("exec-x", labels={"lease_resources": "code-executor-222, gpu-224"})])
    assert worker_resource_names(hosts, "exec-x") == {"exec-x", "code-executor-222", "gpu-224"}
    assert worker_resource_names(hosts, "unknown") == {"unknown"}


# ============================================================================================== execution
async def test_sleep_success(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    w = wid()
    cfg = host(w)
    await make_ready(sessionmaker, cfg)
    runner = Runner()
    out = await policy(sessionmaker, [cfg], runner).maybe_sleep(w)
    assert out.result == SleepResult.slept and out.executed and out.exit_code == 0
    assert runner.calls == [(w, CMD, 0.5)]
    assert await state_of(sessionmaker, w) == "sleeping"
    assert await sleep_rows(sessionmaker, w) == ["requested", "slept"]
    transitions = [(e.payload["from"], e.payload["to"], e.payload["reason"]) for e in await events(sessionmaker, w, EventType.WORKER_STATE)]
    assert ("ready", "sleeping", "idle_sleep") in transitions
    status = await events(sessionmaker, w, EventType.STATUS)
    assert [e.payload["result"] for e in status] == ["requested", "slept"]
    assert all(e.payload["action"] == "idle_sleep" for e in status)
    # a sleeping worker is not evaluated again
    again = await policy(sessionmaker, [cfg], runner).maybe_sleep(w)
    assert again.result == SleepResult.skipped and again.decision.reason == IdleReason.state_not_ready
    assert len(runner.calls) == 1


async def test_definite_failure_restores_ready_and_redacts_output(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    w = wid()
    cfg = host(w)
    await make_ready(sessionmaker, cfg)
    runner = Runner((1, "", "Failed to suspend: password=hunter2secret not accepted"))
    out = await policy(sessionmaker, [cfg], runner).maybe_sleep(w)
    assert out.result == SleepResult.failed and out.exit_code == 1
    assert await state_of(sessionmaker, w) == "ready"
    transitions = [(e.payload["from"], e.payload["to"], e.payload["reason"]) for e in await events(sessionmaker, w, EventType.WORKER_STATE)]
    assert ("sleeping", "ready", "idle_sleep_failed") in transitions
    async with sessionmaker() as s:
        row = (await s.execute(select(WakeEvent).where(WakeEvent.worker_id == w, WakeEvent.status == "failed"))).scalar_one()
    assert "hunter2secret" not in str(row.details) and "REDACTED" in row.details["stderr"]
    for e in await events(sessionmaker, w, EventType.STATUS):
        assert "hunter2secret" not in str(e.payload)


@pytest.mark.parametrize(
    "runner",
    [
        Runner((255, "", "Connection to host closed by remote host.")),
        Runner(exc=OSError("connection reset")),
        Runner(delay=5.0),  # exceeds command_timeout_seconds + guard
    ],
    ids=["ssh-255", "transport-error", "timeout"],
)
async def test_ambiguous_results_keep_sleeping(sessionmaker: async_sessionmaker[AsyncSession], runner: Runner) -> None:
    w = wid()
    cfg = host(w)
    await make_ready(sessionmaker, cfg)
    out = await policy(sessionmaker, [cfg], runner, settings=IdleSleepSettings(command_timeout_seconds=0.2)).maybe_sleep(w)
    assert out.result == SleepResult.unknown and out.error
    assert await state_of(sessionmaker, w) == "sleeping"
    assert await sleep_rows(sessionmaker, w) == ["requested", "unknown"]


async def test_heartbeat_after_failed_suspend_self_corrects(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """A host that did not actually suspend reports itself ready again with its next heartbeat."""
    w = wid()
    cfg = host(w)
    await make_ready(sessionmaker, cfg)
    await policy(sessionmaker, [cfg], Runner(exc=OSError("reset"))).maybe_sleep(w)
    assert await state_of(sessionmaker, w) == "sleeping"
    await make_ready(sessionmaker, cfg)
    assert await state_of(sessionmaker, w) == "ready"


class _RacingRegistry(WorkerRegistry):
    """Dispatch races the decision: a lease on the worker appears right after it was marked sleeping."""

    def __init__(self, sm: async_sessionmaker[AsyncSession], resource: str) -> None:
        super().__init__()
        self.sm, self.resource = sm, resource

    async def set_state(self, session: AsyncSession, worker_id: str, state: WorkerState | str, *, reason: str) -> bool:
        changed = await super().set_state(session, worker_id, state, reason=reason)
        await add_lease(self.sm, self.resource)
        return changed


async def test_work_arriving_during_sleep_request_aborts(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    w = wid()
    cfg = host(w, labels={"lease_resources": f"res-{w}"})
    await make_ready(sessionmaker, cfg)
    runner = Runner()
    out = await policy(sessionmaker, [cfg], runner, registry=_RacingRegistry(sessionmaker, f"res-{w}")).maybe_sleep(w)
    assert out.result == SleepResult.aborted and not out.executed
    assert runner.calls == []
    assert await state_of(sessionmaker, w) == "ready"
    assert await sleep_rows(sessionmaker, w) == ["requested", "aborted"]


async def test_concurrent_policies_sleep_a_worker_once(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    w = wid()
    cfg = host(w)
    await make_ready(sessionmaker, cfg)
    runner = Runner(delay=0.1)
    p1, p2 = policy(sessionmaker, [cfg], runner), policy(sessionmaker, [cfg], runner)
    outs = await asyncio.gather(p1.maybe_sleep(w), p2.maybe_sleep(w))
    assert sorted(o.result.value for o in outs) == ["skipped", "slept"]
    assert len(runner.calls) == 1


async def test_run_once_and_run_forever(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    a, b, c = wid(), wid(), wid()
    cfgs = [host(a), host(b, command=None), host(c)]
    for h in cfgs:
        await make_ready(sessionmaker, h)
    await add_job_step(sessionmaker, c)  # c has work
    runner = Runner()
    p = policy(sessionmaker, cfgs, runner)
    outs = await p.run_once()
    assert {o.decision.worker_id: o.result for o in outs} == {a: SleepResult.slept, c: SleepResult.skipped}
    assert [call[0] for call in runner.calls] == [a]

    stop = asyncio.Event()
    task = asyncio.create_task(p.run_forever(stop, interval_seconds=0.05))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert [call[0] for call in runner.calls] == [a]  # a sleeps, c still busy, b not configured
