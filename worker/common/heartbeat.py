"""Heartbeat sender loop (P07 7.2): signed ``POST {ORCHESTRATOR_URL}/api/workers/heartbeat``.

Every heartbeat is a fresh :class:`~hermclaw.contracts.worker.WorkerHeartbeat` snapshot: effective state,
capabilities, CPU/RAM/disk/load (:mod:`worker.common.system`), plus daemon-specific extras (GPUs, loaded
models, service versions, readiness) from an async provider. The interval follows the orchestrator's
ack (``heartbeat_interval_seconds``); on failure the sender backs off exponentially (capped at the
interval) and keeps trying forever - the orchestrator's offline sweep handles the silence.
On shutdown a final ``offline`` heartbeat is sent (best effort) so the registry flips immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION, GpuInfo, LoadedModel, WorkerHeartbeat
from hermclaw.core.logging import get_logger
from hermclaw.workers.auth import WorkerRequestSigner
from hermclaw.workers.schemas import HeartbeatAck
from worker.common.settings import WorkerDaemonSettings
from worker.common.state import DaemonState
from worker.common.system import SystemSampler

log = get_logger(__name__)

HEARTBEAT_PATH = "/api/workers/heartbeat"
MIN_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 600.0


@dataclass
class HeartbeatExtras:
    gpus: list[GpuInfo] = field(default_factory=list)
    loaded_models: list[LoadedModel] = field(default_factory=list)
    service_versions: dict[str, str] = field(default_factory=dict)
    readiness_error: str | None = None


ExtrasProvider = Callable[[], Awaitable[HeartbeatExtras]]


class HeartbeatError(Exception):
    def __init__(self, message: str, *, code: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class HeartbeatSender:
    def __init__(
        self,
        settings: WorkerDaemonSettings,
        state: DaemonState,
        token: Callable[[], str],
        *,
        extras: ExtrasProvider | None = None,
        sampler: SystemSampler | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        if not settings.orchestrator_url:
            raise ValueError("ORCHESTRATOR_URL is not configured")
        self.settings = settings
        self.state = state
        self._extras = extras
        self.sampler = sampler or SystemSampler(settings.data_dir)
        self.interval_seconds = settings.heartbeat_seconds
        self._http = httpx.AsyncClient(
            base_url=settings.orchestrator_url,
            auth=WorkerRequestSigner(settings.worker_id, token),
            timeout=request_timeout_seconds,
            transport=transport,
            headers={"User-Agent": f"hermclaw-worker/{state.version}"},
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.sent = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.last_ack: HeartbeatAck | None = None
        self.last_error: str | None = None
        self._last_compatible: bool | None = None

    async def build(self) -> WorkerHeartbeat:
        extras = HeartbeatExtras()
        if self._extras is not None:
            try:
                extras = await self._extras()
            except Exception as exc:  # a broken probe must not stop heartbeats
                extras = HeartbeatExtras(readiness_error=f"health probe failed: {type(exc).__name__}")
                log.warning("heartbeat extras provider failed", extra={"error": f"{type(exc).__name__}: {exc}"})
            self.state.readiness_error = extras.readiness_error
        metrics = await asyncio.to_thread(self.sampler.sample)
        return WorkerHeartbeat(
            worker_id=self.settings.worker_id,
            hostname=self.settings.hostname,
            kind=self.settings.kind,
            state=self.state.state,
            protocol_version=WORKER_PROTOCOL_VERSION,
            worker_version=self.state.version,
            capabilities=self.settings.all_capabilities(),
            cpu_percent=metrics.cpu_percent,
            load_avg=metrics.load_avg,
            ram_total_mb=metrics.ram_total_mb,
            ram_used_mb=metrics.ram_used_mb,
            disk_free_mb=metrics.disk_free_mb,
            gpus=extras.gpus,
            loaded_models=extras.loaded_models,
            active_job=self.state.active_job,
            active_step=self.state.active_step,
            uptime_seconds=self.state.uptime_seconds,
            service_versions={"hermclaw-worker": self.state.version, **extras.service_versions},
            sent_at=datetime.now(UTC),
        )

    async def send(self, heartbeat: WorkerHeartbeat) -> HeartbeatAck:
        body = json.dumps(heartbeat.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")
        try:
            resp = await self._http.post(HEARTBEAT_PATH, content=body, headers={"Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            raise HeartbeatError("orchestrator heartbeat timed out", code="ORCHESTRATOR_TIMEOUT") from exc
        except httpx.TransportError as exc:
            raise HeartbeatError(f"orchestrator unreachable: {type(exc).__name__}", code="ORCHESTRATOR_UNREACHABLE") from exc
        if resp.status_code != 200:
            code = "HEARTBEAT_REJECTED"
            with contextlib.suppress(ValueError, AttributeError, TypeError):
                code = str(resp.json()["error"]["code"]) or code
            raise HeartbeatError(f"heartbeat rejected: HTTP {resp.status_code} {code}", code=code, status_code=resp.status_code)
        try:
            return HeartbeatAck.model_validate_json(resp.content)
        except ValidationError as exc:
            raise HeartbeatError("unexpected heartbeat answer", code="HEARTBEAT_PROTOCOL_ERROR", status_code=200) from exc

    async def send_once(self) -> HeartbeatAck:
        """Build and send one heartbeat; updates counters and the reachability flags."""
        try:
            ack = await self.send(await self.build())
        except HeartbeatError as exc:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = f"{exc.code}: {exc}"
            self.state.orchestrator_reachable = exc.status_code is not None
            raise
        self.sent += 1
        self.consecutive_failures = 0
        self.last_error = None
        self.last_ack = ack
        self.state.orchestrator_reachable = True
        self.state.orchestrator_compatible = ack.compatible
        self.interval_seconds = min(MAX_INTERVAL_SECONDS, max(MIN_INTERVAL_SECONDS, ack.heartbeat_interval_seconds))
        if ack.compatible != self._last_compatible:
            if not ack.compatible:
                log.error(
                    "orchestrator reports this worker as incompatible",
                    extra={
                        "expected_protocol_version": ack.expected_protocol_version,
                        "own": WORKER_PROTOCOL_VERSION,
                        "ack_message": ack.message,
                    },
                )
            self._last_compatible = ack.compatible
        return ack

    def next_delay(self) -> float:
        if self.consecutive_failures == 0:
            return self.interval_seconds
        return float(min(self.interval_seconds, 2 ** min(self.consecutive_failures - 1, 10)))

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.send_once()
            except HeartbeatError as exc:
                level = log.warning if self.consecutive_failures <= 3 else log.debug
                level("heartbeat failed", extra={"code": exc.code, "failures": self.consecutive_failures})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("heartbeat loop error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.next_delay())

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="worker-heartbeat")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self, *, final: bool = True, final_timeout_seconds: float = 3.0) -> None:
        """Stop the loop; with ``final`` send one last ``offline`` heartbeat (best effort)."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if final:
            self.state.shutting_down = True
            try:
                await asyncio.wait_for(self.send(await self.build()), timeout=final_timeout_seconds)
            except (HeartbeatError, TimeoutError) as exc:
                log.info("final offline heartbeat not delivered", extra={"error": str(exc)})
        await self._http.aclose()
