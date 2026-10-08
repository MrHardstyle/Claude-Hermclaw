"""DAG scheduler integration tests (P25) against real PostgreSQL with fake handlers/driver (test-only fakes)."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from hermclaw.contracts.common import JobStatus, StepStatus
from hermclaw.core.config import get_config
from hermclaw.persistence.models import Event, Job, Step, StepAttempt, StepDependency
from hermclaw.runtime.transitions import transition_job
from hermclaw.scheduler.handlers import CancelToken, JobPhaseResult, StepOutcome, StepRunContext
from hermclaw.scheduler.scheduler import Scheduler, SchedulerSettings

pytestmark = pytest.mark.asyncio(loop_scope="session")

TERMINAL = ("succeeded", "failed", "cancelled")


# ----------------------------------------------------------------------------- fakes (test code only)
@dataclass
class StepSpec:
    key: str
    kind: str = "test"
    deps: tuple[str, ...] = ()
    max_attempts: int = 3


@dataclass
class FakeDriver:
    sm: Any
    plans: dict[uuid.UUID, list[StepSpec]] = field(default_factory=dict)
    replans: dict[uuid.UUID, list[list[StepSpec]]] = field(default_factory=dict)
    finalized: list[uuid.UUID] = field(default_factory=list)
    replan_calls: list[tuple[uuid.UUID, str, dict[str, Any]]] = field(default_factory=list)
    finalize_ok: bool = True
    prepare_gate: asyncio.Event | None = None

    async def _insert(self, s: Any, job_id: uuid.UUID, specs: list[StepSpec]) -> None:
        ids: dict[str, uuid.UUID] = {}
        for sp in specs:
            st = Step(
                job_id=job_id,
                step_key=sp.key,
                title=sp.key,
                kind=sp.kind,
                capability="test",
                goal=f"goal {sp.key}",
                max_attempts=sp.max_attempts,
            )
            s.add(st)
            await s.flush()
            ids[sp.key] = st.id
        for sp in specs:
            for d in sp.deps:
                s.add(StepDependency(step_id=ids[sp.key], depends_on_step_id=ids[d]))

    async def prepare(self, job_id: uuid.UUID, token: CancelToken) -> JobPhaseResult:
        if self.prepare_gate is not None:
            await self.prepare_gate.wait()
        async with self.sm() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
            await transition_job(s, job, JobStatus.planning, reason="fake planning")
            await self._insert(s, job_id, self.plans.get(job_id, []))
            await s.commit()
        return JobPhaseResult(ok=True)

    async def finalize(self, job_id: uuid.UUID, token: CancelToken) -> JobPhaseResult:
        self.finalized.append(job_id)
        return JobPhaseResult(ok=self.finalize_ok, error_code=None if self.finalize_ok else "PUSH_FAILED")

    async def replan(self, job_id: uuid.UUID, reason: str, evidence: dict[str, Any], token: CancelToken) -> JobPhaseResult:
        self.replan_calls.append((job_id, reason, evidence))
        queue = self.replans.get(job_id) or []
        if not queue:
            return JobPhaseResult(ok=False, error_code="NO_PLAN")
        specs = queue.pop(0)
        async with self.sm() as s:
            await s.execute(
                update(Step)
                .where(Step.job_id == job_id, Step.status != StepStatus.completed.value, Step.superseded.is_(False))
                .values(superseded=True)
            )
            await self._insert(s, job_id, specs)
            await s.commit()
        return JobPhaseResult(ok=True)


class FakeHandler:
    """Behaviour per step_key: list of outcomes consumed per attempt (last one repeats)."""

    def __init__(self, kinds: set[str], script: dict[str, list[str]] | None = None, delay: float = 0.0) -> None:
        self.kinds = frozenset(kinds)
        self.script = script or {}
        self.delay = delay
        self.calls: list[tuple[str, int, str]] = []
        self.active: dict[uuid.UUID, int] = defaultdict(int)
        self.max_active: dict[uuid.UUID, int] = defaultdict(int)
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        self.calls.append((ctx.step_key, ctx.attempt_no, ctx.attempt_kind))
        self.active[ctx.job_id] += 1
        self.max_active[ctx.job_id] = max(self.max_active[ctx.job_id], self.active[ctx.job_id])
        self.started.set()
        try:
            if self.release is not None:
                while not self.release.is_set():
                    if ctx.token.cancelled:
                        return StepOutcome("cancelled", summary=ctx.token.reason)
                    if ctx.token.checkpoint_requested:
                        return StepOutcome("checkpointed", summary="paused", checkpoint={"progress": ctx.attempt_no})
                    await asyncio.sleep(0.02)
            if self.delay:
                await asyncio.sleep(self.delay)
            seq = self.script.get(ctx.step_key, ["completed"])
            what = seq[min(ctx.attempt_no - 1, len(seq) - 1)]
            if what == "crash":
                raise RuntimeError("handler exploded")
            if what == "fail_retry":
                return StepOutcome("failed", error_code="FLAKY", error_message="transient", retryable=True)
            if what == "fail":
                return StepOutcome("failed", error_code="BROKEN", error_message="permanent")
            if what == "blocked":
                return StepOutcome("blocked", error_code="SCOPE", replan_reason="needs scope expansion", replan_evidence={"path": "x"})
            return StepOutcome("completed", summary=f"{ctx.step_key} done", result={"ok": True})
        finally:
            self.active[ctx.job_id] -= 1


# ----------------------------------------------------------------------------- helpers
@pytest.fixture
async def clean_jobs(sessionmaker: Any) -> None:
    """The DB is shared per test session: neutralise non-terminal jobs left by other tests."""
    async with sessionmaker() as s:
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        await s.commit()


async def _new_job(sm: Any, title: str = "job", priority: int = 50) -> uuid.UUID:
    async with sm() as s:
        job = Job(title=title, prompt="do things", priority=priority)
        s.add(job)
        await s.commit()
        return job.id


def _settings(**kw: Any) -> SchedulerSettings:
    base: dict[str, Any] = {"concurrency": 4, "poll_seconds": 0.01, "retry_backoff_seconds": (0, 0, 0), "step_heartbeat_seconds": 1}
    base.update(kw)
    return SchedulerSettings(**base)


async def _run_until(sched: Scheduler, sm: Any, job_ids: list[uuid.UUID], *, timeout: float = 20.0) -> dict[uuid.UUID, Job]:
    async with asyncio.timeout(timeout):
        while True:
            await sched.tick()
            async with sm() as s:
                jobs = {j.id: j for j in (await s.execute(select(Job).where(Job.id.in_(job_ids)))).scalars()}
            if all(j.status in TERMINAL for j in jobs.values()) and not sched._job_tasks:
                await sched.wait_idle()
                return jobs
            await asyncio.sleep(0.01)


async def _steps(sm: Any, job_id: uuid.UUID, *, include_superseded: bool = False) -> dict[str, Step]:
    async with sm() as s:
        stmt = select(Step).where(Step.job_id == job_id)
        if not include_superseded:
            stmt = stmt.where(Step.superseded.is_(False))
        return {st.step_key: st for st in (await s.execute(stmt)).scalars()}


async def _event_types(sm: Any, job_id: uuid.UUID) -> list[str]:
    async with sm() as s:
        return list((await s.execute(select(Event.event_type).where(Event.job_id == job_id).order_by(Event.sequence))).scalars())


# ----------------------------------------------------------------------------- tests
async def test_dag_runs_in_dependency_order_and_finalizes(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    h = FakeHandler({"test", "implement"})
    jid = await _new_job(sm)
    driver.plans[jid] = [
        StepSpec("S1", "implement"),
        StepSpec("S2", "test", ("S1",)),
        StepSpec("S3", "test", ("S1",)),
        StepSpec("S4", "implement", ("S2", "S3")),
    ]
    sched = Scheduler(sm, get_config(), [h], driver, settings=_settings())
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "succeeded"
    order = [c[0] for c in h.calls]
    assert order[0] == "S1" and order[-1] == "S4" and set(order[1:3]) == {"S2", "S3"}
    assert driver.finalized == [jid]
    steps = await _steps(sm, jid)
    assert all(st.status == "completed" and st.result == {"ok": True} for st in steps.values())
    types = await _event_types(sm, jid)
    assert "attempt.started" in types and "attempt.finished" in types and "job.succeeded" in types


async def test_mutating_steps_are_serialised_per_job(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    mut = FakeHandler({"implement"}, delay=0.15)
    ro = FakeHandler({"test"}, delay=0.15)
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1", "implement"), StepSpec("S2", "implement"), StepSpec("S3", "test"), StepSpec("S4", "test")]
    sched = Scheduler(sm, get_config(), [mut, ro], driver, settings=_settings())
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "succeeded"
    assert mut.max_active[jid] == 1, "two mutating steps of one job ran concurrently"
    assert ro.max_active[jid] == 2, "read-only steps should run in parallel"


async def test_retry_with_backoff_then_success(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    h = FakeHandler({"test"}, script={"S1": ["fail_retry", "crash", "completed"]})
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1"), StepSpec("S2", deps=("S1",))]
    sched = Scheduler(sm, get_config(), [h], driver, settings=_settings())
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "succeeded"
    assert [c for c in h.calls if c[0] == "S1"] == [("S1", 1, "initial"), ("S1", 2, "retry"), ("S1", 3, "retry")]
    async with sm() as s:
        attempts = (
            (
                await s.execute(
                    select(StepAttempt).join(Step, Step.id == StepAttempt.step_id).where(Step.job_id == jid, Step.step_key == "S1")
                )
            )
            .scalars()
            .all()
        )
    assert sorted((a.attempt_no, a.outcome, a.error_code) for a in attempts) == [
        (1, "failed", "FLAKY"),
        (2, "failed", "HANDLER_CRASHED"),
        (3, "completed", None),
    ]


async def test_backoff_delays_redispatch(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    h = FakeHandler({"test"}, script={"S1": ["fail_retry", "completed"]})
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1")]
    sched = Scheduler(sm, get_config(), [h], driver, settings=_settings(retry_backoff_seconds=(3600,)))
    for _ in range(30):
        await sched.tick()
        await asyncio.sleep(0.01)
    await sched.wait_idle()
    for _ in range(5):
        await sched.tick()
    st = (await _steps(sm, jid))["S1"]
    assert st.status == "failed" and st.not_before is not None and st.not_before > datetime.now(UTC) + timedelta(minutes=50)
    assert len(h.calls) == 1
    async with sm() as s:
        assert (await s.execute(select(Job.status).where(Job.id == jid))).scalar_one() == "running", "retry pending must not replan"
        await s.execute(update(Step).where(Step.id == st.id).values(not_before=datetime.now(UTC) - timedelta(seconds=1)))
        await s.commit()
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "succeeded" and len(h.calls) == 2


async def test_permanent_failure_blocks_dependents_then_replans(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    h = FakeHandler({"test"}, script={"S2": ["fail"]})
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1"), StepSpec("S2", deps=("S1",)), StepSpec("S3", deps=("S2",))]
    driver.replans[jid] = [[StepSpec("S4"), StepSpec("S5", deps=("S4",))]]
    sched = Scheduler(sm, get_config(), [h], driver, settings=_settings())
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "succeeded"
    assert jobs[jid].replan_count == 1
    (call_job, reason, evidence) = driver.replan_calls[0]
    assert call_job == jid and reason == "blocked_or_failed_steps"
    keys = {f["step_key"]: f for f in evidence["failed_steps"]}
    assert keys["S2"]["error_code"] == "BROKEN" and keys["S3"]["error_code"] == "DEPENDENCY_FAILED"
    old = await _steps(sm, jid, include_superseded=True)
    assert old["S3"].superseded and old["S3"].status == "blocked"
    assert {k for k, v in (await _steps(sm, jid)).items()} == {"S1", "S4", "S5"}


async def test_replan_budget_exhausted_fails_job(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    h = FakeHandler({"test"}, script={"S1": ["blocked"], "R1": ["blocked"], "R2": ["blocked"]})
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1")]
    driver.replans[jid] = [[StepSpec("R1")], [StepSpec("R2")], [StepSpec("R3")]]
    sched = Scheduler(sm, get_config(), [h], driver, settings=_settings())
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "failed" and jobs[jid].error_code == "REPLAN_LIMIT"
    assert jobs[jid].replan_count == get_config().policies.correction.max_replans_per_job
    blocked = (await _steps(sm, jid))["R2"]
    assert blocked.result["replan_reason"] == "needs scope expansion"


async def test_cancel_running_job(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    h = FakeHandler({"test"})
    h.release = asyncio.Event()
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1"), StepSpec("S2", deps=("S1",))]
    sched = Scheduler(sm, get_config(), [h], driver, settings=_settings())
    async with asyncio.timeout(10):
        while not h.started.is_set():
            await sched.tick()
            await asyncio.sleep(0.01)
    async with sm() as s:
        await s.execute(update(Job).where(Job.id == jid).values(cancel_requested=True))
        await s.commit()
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "cancelled"
    steps = await _steps(sm, jid)
    assert steps["S1"].status == "cancelled" and steps["S2"].status == "cancelled"
    assert driver.finalized == []


async def test_pause_checkpoints_and_resume_continues(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    h = FakeHandler({"test"})
    h.release = asyncio.Event()
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1")]
    sched = Scheduler(sm, get_config(), [h], driver, settings=_settings())
    async with asyncio.timeout(10):
        while not h.started.is_set():
            await sched.tick()
            await asyncio.sleep(0.01)
    async with sm() as s:
        await s.execute(update(Job).where(Job.id == jid).values(pause_requested=True))
        await s.commit()
    for _ in range(20):
        await sched.tick()
        await asyncio.sleep(0.02)
    await sched.wait_idle()
    st = (await _steps(sm, jid))["S1"]
    assert st.status == "checkpointed" and st.checkpoint == {"progress": 1}
    assert len(h.calls) == 1, "paused job must not be re-dispatched"
    h.release.set()
    async with sm() as s:
        await s.execute(update(Job).where(Job.id == jid).values(pause_requested=False))
        await s.commit()
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "succeeded"
    assert h.calls[-1] == ("S1", 2, "resume")


async def test_crash_recovery_requeues_in_flight_steps(sessionmaker: Any, clean_jobs: None) -> None:
    """A step left 'running' by a dead scheduler instance with an expired lease is recovered by another instance."""
    sm = sessionmaker
    driver = FakeDriver(sm)
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1"), StepSpec("S2")]
    dead = Scheduler(sm, get_config(), [FakeHandler({"test"})], driver, settings=_settings(), instance_id="dead")
    await dead.tick()  # prepare
    await dead.wait_idle()
    async with sm() as s:
        job = (await s.execute(select(Job).where(Job.id == jid))).scalar_one()
        assert job.status == "running"
        steps = {st.step_key: st for st in (await s.execute(select(Step).where(Step.job_id == jid))).scalars()}
        # simulate crash: S1 mid-run with an expired lease, S2 in 'testing' sub-state with checkpoint
        for key, status, ckpt in (("S1", "running", {}), ("S2", "testing", {"turn": 3})):
            st = steps[key]
            st.status = status
            st.checkpoint = ckpt
            st.lease_owner = "dead"
            st.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
            st.attempt_count = 1
            s.add(StepAttempt(step_id=st.id, job_id=jid, attempt_no=1, kind="initial", status="running"))
        await s.commit()
    h = FakeHandler({"test"})
    alive = Scheduler(sm, get_config(), [h], driver, settings=_settings(), instance_id="alive")
    jobs = await _run_until(alive, sm, [jid])
    assert jobs[jid].status == "succeeded"
    assert sorted(h.calls) == [("S1", 2, "retry"), ("S2", 2, "resume")]
    async with sm() as s:
        lost = (await s.execute(select(StepAttempt).where(StepAttempt.job_id == jid, StepAttempt.attempt_no == 1))).scalars().all()
    assert {a.outcome for a in lost} == {"lost"} and {a.error_code for a in lost} == {"WORKER_LOST"}


async def test_live_lease_of_other_instance_is_respected(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1")]
    first = Scheduler(sm, get_config(), [FakeHandler({"test"})], driver, settings=_settings(), instance_id="first")
    await first.tick()
    await first.wait_idle()
    async with sm() as s:
        st = (await s.execute(select(Step).where(Step.job_id == jid))).scalar_one()
        st.status = "running"
        st.lease_owner = "other-live"
        st.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await s.commit()
    h = FakeHandler({"test"})
    second = Scheduler(sm, get_config(), [h], driver, settings=_settings(), instance_id="second")
    assert await second.startup_recovery() == 0
    for _ in range(5):
        await second.tick()
    assert h.calls == []
    assert (await _steps(sm, jid))["S1"].status == "running"


async def test_orphaned_finalize_is_resumed(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    jid = await _new_job(sm)
    async with sm() as s:
        job = (await s.execute(select(Job).where(Job.id == jid))).scalar_one()
        for target in (JobStatus.planning, JobStatus.running, JobStatus.committing):
            await transition_job(s, job, target)
        job.lock_owner = "dead"
        job.lock_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
    sched = Scheduler(sm, get_config(), [FakeHandler({"test"})], driver, settings=_settings())
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "succeeded" and driver.finalized == [jid]
    assert "job.recovered" in await _event_types(sm, jid)


async def test_finalize_failure_fails_job(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm, finalize_ok=False)
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1")]
    sched = Scheduler(sm, get_config(), [FakeHandler({"test"})], driver, settings=_settings())
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "failed" and jobs[jid].error_code == "PUSH_FAILED"


async def test_missing_handler_blocks_step_and_empty_plan_fails(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    j1 = await _new_job(sm, "nohandler")
    j2 = await _new_job(sm, "empty")
    driver.plans[j1] = [StepSpec("S1", "video")]
    driver.plans[j2] = []
    sched = Scheduler(sm, get_config(), [FakeHandler({"test"})], driver, settings=_settings())
    jobs = await _run_until(sched, sm, [j1, j2])
    assert jobs[j2].status == "failed" and jobs[j2].error_code == "EMPTY_PLAN"
    assert jobs[j1].status == "failed"  # NO_HANDLER -> blocked -> replan -> driver has no replan -> failed
    assert (await _steps(sm, j1))["S1"].error_code == "NO_HANDLER"


async def test_prepare_crash_and_invalid_state_fail_job(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker

    class CrashDriver(FakeDriver):
        async def prepare(self, job_id: uuid.UUID, token: CancelToken) -> JobPhaseResult:
            raise ValueError("planner down")

    class NoopDriver(FakeDriver):
        async def prepare(self, job_id: uuid.UUID, token: CancelToken) -> JobPhaseResult:
            return JobPhaseResult(ok=True)  # never moved the job out of 'queued'

    j1 = await _new_job(sm)
    jobs = await _run_until(Scheduler(sm, get_config(), [], CrashDriver(sm), settings=_settings()), sm, [j1])
    assert jobs[j1].status == "failed" and jobs[j1].error_code == "PREPARE_CRASHED" and "planner down" in (jobs[j1].error_message or "")
    j2 = await _new_job(sm)
    jobs = await _run_until(Scheduler(sm, get_config(), [], NoopDriver(sm), settings=_settings()), sm, [j2])
    assert jobs[j2].status == "failed" and jobs[j2].error_code == "PREPARE_INVALID_STATE"


async def test_priority_and_concurrency_limit(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    h = FakeHandler({"test"}, delay=0.1)
    low = await _new_job(sm, "low", priority=10)
    high = await _new_job(sm, "high", priority=90)
    driver.plans[low] = [StepSpec("S1"), StepSpec("S2")]
    driver.plans[high] = [StepSpec("S1"), StepSpec("S2")]
    sched = Scheduler(sm, get_config(), [h], driver, settings=_settings(concurrency=1))
    peak = 0

    async def watch() -> None:
        nonlocal peak
        while True:
            peak = max(peak, len(sched._running))
            await asyncio.sleep(0.005)

    w = asyncio.create_task(watch())
    try:
        jobs = await _run_until(sched, sm, [low, high])
    finally:
        w.cancel()
    assert jobs[low].status == jobs[high].status == "succeeded"
    assert peak == 1
    async with sm() as s:
        rows = (
            (
                await s.execute(
                    select(StepAttempt.job_id).where(StepAttempt.job_id.in_([low, high])).order_by(StepAttempt.started_at, StepAttempt.id)
                )
            )
            .scalars()
            .all()
        )
    assert rows[0] == high, "higher priority job must be dispatched first"


async def test_manual_replan_request(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)
    h = FakeHandler({"test"})
    h.release = asyncio.Event()
    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1"), StepSpec("S2", deps=("S1",))]
    driver.replans[jid] = [[StepSpec("N1")]]
    sched = Scheduler(sm, get_config(), [h], driver, settings=_settings())
    async with asyncio.timeout(10):
        while not h.started.is_set():
            await sched.tick()
            await asyncio.sleep(0.01)
    async with sm() as s:
        job = (await s.execute(select(Job).where(Job.id == jid))).scalar_one()
        job.metadata_ = {**job.metadata_, "replan_requested": "operator wants a different approach"}
        await s.commit()
    for _ in range(10):
        await sched.tick()  # must wait for the running step
    assert driver.replan_calls == []
    h.release.set()
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "succeeded"
    assert driver.replan_calls[0][1] == "operator wants a different approach"
    assert set(await _steps(sm, jid)) == {"S1", "N1"}


async def test_step_timeout_is_retryable_failure(sessionmaker: Any, clean_jobs: None) -> None:
    sm = sessionmaker
    driver = FakeDriver(sm)

    class SlowOnce:
        kinds = frozenset({"test"})
        calls = 0

        async def run(self, ctx: StepRunContext) -> StepOutcome:
            SlowOnce.calls += 1
            if ctx.attempt_no == 1:
                await asyncio.sleep(30)
            return StepOutcome("completed", summary="fast now")

    jid = await _new_job(sm)
    driver.plans[jid] = [StepSpec("S1")]
    sched = Scheduler(sm, get_config(), [SlowOnce()], driver, settings=_settings(step_timeout_seconds=1))
    jobs = await _run_until(sched, sm, [jid])
    assert jobs[jid].status == "succeeded" and SlowOnce.calls == 2
    async with sm() as s:
        first = (await s.execute(select(StepAttempt).where(StepAttempt.job_id == jid, StepAttempt.attempt_no == 1))).scalar_one()
    assert first.outcome == "failed" and first.error_code == "STEP_TIMEOUT"
