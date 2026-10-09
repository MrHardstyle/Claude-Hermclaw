"""Idle sleep hooks (P10 10.9).

A worker host is put to sleep when *all* of the following hold:

* the host has ``idle_sleep_command`` configured and (by default) Wake-on-LAN enabled – a host that
  cannot be woken again is never put to sleep;
* its registry state is ``ready`` (not ``busy``/``draining``/``starting``/…) and its last heartbeat
  reports no active job/step;
* no active step (``leased|running|testing|verifying|reviewing``) is assigned to it, no step attempt runs
  on it, no lease is held on one of its resources and no resource request waits for one of them;
* the last activity (state change, step/attempt/lease activity, wake history) is at least
  ``idle_after_minutes`` old.

Execution: under the worker row lock the state becomes ``sleeping`` (the scheduler never selects a sleeping
worker), the active-work check is repeated, then the command runs through the injected
:class:`RemoteCommandRunner` (the SSH admin tool of P26 in production). Exit code 0 keeps ``sleeping``; a
definite failure restores ``ready``; an ambiguous result (timeout, transport error, ssh exit 255 – the
connection often drops when the host suspends) keeps ``sleeping`` – a host that is still awake corrects
this with its next heartbeat. Every decision that acts is recorded as ``wake_events`` row (stage
``sleep``) and ``status`` event; state changes emit ``worker.state``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity, StepStatus, WorkerState
from hermclaw.contracts.events import EventType
from hermclaw.core.config import HermclawConfig, HostConfig, HostsConfig
from hermclaw.core.errors import ConfigError
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.persistence.models import ResourceLease, ResourceRequest, Step, StepAttempt, WakeEvent, Worker
from hermclaw.wol.recording import clip, force_worker_state, record_wake_event
from hermclaw.workers.registry import WorkerRegistry

log = get_logger(__name__)

SOURCE_TYPE = "idle_sleep"
SLEEP_STAGE = "sleep"
ACTIVE_STEP_STATUSES = frozenset(
    {StepStatus.leased.value, StepStatus.running.value, StepStatus.testing.value, StepStatus.verifying.value, StepStatus.reviewing.value}
)
HOLDING_LEASE_STATES = ("active", "preempting")
LEASE_RESOURCES_LABEL = "lease_resources"
SSH_TRANSPORT_EXIT = 255


class RemoteCommandRunner(Protocol):
    """Runs one command on a configured host (production: the SSH admin tool, Bauplan §26/P26).

    Returns ``(exit_code, stdout, stderr)``; raises on transport errors.
    """

    async def run(self, host_id: str, command: str, timeout: float) -> tuple[int, str, str]: ...  # noqa: ASYNC109 – fixed interface


class IdleReason(StrEnum):
    idle_timeout = "idle_timeout"  # → sleep
    not_configured = "not_configured"
    wol_disabled = "wol_disabled"
    invalid_command = "invalid_command"
    not_registered = "not_registered"
    state_not_ready = "state_not_ready"
    worker_reports_active_work = "worker_reports_active_work"
    active_steps = "active_steps"
    active_attempts = "active_attempts"
    active_leases = "active_leases"
    pending_requests = "pending_requests"
    recently_active = "recently_active"


class SleepResult(StrEnum):
    skipped = "skipped"  # decision said no
    aborted = "aborted"  # work appeared between the decision and the command
    slept = "slept"  # command exit 0
    failed = "failed"  # definite failure, state restored to ready
    unknown = "unknown"  # timeout / transport error / ssh 255 – kept sleeping, heartbeat corrects


@dataclass(frozen=True)
class IdleSleepSettings:
    idle_after_minutes: float = 30.0
    command_timeout_seconds: float = 60.0
    require_wake_on_lan: bool = True
    check_interval_seconds: float = 60.0


@dataclass(frozen=True)
class IdleDecision:
    worker_id: str
    should_sleep: bool
    reason: IdleReason
    state: str | None = None
    idle_seconds: float | None = None
    last_activity_at: datetime | None = None
    active_steps: int = 0
    active_attempts: int = 0
    active_leases: int = 0
    pending_requests: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "should_sleep": self.should_sleep,
            "reason": self.reason.value,
            "state": self.state,
            "idle_seconds": None if self.idle_seconds is None else round(self.idle_seconds, 1),
            "last_activity_at": self.last_activity_at.isoformat() if self.last_activity_at else None,
            "active_steps": self.active_steps,
            "active_attempts": self.active_attempts,
            "active_leases": self.active_leases,
            "pending_requests": self.pending_requests,
        }


@dataclass(frozen=True)
class IdleSleepOutcome:
    decision: IdleDecision
    result: SleepResult
    exit_code: int | None = None
    error: str | None = None

    @property
    def executed(self) -> bool:
        return self.result in (SleepResult.slept, SleepResult.failed, SleepResult.unknown)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def valid_sleep_command(command: str | None) -> bool:
    """One non-empty command line (no NUL/CR/LF – the runner receives exactly what the operator configured)."""
    return bool(command and command.strip()) and not any(ch in (command or "") for ch in ("\x00", "\n", "\r"))


def worker_resource_names(config: HermclawConfig | HostsConfig, worker_id: str) -> frozenset[str]:
    """Lease resources that belong to a worker host: resource groups of model profiles hosted on it
    (``models.yaml`` ``profiles[].host``), the host label ``lease_resources`` (comma separated, e.g.
    ``code-executor-222,gpu-224``) and the worker id itself."""
    names: set[str] = {worker_id}
    hosts = config.hosts if isinstance(config, HermclawConfig) else config
    if isinstance(config, HermclawConfig):
        names.update(p.resource_group for p in config.models.profiles if p.host == worker_id and p.resource_group)
    with contextlib.suppress(ConfigError):
        label = hosts.by_id(worker_id).labels.get(LEASE_RESOURCES_LABEL, "")
        names.update(n.strip() for n in label.split(",") if n.strip())
    return frozenset(names)


class IdleSleepPolicy:
    """Decides and executes idle sleep for worker hosts. Safe to run in several processes (row lock)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig | HostsConfig,
        runner: RemoteCommandRunner,
        *,
        settings: IdleSleepSettings | None = None,
        registry: WorkerRegistry | None = None,
        resources: Mapping[str, Collection[str]] | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sm = sessionmaker
        self._config = config
        self._hosts = config.hosts if isinstance(config, HermclawConfig) else config
        self._runner = runner
        self.settings = settings or IdleSleepSettings()
        self._registry = registry or WorkerRegistry(clock=now)
        self._resources = {k: frozenset(v) for k, v in (resources or {}).items()}
        self._now = now

    def candidates(self) -> list[HostConfig]:
        """Hosts with an idle sleep command."""
        return [h for h in self._hosts.hosts if h.idle_sleep_command]

    def _resource_names(self, worker_id: str) -> frozenset[str]:
        return self._resources.get(worker_id) or worker_resource_names(self._config, worker_id)

    # ------------------------------------------------------------------------------------------ decision
    async def evaluate(self, worker_id: str, *, session: AsyncSession | None = None) -> IdleDecision:
        """Pure decision (no writes)."""
        if session is not None:
            return await self._evaluate(session, worker_id, lock=False)
        async with self._sm() as s:
            return await self._evaluate(s, worker_id, lock=False)

    async def _active_counts(self, s: AsyncSession, worker_id: str, now: datetime) -> tuple[int, int, int, int]:
        names = sorted(self._resource_names(worker_id))
        steps = await s.scalar(
            select(func.count())
            .select_from(Step)
            .where(Step.assigned_worker_id == worker_id, Step.status.in_(ACTIVE_STEP_STATUSES), Step.superseded.is_(False))
        )
        attempts = await s.scalar(
            select(func.count())
            .select_from(StepAttempt)
            .where(StepAttempt.worker_id == worker_id, StepAttempt.status == "running", StepAttempt.finished_at.is_(None))
        )
        leases = await s.scalar(
            select(func.count())
            .select_from(ResourceLease)
            .where(ResourceLease.resource.in_(names), ResourceLease.state.in_(HOLDING_LEASE_STATES))
        )
        requests = await s.scalar(
            select(func.count())
            .select_from(ResourceRequest)
            .where(ResourceRequest.resource.in_(names), ResourceRequest.state == "waiting", ResourceRequest.expires_at > now)
        )
        return int(steps or 0), int(attempts or 0), int(leases or 0), int(requests or 0)

    async def _last_activity(self, s: AsyncSession, row: Worker) -> datetime:
        names = sorted(self._resource_names(row.id))
        candidates: list[datetime] = [_aware(row.created_at)]
        changed = _parse_dt((row.metadata_ or {}).get("state_changed_at"))
        if changed:
            candidates.append(changed)
        queries = (
            select(func.max(Step.updated_at)).where(Step.assigned_worker_id == row.id),
            select(func.max(func.coalesce(StepAttempt.finished_at, StepAttempt.started_at))).where(StepAttempt.worker_id == row.id),
            select(func.max(func.coalesce(ResourceLease.released_at, ResourceLease.heartbeat_at))).where(ResourceLease.resource.in_(names)),
            select(func.max(WakeEvent.created_at)).where(WakeEvent.worker_id == row.id),
        )
        for q in queries:
            value = await s.scalar(q)
            if isinstance(value, datetime):
                candidates.append(_aware(value))
        return max(candidates)

    async def _evaluate(self, s: AsyncSession, worker_id: str, *, lock: bool) -> IdleDecision:
        try:
            host = self._hosts.by_id(worker_id)
        except ConfigError:
            return IdleDecision(worker_id, False, IdleReason.not_configured)
        if not host.idle_sleep_command:
            return IdleDecision(worker_id, False, IdleReason.not_configured)
        if not valid_sleep_command(host.idle_sleep_command):
            return IdleDecision(worker_id, False, IdleReason.invalid_command)
        if self.settings.require_wake_on_lan and not (host.wake_on_lan.enabled and host.wake_on_lan.mac):
            return IdleDecision(worker_id, False, IdleReason.wol_disabled)
        stmt = select(Worker).where(Worker.id == worker_id).execution_options(populate_existing=True)
        if lock:
            stmt = stmt.with_for_update()
        row = (await s.execute(stmt)).scalar_one_or_none()
        if row is None:
            return IdleDecision(worker_id, False, IdleReason.not_registered)
        state = row.state
        if state != WorkerState.ready.value:
            return IdleDecision(worker_id, False, IdleReason.state_not_ready, state=state)
        if row.active_job_id or row.active_step_id:
            return IdleDecision(worker_id, False, IdleReason.worker_reports_active_work, state=state)
        now = _aware(self._now())
        steps, attempts, leases, requests = await self._active_counts(s, worker_id, now)
        for reason, n in (
            (IdleReason.active_steps, steps),
            (IdleReason.active_attempts, attempts),
            (IdleReason.active_leases, leases),
            (IdleReason.pending_requests, requests),
        ):
            if n:
                return IdleDecision(
                    worker_id,
                    False,
                    reason,
                    state=state,
                    active_steps=steps,
                    active_attempts=attempts,
                    active_leases=leases,
                    pending_requests=requests,
                )
        last = await self._last_activity(s, row)
        idle = (now - last).total_seconds()
        if idle < self.settings.idle_after_minutes * 60.0:
            return IdleDecision(worker_id, False, IdleReason.recently_active, state=state, idle_seconds=idle, last_activity_at=last)
        return IdleDecision(worker_id, True, IdleReason.idle_timeout, state=state, idle_seconds=idle, last_activity_at=last)

    # ------------------------------------------------------------------------------------------ execution
    async def _record(
        self,
        s: AsyncSession,
        decision: IdleDecision,
        status: str,
        text: str,
        *,
        severity: Severity = Severity.info,
        extra: dict[str, Any] | None = None,
    ) -> None:
        details = {**decision.to_dict(), "result": status, **(extra or {})}
        await record_wake_event(s, worker_id=decision.worker_id, job_id=None, stage=SLEEP_STAGE, status=status, details=details)
        await append_event(
            s,
            EventType.STATUS,
            source_type=SOURCE_TYPE,
            source_id=decision.worker_id,
            severity=severity,
            payload={"text": text[:500], "action": "idle_sleep", **details},
        )

    async def maybe_sleep(self, worker_id: str) -> IdleSleepOutcome:
        """Evaluate and, if idle, put the worker to sleep. Never sleeps a worker with active work."""
        host_cmd = ""
        async with self._sm() as s:
            decision = await self._evaluate(s, worker_id, lock=True)
            if not decision.should_sleep:
                await s.rollback()
                return IdleSleepOutcome(decision, SleepResult.skipped)
            host_cmd = self._hosts.by_id(worker_id).idle_sleep_command or ""
            await self._registry.set_state(s, worker_id, WorkerState.sleeping, reason="idle_sleep")
            idle_min = round((decision.idle_seconds or 0.0) / 60.0, 1)
            await self._record(s, decision, "requested", f"Worker {worker_id}: idle for {idle_min} min, going to sleep")
            await s.commit()

        # dispatch may have raced the decision: re-check after the sleeping state is visible to the scheduler
        async with self._sm() as s:
            steps, attempts, leases, requests = await self._active_counts(s, worker_id, _aware(self._now()))
            if steps or attempts or leases or requests:
                await force_worker_state(
                    s, worker_id, WorkerState.ready, reason="idle_sleep_aborted", only_from={WorkerState.sleeping}, source_type=SOURCE_TYPE
                )
                counts = {"active_steps": steps, "active_attempts": attempts, "active_leases": leases, "pending_requests": requests}
                await self._record(s, decision, SleepResult.aborted, f"Worker {worker_id}: sleep aborted, new work arrived", extra=counts)
                await s.commit()
                return IdleSleepOutcome(decision, SleepResult.aborted)
            await s.commit()

        timeout = self.settings.command_timeout_seconds
        exit_code: int | None = None
        stderr = ""
        error: str | None = None
        try:
            exit_code, _stdout, stderr = await asyncio.wait_for(
                self._runner.run(worker_id, host_cmd, timeout), timeout + max(1.0, 0.1 * timeout)
            )
        except TimeoutError:
            error = f"sleep command timed out after {timeout} s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"sleep command transport error: {type(exc).__name__}: {exc}"

        if error is None and exit_code == 0:
            result = SleepResult.slept
        elif error is None and exit_code != SSH_TRANSPORT_EXIT:
            result = SleepResult.failed
            error = f"sleep command exited with {exit_code}"
        else:
            result = SleepResult.unknown
            error = error or f"sleep command exited with {exit_code} (connection closed)"
        extra: dict[str, Any] = {"exit_code": exit_code}
        if stderr:
            extra["stderr"] = clip(DEFAULT_REDACTOR.text(stderr), 500)
        if error:
            extra["error"] = clip(DEFAULT_REDACTOR.text(error), 500)
        async with self._sm() as s:
            if result == SleepResult.failed:
                await force_worker_state(
                    s, worker_id, WorkerState.ready, reason="idle_sleep_failed", only_from={WorkerState.sleeping}, source_type=SOURCE_TYPE
                )
            text = {
                SleepResult.slept: f"Worker {worker_id}: sleeping (idle)",
                SleepResult.failed: f"Worker {worker_id}: sleep command failed, worker stays ready",
                SleepResult.unknown: f"Worker {worker_id}: sleep command result unknown",
            }[result]
            severity = Severity.info if result == SleepResult.slept else Severity.warning
            await self._record(s, decision, result, text, severity=severity, extra=extra)
            await s.commit()
        log.info("idle sleep", extra={"worker_id": worker_id, "result": result.value, "exit_code": exit_code})
        return IdleSleepOutcome(decision, result, exit_code=exit_code, error=extra.get("error"))

    async def run_once(self) -> list[IdleSleepOutcome]:
        """One pass over every host with an idle sleep command (sequential – one SSH session at a time)."""
        out: list[IdleSleepOutcome] = []
        for host in self.candidates():
            try:
                out.append(await self.maybe_sleep(host.id))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("idle sleep check failed", extra={"worker_id": host.id})
        return out

    async def run_forever(self, stop: asyncio.Event, *, interval_seconds: float | None = None) -> None:
        interval = interval_seconds if interval_seconds is not None else self.settings.check_interval_seconds
        while not stop.is_set():
            await self.run_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
