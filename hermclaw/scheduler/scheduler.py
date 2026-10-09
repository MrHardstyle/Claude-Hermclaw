"""DAG scheduler (Bauplan §25 SCHEDULER, Phase 25).

PostgreSQL is the queue (``FOR UPDATE SKIP LOCKED``). One tick:

1. recover expired step leases (crashed owners) → recovery mapping,
2. apply cancel/pause requests,
3. start preparation of queued jobs (inventory/triage/research/planning via ``JobDriver``),
4. promote pending steps whose dependencies completed; block steps whose dependencies failed,
5. dispatch ready steps (capability handler, per-job mutation serialisation, concurrency limit),
6. evaluate jobs: completion → finalize (commit/push), blocked/failed steps → replan or fail.

Handlers run as asyncio tasks with a step-lease heartbeat; crashes leave leases that expire and are
recovered by any scheduler instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, not_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import MUTATING_STEP_KINDS, JobStatus, Severity, StepStatus
from hermclaw.contracts.events import EventType
from hermclaw.core.config import HermclawConfig
from hermclaw.core.errors import InvalidTransition
from hermclaw.core.logging import get_logger
from hermclaw.events.store import append_event
from hermclaw.persistence.models import Job, Step, StepAttempt, StepDependency
from hermclaw.runtime.state_machines import (
    JOB_TERMINAL,
    STEP_IN_FLIGHT,
    can_transition_job,
    can_transition_step,
    recovery_step_state,
)
from hermclaw.runtime.transitions import emit_status, transition_job, transition_step
from hermclaw.scheduler.handlers import CancelToken, JobDriver, StepHandler, StepOutcome, StepRunContext

log = get_logger(__name__)

ACTIVE_JOB_STATES = (
    JobStatus.running,
    JobStatus.testing,
    JobStatus.verifying,
    JobStatus.reviewing,
    JobStatus.correcting,
    JobStatus.waiting_for_resources,
    JobStatus.waiting_for_worker,
    JobStatus.waking_worker,
)
PREPARE_STATES = (JobStatus.inventory, JobStatus.discovering, JobStatus.researching, JobStatus.planning)
PHASE_STATES = (JobStatus.committing, JobStatus.deploying, JobStatus.replanning)
_MUTATING = {k.value for k in MUTATING_STEP_KINDS}


def _now() -> datetime:
    return datetime.now(UTC)


async def step_to(s: AsyncSession, st: Step, target: StepStatus, **kw: Any) -> None:
    """Transition a step, passing through ``running`` when a sub-state (testing/verifying/…) cannot reach ``target`` directly."""
    if (
        st.status != target.value
        and not can_transition_step(st.status, target)
        and can_transition_step(st.status, StepStatus.running)
        and can_transition_step(StepStatus.running, target)
    ):
        await transition_step(s, st, StepStatus.running, reason="normalise sub-state", actor=kw.get("actor", "runtime"))
    await transition_step(s, st, target, **kw)


async def job_to(s: AsyncSession, job: Job, target: JobStatus, **kw: Any) -> None:
    """Transition a job, passing through ``running`` when the current macro state cannot reach ``target`` directly."""
    if (
        job.status != target.value
        and not can_transition_job(job.status, target)
        and can_transition_job(job.status, JobStatus.running)
        and can_transition_job(JobStatus.running, target)
    ):
        await transition_job(s, job, JobStatus.running, reason="normalise macro state", actor=kw.get("actor", "runtime"))
    await transition_job(s, job, target, **kw)


@dataclass
class SchedulerSettings:
    concurrency: int = 4
    poll_seconds: float = 1.0
    step_lease_seconds: int = 120
    step_heartbeat_seconds: int = 30
    step_timeout_seconds: int = 4 * 3600
    retry_backoff_seconds: tuple[int, ...] = (30, 120, 600)
    job_lock_seconds: int = 3600
    cancel_grace_seconds: int = 60


@dataclass
class _Running:
    task: asyncio.Task[None]
    token: CancelToken
    job_id: uuid.UUID
    kind: str


@dataclass
class _JobTask:
    task: asyncio.Task[None]
    token: CancelToken
    phase: str


@dataclass
class TickReport:
    recovered: int = 0
    promoted: int = 0
    blocked: int = 0
    dispatched: int = 0
    jobs_started: int = 0
    jobs_finalizing: int = 0
    jobs_replanning: int = 0
    jobs_cancelled: int = 0
    phases_resumed: int = 0
    notes: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig,
        handlers: list[StepHandler],
        driver: JobDriver,
        *,
        settings: SchedulerSettings | None = None,
        instance_id: str | None = None,
        services: Any = None,
    ) -> None:
        self.sm = sessionmaker
        self.config = config
        self.driver = driver
        self.settings = settings or SchedulerSettings()
        self.instance_id = instance_id or f"scheduler-{os.uname().nodename}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.services = services
        self._handlers: dict[str, StepHandler] = {}
        for h in handlers:
            for k in h.kinds:
                self._handlers[k] = h
        self._running: dict[uuid.UUID, _Running] = {}
        self._job_tasks: dict[uuid.UUID, _JobTask] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    async def run_forever(self) -> None:
        await self.startup_recovery()
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                log.exception("scheduler tick failed", extra={"error": str(exc)})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.poll_seconds)
        await self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    async def shutdown(self, *, checkpoint: bool = True) -> None:
        for r in self._running.values():
            if checkpoint:
                r.token.request_checkpoint("scheduler shutdown")
            else:
                r.token.cancel("scheduler shutdown")
        tasks = [r.task for r in self._running.values()] + [j.task for j in self._job_tasks.values()]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=self.settings.cancel_grace_seconds)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def wait_idle(self) -> None:
        """Wait until no handler/job task is running (wrap in ``asyncio.timeout`` to bound it)."""
        while self._running or self._job_tasks:
            tasks = [r.task for r in self._running.values()] + [j.task for j in self._job_tasks.values()]
            await asyncio.wait(tasks)
            self._reap_finished()

    # ------------------------------------------------------------------ recovery
    async def startup_recovery(self) -> int:
        """P34: steps/jobs left in flight by a crashed process are re-queued according to the recovery mapping."""
        n = 0
        async with self.sm() as s:
            steps = (
                (
                    await s.execute(
                        select(Step)
                        .where(Step.status.in_([st.value for st in STEP_IN_FLIGHT]), Step.superseded.is_(False))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for st in steps:
                if st.lease_owner and st.lease_owner != self.instance_id and st.lease_expires_at and st.lease_expires_at > _now():
                    continue  # another live scheduler owns it
                n += await self._recover_step(s, st, reason="startup recovery")
            jobs = (
                (await s.execute(select(Job).where(Job.status.in_([j.value for j in PREPARE_STATES])).with_for_update(skip_locked=True)))
                .scalars()
                .all()
            )
            for job in jobs:
                if job.lock_owner and job.lock_owner != self.instance_id and job.lock_expires_at and job.lock_expires_at > _now():
                    continue
                job.lock_owner = None
                job.lock_expires_at = None
                job.metadata_ = {**(job.metadata_ or {}), "recovered_prepare_from": job.status}
                await append_event(
                    s, "job.recovered", source_type="scheduler", source_id=self.instance_id, job_id=job.id, payload={"status": job.status}
                )
            await s.commit()
        return n

    async def _recover_step(self, s: AsyncSession, st: Step, *, reason: str) -> int:
        target = recovery_step_state(st.status, has_checkpoint=bool(st.checkpoint))
        attempt = (
            await s.execute(select(StepAttempt).where(StepAttempt.step_id == st.id).order_by(StepAttempt.attempt_no.desc()).limit(1))
        ).scalar_one_or_none()
        if attempt and attempt.finished_at is None:
            attempt.status = "lost"
            attempt.outcome = "lost"
            attempt.finished_at = _now()
            attempt.error_code = "WORKER_LOST"
        # leased cannot go to checkpointed (nothing ran yet); sub-states pass through running
        if st.status == StepStatus.leased.value:
            target = StepStatus.ready
        await step_to(s, st, target, reason=reason, actor=self.instance_id, extra={"recovered": True})
        st.lease_owner = None
        st.lease_expires_at = None
        return 1

    async def recover_expired_leases(self) -> int:
        n = 0
        async with self.sm() as s:
            steps = (
                (
                    await s.execute(
                        select(Step)
                        .where(
                            Step.status.in_([st.value for st in STEP_IN_FLIGHT]),
                            Step.lease_expires_at.is_not(None),
                            Step.lease_expires_at < _now(),
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for st in steps:
                if st.id in self._running:  # our own task – heartbeat is late but alive
                    continue
                n += await self._recover_step(s, st, reason="step lease expired")
            await s.commit()
        return n

    # ------------------------------------------------------------------ tick
    async def tick(self) -> TickReport:
        rep = TickReport()
        self._reap_finished()
        rep.recovered = await self.recover_expired_leases()
        rep.jobs_cancelled = await self._apply_controls()
        rep.phases_resumed = await self._resume_job_phases()
        rep.jobs_started = await self._start_queued_jobs()
        rep.promoted, rep.blocked = await self._promote_steps()
        rep.dispatched = await self._dispatch()
        fin, rep_n = await self._evaluate_jobs()
        rep.jobs_finalizing, rep.jobs_replanning = fin, rep_n
        return rep

    def _reap_finished(self) -> None:
        for sid in [k for k, r in self._running.items() if r.task.done()]:
            self._running.pop(sid, None)
        for jid in [k for k, j in self._job_tasks.items() if j.task.done()]:
            self._job_tasks.pop(jid, None)

    # ------------------------------------------------------------------ controls
    async def _apply_controls(self) -> int:
        cancelled = 0
        async with self.sm() as s:
            jobs = (
                (
                    await s.execute(
                        select(Job).where(
                            Job.status.not_in([j.value for j in JOB_TERMINAL]),
                            (Job.cancel_requested.is_(True)) | (Job.pause_requested.is_(True)),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for job in jobs:
                for r in self._running.values():
                    if r.job_id != job.id:
                        continue
                    if job.cancel_requested:
                        r.token.cancel("job cancelled")
                    elif job.pause_requested:
                        r.token.request_checkpoint("job paused")
                jt = self._job_tasks.get(job.id)
                if jt and job.cancel_requested:
                    jt.token.cancel("job cancelled")
                if job.cancel_requested and not any(r.job_id == job.id for r in self._running.values()) and job.id not in self._job_tasks:
                    locked = (await s.execute(select(Job).where(Job.id == job.id).with_for_update(skip_locked=True))).scalar_one_or_none()
                    if locked is None or locked.status in {j.value for j in JOB_TERMINAL}:
                        continue
                    for st in (await s.execute(select(Step).where(Step.job_id == job.id, Step.superseded.is_(False)))).scalars():
                        if st.status not in (StepStatus.completed.value, StepStatus.cancelled.value, StepStatus.failed.value):
                            await step_to(s, st, StepStatus.cancelled, reason="job cancelled", actor=self.instance_id)
                    await transition_job(
                        s, locked, JobStatus.cancelled, reason="cancel requested", actor=self.instance_id, status_line="Job abgebrochen"
                    )
                    cancelled += 1
            await s.commit()
        return cancelled

    # ------------------------------------------------------------------ job preparation
    async def _lock_job(self, s: AsyncSession, job: Job) -> bool:
        if job.lock_owner and job.lock_owner != self.instance_id and job.lock_expires_at and job.lock_expires_at > _now():
            return False
        job.lock_owner = self.instance_id
        job.lock_expires_at = _now() + timedelta(seconds=self.settings.job_lock_seconds)
        return True

    async def _start_queued_jobs(self) -> int:
        started = 0
        free = max(0, self.settings.concurrency - len(self._job_tasks))
        if free == 0:
            return 0
        async with self.sm() as s:
            jobs = (
                (
                    await s.execute(
                        select(Job)
                        .where(
                            Job.status.in_([JobStatus.queued.value, *[p.value for p in PREPARE_STATES]]),
                            Job.cancel_requested.is_(False),
                            Job.pause_requested.is_(False),
                        )
                        .order_by(Job.priority.desc(), Job.created_at)
                        .limit(free)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            to_start: list[uuid.UUID] = []
            for job in jobs:
                if job.id in self._job_tasks or not await self._lock_job(s, job):
                    continue
                to_start.append(job.id)
            await s.commit()
        for jid in to_start:
            token = CancelToken()
            self._job_tasks[jid] = _JobTask(asyncio.create_task(self._run_prepare(jid, token), name=f"prepare-{jid}"), token, "prepare")
            started += 1
        return started

    async def _run_prepare(self, job_id: uuid.UUID, token: CancelToken) -> None:
        res_ok: bool
        code: str | None
        msg: str | None
        nxt: str | None
        try:
            res = await self.driver.prepare(job_id, token)
        except Exception as exc:
            log.exception("job preparation crashed", extra={"job_id": str(job_id)})
            res_ok, code, msg, nxt = False, "PREPARE_CRASHED", f"{type(exc).__name__}: {exc}", None
        else:
            res_ok, code, msg, nxt = res.ok, res.error_code, res.error_message, res.next_status
        async with self.sm() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
            job.lock_owner = None
            job.lock_expires_at = None
            if job.status in {j.value for j in JOB_TERMINAL}:
                await s.commit()
                return
            if token.cancelled:
                pass  # _apply_controls finalises cancellation
            elif res_ok:
                target = JobStatus(nxt) if nxt else JobStatus.running
                try:
                    await transition_job(s, job, target, reason="preparation finished", actor=self.instance_id)
                except InvalidTransition as exc:
                    # driver contract violation: never loop forever re-preparing the job
                    await transition_job(
                        s,
                        job,
                        JobStatus.failed,
                        reason="preparation ended in invalid state",
                        actor=self.instance_id,
                        error_code="PREPARE_INVALID_STATE",
                        error_message=str(exc),
                    )
            else:
                await transition_job(
                    s,
                    job,
                    JobStatus.failed,
                    reason="preparation failed",
                    actor=self.instance_id,
                    error_code=code,
                    error_message=msg,
                    status_line=f"Vorbereitung fehlgeschlagen: {code}",
                )
            await s.commit()

    # ------------------------------------------------------------------ DAG promotion
    async def _promote_steps(self) -> tuple[int, int]:
        promoted = blocked = 0
        async with self.sm() as s:
            pending = (
                (
                    await s.execute(
                        select(Step)
                        .join(Job, Job.id == Step.job_id)
                        .where(
                            Step.status == StepStatus.pending.value,
                            Step.superseded.is_(False),
                            Job.status.in_([j.value for j in ACTIVE_JOB_STATES]),
                        )
                        .with_for_update(of=Step, skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for st in pending:
                deps = (
                    await s.execute(
                        select(Step.status, Step.step_key, Step.not_before, Step.attempt_count, Step.max_attempts)
                        .join(StepDependency, StepDependency.depends_on_step_id == Step.id)
                        .where(StepDependency.step_id == st.id)
                    )
                ).all()
                # a failed dependency only blocks when no retry is scheduled for it
                bad = [
                    d.step_key
                    for d in deps
                    if d.status in (StepStatus.cancelled.value, StepStatus.blocked.value)
                    or (d.status == StepStatus.failed.value and not (d.not_before is not None and d.attempt_count < d.max_attempts))
                ]
                if all(d.status == StepStatus.completed.value for d in deps):
                    await transition_step(s, st, StepStatus.ready, reason="dependencies completed", actor=self.instance_id)
                    promoted += 1
                elif bad:
                    await step_to(
                        s,
                        st,
                        StepStatus.blocked,
                        reason="dependency failed",
                        actor=self.instance_id,
                        error_code="DEPENDENCY_FAILED",
                        error_message=f"dependencies not satisfiable: {', '.join(bad)}",
                    )
                    blocked += 1
            await s.commit()
        return promoted, blocked

    # ------------------------------------------------------------------ dispatch
    async def _dispatch(self) -> int:
        free = self.settings.concurrency - len(self._running)
        if free <= 0:
            return 0
        dispatched: list[tuple[uuid.UUID, StepRunContext, StepHandler]] = []
        async with self.sm() as s:
            # failed steps scheduled for retry whose backoff elapsed
            retry = (
                (
                    await s.execute(
                        select(Step)
                        .where(
                            Step.status == StepStatus.failed.value,
                            Step.not_before.is_not(None),
                            Step.not_before <= _now(),
                            Step.superseded.is_(False),
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for st in retry:
                await transition_step(s, st, StepStatus.ready, reason="retry after backoff", actor=self.instance_id)
                st.not_before = None
            checkpointed = (
                (
                    await s.execute(
                        select(Step)
                        .join(Job, Job.id == Step.job_id)
                        .where(
                            Step.status == StepStatus.checkpointed.value,
                            Job.pause_requested.is_(False),
                            Job.cancel_requested.is_(False),
                            Step.superseded.is_(False),
                        )
                        .with_for_update(of=Step, skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for st in checkpointed:
                await transition_step(s, st, StepStatus.ready, reason="resume from checkpoint", actor=self.instance_id)
            await s.flush()
            rows = (
                await s.execute(
                    select(Step, Job.priority)
                    .join(Job, Job.id == Step.job_id)
                    .where(
                        Step.status == StepStatus.ready.value,
                        Step.superseded.is_(False),
                        (Step.not_before.is_(None)) | (Step.not_before <= _now()),
                        Job.status.in_([j.value for j in ACTIVE_JOB_STATES]),
                        Job.pause_requested.is_(False),
                        Job.cancel_requested.is_(False),
                        not_(Job.metadata_.has_key("replan_requested")),  # manual replan pending: drain, do not start more
                    )
                    .order_by(Job.priority.desc(), Step.priority.desc(), Step.created_at, Step.step_key)
                    .limit(free * 4)
                    .with_for_update(of=Step, skip_locked=True)
                )
            ).all()
            busy_mutating_jobs = await self._jobs_with_active_mutation(s)
            for st, _prio in rows:
                if len(dispatched) >= free:
                    break
                handler = self._handlers.get(st.kind)
                if handler is None:
                    await step_to(
                        s,
                        st,
                        StepStatus.blocked,
                        reason="no handler for step kind",
                        error_code="NO_HANDLER",
                        error_message=f"no handler registered for kind '{st.kind}'",
                    )
                    continue
                if st.kind in _MUTATING:
                    if st.job_id in busy_mutating_jobs:
                        continue  # one mutating step per job workspace at a time
                    busy_mutating_jobs.add(st.job_id)
                attempt_no = st.attempt_count + 1
                last = (
                    await s.execute(
                        select(StepAttempt).where(StepAttempt.step_id == st.id).order_by(StepAttempt.attempt_no.desc()).limit(1)
                    )
                ).scalar_one_or_none()
                kind = "initial"
                if last is not None:
                    kind = "resume" if st.checkpoint else ("correction" if (last.correction_input or {}).get("pending") else "retry")
                attempt = StepAttempt(
                    step_id=st.id,
                    job_id=st.job_id,
                    attempt_no=attempt_no,
                    kind=kind,
                    status="running",
                    correction_input=(last.correction_input if last and kind == "correction" else {}),
                )
                s.add(attempt)
                st.attempt_count = attempt_no
                st.lease_owner = self.instance_id
                st.lease_expires_at = _now() + timedelta(seconds=self.settings.step_lease_seconds)
                await transition_step(
                    s,
                    st,
                    StepStatus.leased,
                    reason="dispatched",
                    actor=self.instance_id,
                    extra={"attempt_no": attempt_no, "attempt_kind": kind},
                )
                await s.flush()
                await append_event(
                    s,
                    EventType.ATTEMPT_STARTED,
                    source_type="scheduler",
                    source_id=self.instance_id,
                    job_id=st.job_id,
                    step_id=st.id,
                    attempt_id=attempt.id,
                    payload={"attempt_no": attempt_no, "kind": kind},
                )
                ctx = StepRunContext(
                    job_id=st.job_id,
                    step_id=st.id,
                    attempt_id=attempt.id,
                    attempt_no=attempt_no,
                    attempt_kind=kind,
                    step_key=st.step_key,
                    kind=st.kind,
                    capability=st.capability,
                    sessionmaker=self.sm,
                    config=self.config,
                    token=CancelToken(),
                    checkpoint=dict(st.checkpoint or {}),
                    correction_input=dict(attempt.correction_input or {}),
                    services=self.services,
                )
                dispatched.append((st.id, ctx, handler))
            await s.commit()
        for sid, ctx, handler in dispatched:
            task = asyncio.create_task(self._run_step(ctx, handler), name=f"step-{ctx.step_key}-{sid}")
            self._running[sid] = _Running(task, ctx.token, ctx.job_id, ctx.kind)
        return len(dispatched)

    async def _jobs_with_active_mutation(self, s: AsyncSession) -> set[uuid.UUID]:
        rows = (
            (
                await s.execute(
                    select(Step.job_id).where(
                        Step.kind.in_(_MUTATING), Step.status.in_([st.value for st in STEP_IN_FLIGHT]), Step.superseded.is_(False)
                    )
                )
            )
            .scalars()
            .all()
        )
        return set(rows) | {r.job_id for r in self._running.values() if r.kind in _MUTATING}

    async def _heartbeat(self, step_id: uuid.UUID, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.settings.step_heartbeat_seconds)
            if stop.is_set():
                return
            with contextlib.suppress(Exception):
                async with self.sm() as s:
                    await s.execute(
                        update(Step)
                        .where(Step.id == step_id, Step.lease_owner == self.instance_id)
                        .values(lease_expires_at=_now() + timedelta(seconds=self.settings.step_lease_seconds))
                    )
                    await s.commit()

    async def _run_step(self, ctx: StepRunContext, handler: StepHandler) -> None:
        async with self.sm() as s:
            st = (await s.execute(select(Step).where(Step.id == ctx.step_id).with_for_update())).scalar_one()
            await transition_step(s, st, StepStatus.running, reason="handler started", actor=self.instance_id)
            await emit_status(s, ctx.job_id, f"Step {ctx.step_key} gestartet ({ctx.kind})", step_id=ctx.step_id)
            await s.commit()
        stop_hb = asyncio.Event()
        hb = asyncio.create_task(self._heartbeat(ctx.step_id, stop_hb))
        try:
            outcome = await asyncio.wait_for(handler.run(ctx), timeout=self.settings.step_timeout_seconds)
        except TimeoutError:
            # wait_for already cancelled the handler coroutine; do NOT trip the job-cancel token (that would turn a
            # retryable timeout into a cancellation)
            outcome = StepOutcome(
                "failed", error_code="STEP_TIMEOUT", error_message=f"step exceeded {self.settings.step_timeout_seconds}s", retryable=True
            )
        except asyncio.CancelledError:
            outcome = StepOutcome("checkpointed", summary="scheduler stopped", checkpoint=ctx.checkpoint)
        except Exception as exc:
            log.exception("step handler crashed", extra={"step_id": str(ctx.step_id)})
            outcome = StepOutcome("failed", error_code="HANDLER_CRASHED", error_message=f"{type(exc).__name__}: {exc}", retryable=True)
        finally:
            stop_hb.set()
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
        if ctx.token.cancelled and outcome.outcome not in ("completed",):
            outcome = StepOutcome("cancelled", summary=ctx.token.reason or "cancelled")
        await self._apply_outcome(ctx, outcome)

    async def _apply_outcome(self, ctx: StepRunContext, out: StepOutcome) -> None:
        async with self.sm() as s:
            st = (await s.execute(select(Step).where(Step.id == ctx.step_id).with_for_update())).scalar_one()
            attempt = (await s.execute(select(StepAttempt).where(StepAttempt.id == ctx.attempt_id).with_for_update())).scalar_one()
            attempt.finished_at = _now()
            attempt.outcome = out.outcome
            attempt.status = out.outcome
            attempt.summary = out.summary[:4000] if out.summary else None
            attempt.error_code = out.error_code
            st.lease_owner = None
            st.lease_expires_at = None
            if out.result:
                st.result = {**(st.result or {}), **out.result}
            sev = Severity.info if out.outcome == "completed" else Severity.warning
            await append_event(
                s,
                EventType.ATTEMPT_FINISHED,
                source_type="scheduler",
                source_id=self.instance_id,
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                attempt_id=ctx.attempt_id,
                severity=sev,
                payload={"outcome": out.outcome, "error_code": out.error_code, "summary": out.summary[:500]},
            )
            if st.status in (StepStatus.leased.value,):
                await transition_step(s, st, StepStatus.running, reason="late start", actor=self.instance_id)
            if st.status in {x.value for x in (StepStatus.completed, StepStatus.cancelled)}:
                await s.commit()  # already terminal (e.g. cancelled by an operator) – keep the attempt record only
                return
            if out.outcome == "completed":
                st.checkpoint = {}
                st.error_code = None
                st.error_message = None
                await step_to(
                    s,
                    st,
                    StepStatus.completed,
                    reason=out.summary[:200] or "completed",
                    actor=self.instance_id,
                    status_line=f"Step {st.step_key} abgeschlossen",
                )
            elif out.outcome == "checkpointed":
                st.checkpoint = out.checkpoint or st.checkpoint or {"note": "checkpoint without state"}
                await step_to(s, st, StepStatus.checkpointed, reason=out.summary or "checkpoint", actor=self.instance_id)
            elif out.outcome == "cancelled":
                await step_to(s, st, StepStatus.cancelled, reason=out.summary or "cancelled", actor=self.instance_id)
            elif out.outcome == "failed":
                await step_to(
                    s,
                    st,
                    StepStatus.failed,
                    reason=out.error_message or "failed",
                    actor=self.instance_id,
                    error_code=out.error_code,
                    error_message=out.error_message,
                )
                if out.retryable and st.attempt_count < st.max_attempts:
                    backoff = self.settings.retry_backoff_seconds[min(st.attempt_count - 1, len(self.settings.retry_backoff_seconds) - 1)]
                    st.not_before = _now() + timedelta(seconds=backoff)
                    await emit_status(
                        s, ctx.job_id, f"Step {st.step_key} wird in {backoff}s erneut versucht", step_id=st.id, severity=Severity.warning
                    )
            elif out.outcome in ("blocked", "replan"):
                st.result = {
                    **(st.result or {}),
                    "replan_reason": out.replan_reason or out.error_code,
                    "replan_evidence": out.replan_evidence,
                }
                await step_to(
                    s,
                    st,
                    StepStatus.blocked,
                    reason=out.replan_reason or out.error_message or "blocked",
                    actor=self.instance_id,
                    error_code=out.error_code or "BLOCKED",
                    error_message=out.error_message,
                )
            await s.commit()

    # ------------------------------------------------------------------ job evaluation
    async def _evaluate_jobs(self) -> tuple[int, int]:
        finalizing = replanning = 0
        spawn: list[tuple[uuid.UUID, str, str]] = []
        async with self.sm() as s:
            jobs = (
                (
                    await s.execute(
                        select(Job)
                        .where(Job.status.in_([j.value for j in ACTIVE_JOB_STATES]), Job.cancel_requested.is_(False))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for job in jobs:
                if job.id in self._job_tasks:
                    continue
                counts = dict(
                    (
                        await s.execute(
                            select(Step.status, func.count()).where(Step.job_id == job.id, Step.superseded.is_(False)).group_by(Step.status)
                        )
                    ).all()
                )
                total = sum(counts.values())
                retry_pending = (
                    await s.execute(
                        select(func.count())
                        .select_from(Step)
                        .where(
                            and_(
                                Step.job_id == job.id,
                                Step.status == StepStatus.failed.value,
                                Step.not_before.is_not(None),
                                Step.superseded.is_(False),
                            )
                        )
                    )
                ).scalar_one()
                in_flight = (
                    sum(counts.get(st.value, 0) for st in (*STEP_IN_FLIGHT, StepStatus.ready, StepStatus.pending, StepStatus.checkpointed))
                    + retry_pending
                )
                manual_replan = (job.metadata_ or {}).get("replan_requested")
                running_now = sum(counts.get(st.value, 0) for st in STEP_IN_FLIGHT)
                if manual_replan and running_now:
                    continue  # manual replan waits for running steps; dispatch is already paused for this job
                if total and counts.get(StepStatus.completed.value, 0) == total and not manual_replan:
                    if not await self._lock_job(s, job):
                        continue
                    await job_to(
                        s,
                        job,
                        JobStatus.committing,
                        reason="all steps completed",
                        actor=self.instance_id,
                        status_line="Alle Steps abgeschlossen – Ergebnisse werden committet",
                    )
                    spawn.append((job.id, "finalize", ""))
                    finalizing += 1
                elif manual_replan or (
                    in_flight == 0 and (counts.get(StepStatus.blocked.value, 0) or counts.get(StepStatus.failed.value, 0))
                ):
                    reason = str(manual_replan) if manual_replan else "blocked_or_failed_steps"
                    max_replans = self.config.policies.correction.max_replans_per_job
                    if job.replan_count >= max_replans and not manual_replan:
                        failed_keys = [
                            k
                            for (k,) in (
                                await s.execute(
                                    select(Step.step_key).where(
                                        Step.job_id == job.id,
                                        Step.status.in_([StepStatus.failed.value, StepStatus.blocked.value]),
                                        Step.superseded.is_(False),
                                    )
                                )
                            ).all()
                        ]
                        await transition_job(
                            s,
                            job,
                            JobStatus.failed,
                            reason="replan budget exhausted",
                            actor=self.instance_id,
                            error_code="REPLAN_LIMIT",
                            error_message=f"steps not completed: {', '.join(failed_keys)}",
                            status_line="Job fehlgeschlagen – Replan-Budget erschöpft",
                        )
                        continue
                    if not await self._lock_job(s, job):
                        continue
                    job.metadata_ = {**{k: v for k, v in (job.metadata_ or {}).items() if k != "replan_requested"}, "replan_reason": reason}
                    await job_to(s, job, JobStatus.replanning, reason=reason, actor=self.instance_id, status_line="Gemma plant neu")
                    spawn.append((job.id, "replan", reason))
                    replanning += 1
                elif total == 0 and not in_flight:
                    await transition_job(
                        s, job, JobStatus.failed, reason="plan has no steps", actor=self.instance_id, error_code="EMPTY_PLAN"
                    )
            await s.commit()
        # spawn only after commit so the phase task never observes the pre-transition state
        for jid, phase, reason in spawn:
            self._spawn_job_task(jid, phase, reason=reason)
        return finalizing, replanning

    async def _resume_job_phases(self) -> int:
        """Jobs left in committing/deploying/replanning without a live owner (crash, restart) get their phase task respawned."""
        spawn: list[tuple[uuid.UUID, str, str]] = []
        async with self.sm() as s:
            jobs = (
                (
                    await s.execute(
                        select(Job)
                        .where(Job.status.in_([p.value for p in PHASE_STATES]), Job.cancel_requested.is_(False))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for job in jobs:
                if job.id in self._job_tasks or not await self._lock_job(s, job):
                    continue
                phase = "replan" if job.status == JobStatus.replanning.value else "finalize"
                reason = str((job.metadata_ or {}).get("replan_reason") or "resumed after restart")
                await append_event(
                    s,
                    "job.recovered",
                    source_type="scheduler",
                    source_id=self.instance_id,
                    job_id=job.id,
                    payload={"status": job.status, "phase": phase},
                )
                spawn.append((job.id, phase, reason))
            await s.commit()
        for jid, phase, reason in spawn:
            self._spawn_job_task(jid, phase, reason=reason)
        return len(spawn)

    def _spawn_job_task(self, job_id: uuid.UUID, phase: str, *, reason: str = "") -> None:
        token = CancelToken()
        coro = self._run_finalize(job_id, token) if phase == "finalize" else self._run_replan(job_id, reason, token)
        self._job_tasks[job_id] = _JobTask(asyncio.create_task(coro, name=f"{phase}-{job_id}"), token, phase)

    async def _run_finalize(self, job_id: uuid.UUID, token: CancelToken) -> None:
        try:
            res = await self.driver.finalize(job_id, token)
            ok, code, msg = res.ok, res.error_code, res.error_message
        except Exception as exc:
            log.exception("finalize crashed", extra={"job_id": str(job_id)})
            ok, code, msg = False, "FINALIZE_CRASHED", f"{type(exc).__name__}: {exc}"
        async with self.sm() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
            job.lock_owner = None
            job.lock_expires_at = None
            if job.status not in {j.value for j in JOB_TERMINAL}:
                if ok:
                    if job.status in (JobStatus.deploying.value, JobStatus.committing.value):
                        await transition_job(
                            s,
                            job,
                            JobStatus.succeeded,
                            reason="finalized",
                            actor=self.instance_id,
                            status_line="Job erfolgreich abgeschlossen",
                        )
                else:
                    await transition_job(
                        s,
                        job,
                        JobStatus.failed,
                        reason="finalize failed",
                        actor=self.instance_id,
                        error_code=code,
                        error_message=msg,
                        status_line=f"Abschluss fehlgeschlagen: {code}",
                    )
            await s.commit()

    async def _run_replan(self, job_id: uuid.UUID, reason: str, token: CancelToken) -> None:
        async with self.sm() as s:
            failed = (
                (
                    await s.execute(
                        select(Step).where(
                            Step.job_id == job_id,
                            Step.status.in_([StepStatus.failed.value, StepStatus.blocked.value]),
                            Step.superseded.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            evidence = {
                "failed_steps": [
                    {
                        "step_key": f.step_key,
                        "status": f.status,
                        "error_code": f.error_code,
                        "error_message": f.error_message,
                        "result": f.result,
                    }
                    for f in failed
                ]
            }
        try:
            res = await self.driver.replan(job_id, reason, evidence, token)
            ok, code, msg, nxt = res.ok, res.error_code, res.error_message, res.next_status
        except Exception as exc:
            log.exception("replan crashed", extra={"job_id": str(job_id)})
            ok, code, msg, nxt = False, "REPLAN_CRASHED", f"{type(exc).__name__}: {exc}", None
        async with self.sm() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
            job.lock_owner = None
            job.lock_expires_at = None
            if job.status == JobStatus.replanning.value:
                if ok:
                    await transition_job(
                        s, job, JobStatus(nxt) if nxt else JobStatus.running, reason="replan created", actor=self.instance_id
                    )
                else:
                    await transition_job(
                        s,
                        job,
                        JobStatus.failed,
                        reason="replan failed",
                        actor=self.instance_id,
                        error_code=code,
                        error_message=msg,
                        status_line=f"Replanning fehlgeschlagen: {code}",
                    )
            await s.commit()
