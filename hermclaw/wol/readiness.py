"""Worker readiness: ``ensure_worker_ready(worker_id)`` (Bauplan §2.7, §26; P10 10.1–10.8).

Stages (each recorded as a ``wake_events`` row and a ``worker.wake.stage`` event)::

    detect → wol_send → ping → ssh → worker_api → services → capabilities → READY

* ``detect``       – registry row state + a quick TCP probe of the worker API port. A reachable host skips
  ``wol_send`` and ``ping`` (recorded as ``skipped``).
* ``wol_send``     – magic packet to the host's broadcast address; failed sends are retried, all sends
  (including the re-sends during ``ping``) are bounded by ``wake_on_lan.max_attempts``. ``worker.wake.sent``
  per packet; the registry state becomes ``waking``.
* ``ping``         – ICMP (``ping`` binary) or TCP fallback to the SSH port, within ``ping_timeout_seconds``.
* ``ssh``          – SSH identification banner on the configured SSH port.
* ``worker_api``   – ``GET <worker_api>/health`` → 200 (and ``status`` ``ok``/``degraded`` if JSON).
* ``services``     – every further configured host service (e.g. Ollama ``/api/version``).
* ``capabilities`` – the capability registry is current: compatible worker in ``ready``/``busy``, a heartbeat
  newer than the wake when the host was down, and all required capabilities reported.

``ssh`` … ``capabilities`` share one budget of ``service_timeout_seconds`` that starts when the host is
reachable. An overall deadline caps everything. Failure codes: :class:`WakeFailureCode`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity, WorkerState
from hermclaw.contracts.events import EventType
from hermclaw.core.config import HermclawConfig, HostConfig, HostsConfig
from hermclaw.core.errors import ConfigError
from hermclaw.core.logging import get_logger
from hermclaw.events.store import append_event
from hermclaw.persistence.models import Worker
from hermclaw.wol.errors import WakeFailureCode, WolError
from hermclaw.wol.magic import DEFAULT_WOL_PORT, send_magic_packet
from hermclaw.wol.probes import InvalidTarget, ProbeOutcome, Probes, build_http_url, validate_address, validate_base_url
from hermclaw.wol.recording import SOURCE_TYPE, clip, force_worker_state, record_wake_event
from hermclaw.wol.status import WorkerUiStatus, ui_status_for
from hermclaw.workers.errors import WorkerNotFound
from hermclaw.workers.registry import DEFAULT_WORKER_PORT, WorkerRegistry

log = get_logger(__name__)

LIVE_STATES = frozenset({WorkerState.starting, WorkerState.ready, WorkerState.busy, WorkerState.draining})
DISPATCHABLE_STATES = frozenset({WorkerState.ready, WorkerState.busy})
# orchestrator-owned states a successful wake may promote to ``ready``
_PROMOTABLE = frozenset({WorkerState.offline, WorkerState.sleeping, WorkerState.waking, WorkerState.error})
_HEALTH_STATUS_OK = frozenset({"ok", "degraded"})


class WakeStage(StrEnum):
    detect = "detect"
    wol_send = "wol_send"
    ping = "ping"
    ssh = "ssh"
    worker_api = "worker_api"
    services = "services"
    capabilities = "capabilities"
    ready = "ready"


class StageStatus(StrEnum):
    ok = "ok"
    failed = "failed"
    skipped = "skipped"


STAGE_FAILURE: dict[WakeStage, WakeFailureCode] = {
    WakeStage.wol_send: WakeFailureCode.WOL_SEND_FAILED,
    WakeStage.ping: WakeFailureCode.PING_TIMEOUT,
    WakeStage.ssh: WakeFailureCode.SSH_TIMEOUT,
    WakeStage.worker_api: WakeFailureCode.WORKER_API_TIMEOUT,
    WakeStage.services: WakeFailureCode.MODEL_SERVICE_TIMEOUT,
    WakeStage.capabilities: WakeFailureCode.CAPABILITY_MISSING,
}


# ============================================================================================ results
class StageResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: WakeStage
    status: StageStatus
    duration_ms: int = 0
    attempts: int = 0
    error_code: WakeFailureCode | None = None
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ReadyResult(BaseModel):
    """Outcome of :meth:`WakeController.ensure_worker_ready`. Only ``ready=True`` permits dispatch."""

    model_config = ConfigDict(frozen=True)

    worker_id: str
    ready: bool
    status: WorkerUiStatus
    error_code: WakeFailureCode | None = None
    message: str = ""
    woke: bool = False
    packets_sent: int = 0
    worker_state: WorkerState | None = None
    duration_ms: int = 0
    stages: list[StageResult] = Field(default_factory=list)

    def stage(self, stage: WakeStage) -> StageResult | None:
        for s in self.stages:
            if s.stage == stage:
                return s
        return None

    def raise_for_failure(self) -> ReadyResult:
        """Return ``self`` when ready, else raise :class:`WolError` with the failure code."""
        if self.ready:
            return self
        code = self.error_code or WakeFailureCode.WORKER_API_TIMEOUT
        raise WolError(self.message or f"worker {self.worker_id} not ready", code=code, details={"worker_id": self.worker_id})


# ============================================================================================ registry access
@dataclass(frozen=True)
class RegistrySnapshot:
    state: WorkerState
    capabilities: frozenset[str]
    last_heartbeat_at: datetime | None = None
    compatible: bool = True


class RegistryLookup(Protocol):
    """Current registry view of one worker (``None`` = unknown)."""

    async def __call__(self, session: AsyncSession, worker_id: str) -> RegistrySnapshot | None: ...


def default_registry_lookup(registry: WorkerRegistry | None = None) -> RegistryLookup:
    """Default lookup backed by :class:`WorkerRegistry` (reported capabilities, state, heartbeat)."""
    reg = registry or WorkerRegistry()

    async def lookup(session: AsyncSession, worker_id: str) -> RegistrySnapshot | None:
        try:
            info = await reg.get_worker(session, worker_id)
        except WorkerNotFound:
            return None
        return RegistrySnapshot(
            state=info.state,
            capabilities=frozenset(info.capabilities),
            last_heartbeat_at=info.last_heartbeat_at,
            compatible=info.compatible,
        )

    return lookup


class MagicSender(Protocol):
    async def __call__(
        self, mac: str, broadcast: str, port: int, *, copies: int = 1, source_address: str | None = None, timeout_seconds: float = 2.0
    ) -> int: ...


# ============================================================================================ settings
@dataclass(frozen=True)
class WakeSettings:
    """Code-level tuning. Per-host timeouts and attempts come from ``hosts.yaml`` ``wake_on_lan``."""

    probe_interval_seconds: float = 2.0  # pause between two failed probes of a stage
    probe_timeout_seconds: float = 3.0  # one TCP/HTTP/SSH probe
    quick_probe_timeout_seconds: float = 1.5  # detect stage
    resend_interval_seconds: float | None = None  # default: ping_timeout / max_attempts
    send_retry_backoff_seconds: float = 1.0  # after a failed send
    packet_copies: int = 1  # datagrams per send attempt
    source_address: str | None = None  # bind the WOL socket to one local interface address
    heartbeat_skew_seconds: float = 5.0  # tolerance when comparing heartbeat time with the wake start
    overall_slack_seconds: float = 15.0  # added to ping + service timeout for the overall deadline
    accept_degraded_health: bool = True  # ``/health`` status ``degraded`` still means the API is up


# ============================================================================================ run context
@dataclass
class _Run:
    worker_id: str
    job_id: uuid.UUID | None
    host: HostConfig
    api_base: str
    api_host: str
    api_port: int
    ssh_port: int
    required: frozenset[str]
    started: float
    started_wall: datetime
    deadline: float
    stages: list[StageResult] = field(default_factory=list)
    packets_sent: int = 0
    send_attempts: int = 0
    woke: bool = False
    fresh_since: datetime | None = None
    initial_state: WorkerState | None = None
    services: list[tuple[str, str | None, int]] = field(default_factory=list)
    services_skipped: list[str] = field(default_factory=list)


class _StageFailed(Exception):
    def __init__(self, stage: WakeStage, detail: str, *, attempts: int, data: dict[str, Any], duration_ms: int) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail
        self.attempts = attempts
        self.data = data
        self.duration_ms = duration_ms


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _hosts(config: HermclawConfig | HostsConfig) -> HostsConfig:
    return config.hosts if isinstance(config, HermclawConfig) else config


def _short(value: Any, limit: int = 64) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list | tuple | set | frozenset):
        return [_short(v, limit) for v in list(value)[:20]]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)[:limit]


# ============================================================================================ controller
class WakeController:
    """Wakes workers and confirms readiness before dispatch. One instance per orchestrator process.

    Concurrent calls for the same worker (and capability set) share one in-flight run.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig | HostsConfig,
        registry_lookup: RegistryLookup | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        probes: Probes | None = None,
        registry: WorkerRegistry | None = None,
        settings: WakeSettings | None = None,
        sender: MagicSender | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sm = sessionmaker
        self._hosts = _hosts(config)
        self._registry = registry or WorkerRegistry(clock=now)
        self._lookup: RegistryLookup = registry_lookup or default_registry_lookup(self._registry)
        self._owns_probes = probes is None
        self._probes = probes or Probes(http_client)
        self.settings = settings or WakeSettings()
        self._send: MagicSender = sender or send_magic_packet
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._inflight: dict[tuple[str, frozenset[str]], asyncio.Task[ReadyResult]] = {}

    async def aclose(self) -> None:
        """Cancel in-flight runs and close owned HTTP resources."""
        tasks = list(self._inflight.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - already logged by the run
                log.debug("in-flight wake ended with error during close", exc_info=True)
        if self._owns_probes:
            await self._probes.aclose()

    # ------------------------------------------------------------------------------------------ public API
    async def ensure_worker_ready(
        self,
        worker_id: str,
        job_id: uuid.UUID | None = None,
        *,
        required_capabilities: Collection[str] | None = None,
        deadline_seconds: float | None = None,
    ) -> ReadyResult:
        """Wake ``worker_id`` if necessary and wait until it is dispatchable. Never raises for a wake
        failure (see :meth:`ReadyResult.raise_for_failure`); raises :class:`WorkerNotFound` for a worker
        that is not a configured host and :class:`ConfigError` for an unusable host configuration."""
        required = frozenset(c.strip() for c in (required_capabilities or ()) if c and c.strip())
        key = (worker_id, required)
        task = self._inflight.get(key)
        if task is None:
            ctx = self._context(worker_id, job_id, required, deadline_seconds)  # config errors raise here, synchronously
            task = asyncio.create_task(self._run(ctx), name=f"wake:{worker_id}")
            self._inflight[key] = task

            def _forget(done: asyncio.Task[ReadyResult], k: tuple[str, frozenset[str]] = key) -> None:
                if self._inflight.get(k) is done:
                    del self._inflight[k]
                if not done.cancelled() and done.exception() is not None:  # also marks it retrieved
                    log.error("wake run failed", extra={"worker_id": k[0], "error": type(done.exception()).__name__})

            task.add_done_callback(_forget)
        # a cancelled waiter must not abort a wake other callers share; the run itself is deadline-bounded
        return await asyncio.shield(task)

    async def ui_statuses(self) -> dict[str, WorkerUiStatus]:
        """UI status (10.8) of every configured worker host (execution/model workers)."""
        workers = [h for h in self._hosts.hosts if h.role in ("execution_worker", "model_worker")]
        async with self._sm() as s:
            rows = {w.id: w.state for w in (await s.execute(select(Worker).where(Worker.id.in_([h.id for h in workers])))).scalars()}
        return {h.id: ui_status_for(rows.get(h.id), wakeable=h.wake_on_lan.enabled and bool(h.wake_on_lan.mac)) for h in workers}

    async def worker_ui_status(self, worker_id: str) -> WorkerUiStatus:
        """UI status (10.8) of one worker from its registry row and WOL configuration."""
        host = self._host(worker_id)
        async with self._sm() as s:
            row = await s.get(Worker, worker_id)
            state = row.state if row else None
        return ui_status_for(state, wakeable=host.wake_on_lan.enabled and bool(host.wake_on_lan.mac))

    # ------------------------------------------------------------------------------------------ pipeline
    def _host(self, worker_id: str) -> HostConfig:
        try:
            return self._hosts.by_id(worker_id)
        except ConfigError as exc:
            raise WorkerNotFound(f"worker '{worker_id}' is not a configured host", details={"worker_id": worker_id}) from exc

    def _context(self, worker_id: str, job_id: uuid.UUID | None, required: frozenset[str], deadline_seconds: float | None) -> _Run:
        host = self._host(worker_id)
        try:
            validate_address(host.address)
            api_base = validate_base_url(host.worker_api or f"http://{host.address}:{DEFAULT_WORKER_PORT}")
        except InvalidTarget as exc:
            raise ConfigError(f"host '{worker_id}': {exc}", details={"worker_id": worker_id}) from exc
        url = httpx.URL(api_base)
        api_port = url.port or (443 if url.scheme == "https" else 80)
        wol = host.wake_on_lan
        budget = float(wol.ping_timeout_seconds + wol.service_timeout_seconds) + self.settings.overall_slack_seconds
        if deadline_seconds is not None:
            budget = min(budget, max(float(deadline_seconds), 0.0))
        start = self._clock()
        ctx = _Run(
            worker_id=worker_id,
            job_id=job_id,
            host=host,
            api_base=api_base,
            api_host=url.host,
            api_port=api_port,
            ssh_port=host.ssh.port if host.ssh else 22,
            required=required,
            started=start,
            started_wall=_aware(self._now()),
            deadline=start + budget,
        )
        ctx.services, ctx.services_skipped = self._service_targets(ctx)
        return ctx

    async def _run(self, ctx: _Run) -> ReadyResult:
        try:
            host_up = await self._detect(ctx)
            if host_up:
                for stage in (WakeStage.wol_send, WakeStage.ping):
                    await self._stage_done(ctx, stage, StageStatus.skipped, detail="host already reachable")
            else:
                await self._wake(ctx)
                await self._wait_ping(ctx)
            service_deadline = min(self._clock() + float(ctx.host.wake_on_lan.service_timeout_seconds), ctx.deadline)
            await self._wait_ssh(ctx, service_deadline)
            await self._wait_api(ctx, service_deadline)
            await self._wait_services(ctx, service_deadline)
            await self._wait_capabilities(ctx, service_deadline)
        except _StageFailed as failed:
            return await self._fail(ctx, failed)
        return await self._succeed(ctx)

    # ---------------------------------------------------------------- detect
    async def _detect(self, ctx: _Run) -> bool:
        t0 = self._clock()
        async with self._sm() as s:
            row = await s.get(Worker, ctx.worker_id)
            state = WorkerState(row.state) if row else None
        ctx.initial_state = state
        quick = await self._probes.tcp_connect(ctx.api_host, ctx.api_port, timeout_seconds=self.settings.quick_probe_timeout_seconds)
        host_up = quick.ok
        # the registry must report the worker *after* this wake when it was not demonstrably live before
        if not (host_up and state in LIVE_STATES):
            ctx.fresh_since = ctx.started_wall - timedelta(seconds=self.settings.heartbeat_skew_seconds)
        wol = ctx.host.wake_on_lan
        await self._stage_done(
            ctx,
            WakeStage.detect,
            StageStatus.ok,
            attempts=1,
            duration_ms=self._ms(t0),
            detail="host reachable" if host_up else "host not reachable",
            data={
                "state": state.value if state else None,
                "registered": state is not None,
                "host_reachable": host_up,
                "probe": quick.detail,
                "wol_enabled": bool(wol.enabled and wol.mac),
                "action": "probe" if host_up else "wake",
            },
        )
        return host_up

    # ---------------------------------------------------------------- wol_send
    async def _wake(self, ctx: _Run) -> None:
        wol = ctx.host.wake_on_lan
        t0 = self._clock()
        if not (wol.enabled and wol.mac):
            raise _StageFailed(
                WakeStage.wol_send,
                f"host {ctx.worker_id} is not reachable and Wake-on-LAN is not enabled/configured",
                attempts=0,
                data={"reason": "wol_not_configured"},
                duration_ms=self._ms(t0),
            )
        async with self._sm() as s:
            if (await s.get(Worker, ctx.worker_id)) is not None:
                await self._registry.set_state(s, ctx.worker_id, WorkerState.waking, reason="wake_on_lan")
                await s.commit()
        max_attempts = max(1, wol.max_attempts)
        errors: list[str] = []
        while ctx.send_attempts < max_attempts:
            err = await self._send_once(ctx)
            if err is None:
                await self._stage_done(
                    ctx,
                    WakeStage.wol_send,
                    StageStatus.ok,
                    attempts=ctx.send_attempts,
                    duration_ms=self._ms(t0),
                    detail=f"magic packet sent to {wol.broadcast}:{wol.port}",
                    data={"errors": errors} if errors else {},
                )
                return
            errors.append(clip(err.message, 200))
            if str(err.details.get("reason", "")).startswith("invalid_"):
                break  # invalid MAC/broadcast/port: retrying cannot help
            if ctx.send_attempts < max_attempts:
                await self._sleep(self.settings.send_retry_backoff_seconds)
        raise _StageFailed(
            WakeStage.wol_send,
            f"magic packet could not be sent: {errors[-1] if errors else 'unknown error'}",
            attempts=ctx.send_attempts,
            data={"errors": errors, "broadcast": wol.broadcast, "port": wol.port},
            duration_ms=self._ms(t0),
        )

    async def _send_once(self, ctx: _Run) -> WolError | None:
        wol = ctx.host.wake_on_lan
        ctx.send_attempts += 1
        attempt = ctx.send_attempts
        try:
            sent = await self._send(
                wol.mac,
                wol.broadcast,
                wol.port or DEFAULT_WOL_PORT,
                copies=self.settings.packet_copies,
                source_address=self.settings.source_address,
            )
        except WolError as exc:
            log.warning("wake-on-lan send failed", extra={"worker_id": ctx.worker_id, "attempt": attempt, "code": exc.code})
            return exc
        ctx.packets_sent += 1
        ctx.woke = True
        async with self._sm() as s:
            await append_event(
                s,
                EventType.WORKER_WAKE_SENT,
                source_type=SOURCE_TYPE,
                source_id=ctx.worker_id,
                job_id=ctx.job_id,
                payload={
                    "worker_id": ctx.worker_id,
                    "mac": wol.mac,
                    "broadcast": wol.broadcast,
                    "port": wol.port,
                    "attempt": attempt,
                    "max_attempts": wol.max_attempts,
                    "bytes": sent,
                    "ui_status": WorkerUiStatus.WAKING.value,
                },
            )
            if ctx.job_id is not None:
                await self._status_line(s, ctx, f"Worker {ctx.worker_id}: Wake-on-LAN gesendet (Versuch {attempt}/{wol.max_attempts})")
            await s.commit()
        return None

    # ---------------------------------------------------------------- waiting
    async def _wait(
        self,
        ctx: _Run,
        probe: Callable[[float], Awaitable[ProbeOutcome]],
        deadline: float,
        *,
        between: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[bool, ProbeOutcome | None, int]:
        """Probe until ok, fatal or deadline. ``between`` runs after every failed probe (re-sends)."""
        attempts = 0
        last: ProbeOutcome | None = None
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False, last, attempts
            attempts += 1
            last = await probe(min(self.settings.probe_timeout_seconds, remaining))
            if last.ok:
                return True, last, attempts
            if last.fatal:
                return False, last, attempts
            if between is not None:
                await between()
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False, last, attempts
            await self._sleep(min(self.settings.probe_interval_seconds, remaining))

    async def _run_stage(
        self,
        ctx: _Run,
        stage: WakeStage,
        probe: Callable[[float], Awaitable[ProbeOutcome]],
        deadline: float,
        *,
        timeout_label: str,
        between: Callable[[], Awaitable[None]] | None = None,
    ) -> ProbeOutcome:
        t0 = self._clock()
        ok, last, attempts = await self._wait(ctx, probe, deadline, between=between)
        if not ok:
            detail = last.detail if last else "no probe possible before the deadline"
            data = {"last": detail, **({k: _short(v) for k, v in last.data.items() if k != "json"} if last else {})}
            prefix = "" if last and last.fatal else f"{timeout_label} not reached within {round(max(deadline - t0, 0.0), 1)} s: "
            raise _StageFailed(stage, prefix + detail, attempts=attempts, data=data, duration_ms=self._ms(t0))
        assert last is not None
        await self._stage_done(
            ctx,
            stage,
            StageStatus.ok,
            attempts=attempts,
            duration_ms=self._ms(t0),
            detail=last.detail,
            data={k: _short(v) for k, v in last.data.items() if k != "json"},
        )
        return last

    async def _wait_ping(self, ctx: _Run) -> None:
        wol = ctx.host.wake_on_lan
        deadline = min(self._clock() + float(wol.ping_timeout_seconds), ctx.deadline)
        max_attempts = max(1, wol.max_attempts)
        every = self.settings.resend_interval_seconds or max(float(wol.ping_timeout_seconds) / max_attempts, 1.0)
        next_resend = self._clock() + every

        async def resend() -> None:
            nonlocal next_resend
            if ctx.send_attempts < max_attempts and self._clock() >= next_resend:
                await self._send_once(ctx)  # a failed re-send is logged; the first packet already went out
                next_resend = self._clock() + every

        async def probe(timeout_seconds: float) -> ProbeOutcome:
            return await self._probes.ping(ctx.host.address, timeout_seconds=timeout_seconds, fallback_port=ctx.ssh_port)

        await self._run_stage(ctx, WakeStage.ping, probe, deadline, timeout_label="ping", between=resend)

    async def _wait_ssh(self, ctx: _Run, deadline: float) -> None:
        async def probe(timeout_seconds: float) -> ProbeOutcome:
            return await self._probes.ssh_banner(ctx.host.address, ctx.ssh_port, timeout_seconds=timeout_seconds)

        await self._run_stage(ctx, WakeStage.ssh, probe, deadline, timeout_label=f"ssh port {ctx.ssh_port}")

    async def _wait_api(self, ctx: _Run, deadline: float) -> None:
        url = f"{ctx.api_base}/health"
        accepted = _HEALTH_STATUS_OK if self.settings.accept_degraded_health else frozenset({"ok"})

        async def probe(timeout_seconds: float) -> ProbeOutcome:
            out = await self._probes.http_get(url, timeout_seconds=timeout_seconds)
            if not out.ok:
                return out
            body = out.data.get("json")
            data: dict[str, Any] = {"status_code": out.data.get("status_code")}
            if isinstance(body, dict):
                for key in ("status", "state", "worker_id", "worker_version", "protocol_version"):
                    if key in body:
                        data[key] = _short(body[key])
                reported = body.get("worker_id")
                if reported is not None and reported != ctx.worker_id:
                    return ProbeOutcome(
                        False, f"{url} answers as worker {_short(reported)!r}, expected {ctx.worker_id!r}", data, fatal=True
                    )
                status = body.get("status")
                if status is not None and status not in accepted:
                    return ProbeOutcome(False, f"{url} health status {_short(status)!r}", data)
            return ProbeOutcome(True, f"worker API {url} healthy", data)

        await self._run_stage(ctx, WakeStage.worker_api, probe, deadline, timeout_label="worker API")

    def _service_targets(self, ctx: _Run) -> tuple[list[tuple[str, str | None, int]], list[str]]:
        api_host = ctx.api_host
        targets: list[tuple[str, str | None, int]] = []
        skipped: list[str] = []
        for svc in ctx.host.services:
            same_as_api = svc.port == ctx.api_port and api_host in (ctx.host.address, validate_address(ctx.host.address))
            if same_as_api or svc.port == ctx.ssh_port:
                skipped.append(svc.name)  # already verified by the worker_api / ssh stage
                continue
            try:
                url = build_http_url(ctx.host.address, svc.port, svc.http_path) if svc.http_path else None
            except InvalidTarget as exc:
                raise ConfigError(f"host '{ctx.worker_id}' service '{svc.name}': {exc}") from exc
            targets.append((svc.name, url, svc.port))
        return targets, skipped

    async def _wait_services(self, ctx: _Run, deadline: float) -> None:
        targets, skipped = ctx.services, ctx.services_skipped
        if not targets:
            await self._stage_done(
                ctx, WakeStage.services, StageStatus.skipped, detail="no further host services", data={"skipped": skipped}
            )
            return
        pending = {name: (url, port) for name, url, port in targets}
        last: dict[str, str] = {}

        async def probe(timeout_seconds: float) -> ProbeOutcome:
            for name, (url, port) in list(pending.items()):
                if url:
                    out = await self._probes.http_get(url, timeout_seconds=timeout_seconds, ok_statuses=range(200, 400))
                else:
                    out = await self._probes.tcp_connect(ctx.host.address, port, timeout_seconds=timeout_seconds)
                last[name] = out.detail
                if out.ok:
                    del pending[name]
            if pending:
                return ProbeOutcome(
                    False, "waiting for " + ", ".join(f"{n} ({last.get(n, '-')})" for n in sorted(pending)), {"pending": sorted(pending)}
                )
            return ProbeOutcome(
                True, "services up: " + ", ".join(n for n, _, _ in targets), {"services": [n for n, _, _ in targets], "skipped": skipped}
            )

        await self._run_stage(ctx, WakeStage.services, probe, deadline, timeout_label="host services")

    async def _wait_capabilities(self, ctx: _Run, deadline: float) -> None:
        fresh_since = ctx.fresh_since

        async def probe(_timeout_seconds: float) -> ProbeOutcome:
            async with self._sm() as s:
                snap = await self._lookup(s, ctx.worker_id)
            if snap is None:
                return ProbeOutcome(False, f"worker {ctx.worker_id} is not in the capability registry", {"registered": False})
            missing = sorted(ctx.required - snap.capabilities)
            data: dict[str, Any] = {"state": snap.state.value, "missing": missing, "required": sorted(ctx.required)}
            if not snap.compatible:
                return ProbeOutcome(False, f"worker {ctx.worker_id} is incompatible (protocol/kind mismatch)", data, fatal=True)
            if fresh_since is not None and (snap.last_heartbeat_at is None or _aware(snap.last_heartbeat_at) < fresh_since):
                return ProbeOutcome(False, f"no heartbeat from {ctx.worker_id} since the wake started", data)
            if snap.state not in DISPATCHABLE_STATES:
                return ProbeOutcome(False, f"worker {ctx.worker_id} reports state {snap.state.value}", data)
            if missing:
                return ProbeOutcome(False, f"worker {ctx.worker_id} lacks capabilities {', '.join(missing)}", data)
            return ProbeOutcome(True, "capability registry current", {**data, "capabilities": sorted(snap.capabilities)})

        await self._run_stage(ctx, WakeStage.capabilities, probe, deadline, timeout_label="capability registry")

    # ---------------------------------------------------------------- recording
    @staticmethod
    def _progress_ui(ctx: _Run) -> WorkerUiStatus:
        """UI status while the pipeline runs: WAKING after a magic packet, the live state for a worker that
        was already up, STARTING for a reachable host whose worker is not (yet) registered as live."""
        if ctx.woke:
            return WorkerUiStatus.WAKING
        if ctx.initial_state in LIVE_STATES:
            return ui_status_for(ctx.initial_state)
        return WorkerUiStatus.STARTING

    def _ms(self, since: float) -> int:
        return max(0, int((self._clock() - since) * 1000))

    async def _status_line(self, s: AsyncSession, ctx: _Run, text: str, severity: Severity = Severity.info) -> None:
        await append_event(
            s,
            EventType.STATUS,
            source_type=SOURCE_TYPE,
            source_id=ctx.worker_id,
            job_id=ctx.job_id,
            severity=severity,
            payload={"text": text[:500]},
        )

    async def _stage_done(
        self,
        ctx: _Run,
        stage: WakeStage,
        status: StageStatus,
        *,
        attempts: int = 0,
        duration_ms: int = 0,
        detail: str = "",
        data: dict[str, Any] | None = None,
    ) -> StageResult:
        result = StageResult(stage=stage, status=status, attempts=attempts, duration_ms=duration_ms, detail=clip(detail), data=data or {})
        ctx.stages.append(result)
        ui = self._progress_ui(ctx)
        async with self._sm() as s:
            await record_wake_event(
                s,
                worker_id=ctx.worker_id,
                job_id=ctx.job_id,
                stage=stage.value,
                status=status.value,
                details={"detail": result.detail, "attempts": attempts, "duration_ms": duration_ms, **result.data},
            )
            await append_event(
                s,
                EventType.WORKER_WAKE_STAGE,
                source_type=SOURCE_TYPE,
                source_id=ctx.worker_id,
                job_id=ctx.job_id,
                duration_ms=duration_ms,
                payload={
                    "worker_id": ctx.worker_id,
                    "stage": stage.value,
                    "status": status.value,
                    "attempts": attempts,
                    "detail": result.detail,
                    "data": result.data,
                    "ui_status": ui.value,
                },
            )
            await s.commit()
        return result

    async def _fail(self, ctx: _Run, failed: _StageFailed) -> ReadyResult:
        code = STAGE_FAILURE[failed.stage]
        detail = clip(failed.detail)
        result = StageResult(
            stage=failed.stage,
            status=StageStatus.failed,
            attempts=failed.attempts,
            duration_ms=failed.duration_ms,
            error_code=code,
            detail=detail,
            data=failed.data,
        )
        ctx.stages.append(result)
        total_ms = self._ms(ctx.started)
        async with self._sm() as s:
            await record_wake_event(
                s,
                worker_id=ctx.worker_id,
                job_id=ctx.job_id,
                stage=failed.stage.value,
                status=StageStatus.failed.value,
                error_code=code.value,
                details={"detail": detail, "attempts": failed.attempts, "duration_ms": failed.duration_ms, **failed.data},
            )
            await append_event(
                s,
                EventType.WORKER_WAKE_STAGE,
                source_type=SOURCE_TYPE,
                source_id=ctx.worker_id,
                job_id=ctx.job_id,
                severity=Severity.error,
                duration_ms=failed.duration_ms,
                payload={
                    "worker_id": ctx.worker_id,
                    "stage": failed.stage.value,
                    "status": StageStatus.failed.value,
                    "attempts": failed.attempts,
                    "error_code": code.value,
                    "detail": detail,
                    "data": failed.data,
                    "ui_status": WorkerUiStatus.ERROR.value,
                },
            )
            # never clobber a state a live heartbeat reported (e.g. ready worker lacking one capability)
            row = await s.get(Worker, ctx.worker_id, with_for_update=True)
            state: WorkerState | None = WorkerState(row.state) if row else None
            if row is not None and state not in LIVE_STATES:
                await self._registry.set_state(s, ctx.worker_id, WorkerState.error, reason=code.value.lower())
                state = WorkerState.error
            await append_event(
                s,
                EventType.WORKER_WAKE_FAILED,
                source_type=SOURCE_TYPE,
                source_id=ctx.worker_id,
                job_id=ctx.job_id,
                severity=Severity.error,
                duration_ms=total_ms,
                payload={
                    "worker_id": ctx.worker_id,
                    "error_code": code.value,
                    "stage": failed.stage.value,
                    "message": detail,
                    "packets_sent": ctx.packets_sent,
                    "worker_state": state.value if state else None,
                    "ui_status": WorkerUiStatus.ERROR.value,
                },
            )
            if ctx.job_id is not None:
                await self._status_line(s, ctx, f"Worker {ctx.worker_id}: nicht bereit ({code.value})", Severity.error)
            await s.commit()
        log.warning("worker not ready", extra={"worker_id": ctx.worker_id, "code": code.value, "stage": failed.stage.value})
        return ReadyResult(
            worker_id=ctx.worker_id,
            ready=False,
            status=WorkerUiStatus.ERROR,
            error_code=code,
            message=detail,
            woke=ctx.woke,
            packets_sent=ctx.packets_sent,
            worker_state=state,
            duration_ms=total_ms,
            stages=list(ctx.stages),
        )

    async def _succeed(self, ctx: _Run) -> ReadyResult:
        total_ms = self._ms(ctx.started)
        async with self._sm() as s:
            await force_worker_state(s, ctx.worker_id, WorkerState.ready, reason="wake_ready", only_from=_PROMOTABLE, now=self._now())
            row = await s.get(Worker, ctx.worker_id)
            state = WorkerState(row.state) if row else None
            ui = ui_status_for(state) if state in DISPATCHABLE_STATES else WorkerUiStatus.READY
            result = StageResult(stage=WakeStage.ready, status=StageStatus.ok, duration_ms=total_ms, detail="worker ready for dispatch")
            ctx.stages.append(result)
            await record_wake_event(
                s,
                worker_id=ctx.worker_id,
                job_id=ctx.job_id,
                stage=WakeStage.ready.value,
                status=StageStatus.ok.value,
                details={"duration_ms": total_ms, "woke": ctx.woke, "packets_sent": ctx.packets_sent},
            )
            await append_event(
                s,
                EventType.WORKER_READY,
                source_type=SOURCE_TYPE,
                source_id=ctx.worker_id,
                job_id=ctx.job_id,
                duration_ms=total_ms,
                payload={
                    "worker_id": ctx.worker_id,
                    "woke": ctx.woke,
                    "packets_sent": ctx.packets_sent,
                    "worker_state": state.value if state else None,
                    "capabilities": sorted(ctx.required),
                    "ui_status": ui.value,
                },
            )
            if ctx.job_id is not None and ctx.woke:
                await self._status_line(s, ctx, f"Worker {ctx.worker_id}: geweckt und bereit")
            await s.commit()
        return ReadyResult(
            worker_id=ctx.worker_id,
            ready=True,
            status=ui,
            woke=ctx.woke,
            packets_sent=ctx.packets_sent,
            worker_state=state,
            duration_ms=total_ms,
            stages=list(ctx.stages),
            message="ready",
        )


async def ensure_workers_ready(
    controller: WakeController, worker_ids: Sequence[str], job_id: uuid.UUID | None = None
) -> dict[str, ReadyResult]:
    """Wake several workers concurrently (e.g. execution + model worker of one job)."""
    results = await asyncio.gather(*(controller.ensure_worker_ready(w, job_id) for w in worker_ids))
    return dict(zip(worker_ids, results, strict=True))
