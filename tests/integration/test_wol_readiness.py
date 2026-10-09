"""P10 readiness pipeline end to end against PostgreSQL and real local network services.

A :class:`FakeHost` simulates a sleeping worker host on 127.0.0.1: its "NIC" is a UDP listener that
"boots" the host (SSH banner server, worker API, Ollama-like service, registry heartbeat) when it receives
the magic packet for its MAC. Everything the controller touches is real: UDP, TCP, HTTP, the registry and
the event store.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION, WorkerHeartbeat
from hermclaw.core.config import HostConfig, HostsConfig, ServiceProbe, SshConfig, WakeOnLanConfig
from hermclaw.persistence.models import Event, WakeEvent, Worker
from hermclaw.wol import (
    ReadyResult,
    StageStatus,
    WakeController,
    WakeSettings,
    WakeStage,
    WorkerUiStatus,
    build_magic_packet,
    ensure_workers_ready,
)
from hermclaw.workers.registry import WorkerRegistry

pytestmark = pytest.mark.integration

MAC = "52:54:00:aa:bb:cc"
FAST = WakeSettings(
    probe_interval_seconds=0.05,
    probe_timeout_seconds=0.5,
    quick_probe_timeout_seconds=0.3,
    send_retry_backoff_seconds=0.05,
    resend_interval_seconds=0.4,
    heartbeat_skew_seconds=0.0,
    overall_slack_seconds=1.0,
)
ALL_STAGES = [
    WakeStage.detect,
    WakeStage.wol_send,
    WakeStage.ping,
    WakeStage.ssh,
    WakeStage.worker_api,
    WakeStage.services,
    WakeStage.capabilities,
    WakeStage.ready,
]


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wid(prefix: str = "wol") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class _Nic(asyncio.DatagramProtocol):
    def __init__(self, host: FakeHost) -> None:
        self.host = host

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.host.packets.append(data)
        if data == build_magic_packet(self.host.mac) and self.host.boot_on_wake and self.host.boot_task is None:
            self.host.boot_task = asyncio.ensure_future(self.host.boot())


class FakeHost:
    """A worker host on 127.0.0.1 with SSH, worker API, an Ollama-like service and a UDP "NIC"."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        worker_id: str,
        *,
        kind: WorkerKind = WorkerKind.model,
        capabilities: Sequence[str] = ("model_inference",),
        boot_delay: float = 0.2,
        boot_on_wake: bool = True,
        with_ssh: bool = True,
        with_api: bool = True,
        with_ollama: bool = True,
        heartbeat_on_boot: bool = True,
        health: dict[str, Any] | None = None,
        api_http_status: int = 200,
        ollama_status: int = 200,
        protocol_version: int = WORKER_PROTOCOL_VERSION,
    ) -> None:
        self.sm = sessionmaker
        self.worker_id = worker_id
        self.kind = kind
        self.capabilities = list(capabilities)
        self.mac = MAC
        self.boot_delay = boot_delay
        self.boot_on_wake = boot_on_wake
        self.with_ssh, self.with_api, self.with_ollama = with_ssh, with_api, with_ollama
        self.heartbeat_on_boot = heartbeat_on_boot
        self.health = health
        self.api_http_status = api_http_status
        self.ollama_status = ollama_status
        self.protocol_version = protocol_version
        self.ssh_port, self.api_port, self.ollama_port = free_port(), free_port(), free_port()
        self.wol_port = free_port(socket.SOCK_DGRAM)
        self.packets: list[bytes] = []
        self.boot_task: asyncio.Future[None] | None = None
        self.booted = asyncio.Event()
        self.health_calls = 0
        self._nic: asyncio.DatagramTransport | None = None
        self._ssh: asyncio.Server | None = None
        self._runners: list[web.AppRunner] = []

    # ------------------------------------------------------------------ config
    def host_config(self, *, address: str = "127.0.0.1", services: list[ServiceProbe] | None = None, **wol: Any) -> HostConfig:
        wol_values: dict[str, Any] = {
            "enabled": True,
            "mac": self.mac,
            "broadcast": "127.0.0.1",
            "port": self.wol_port,
            "ping_timeout_seconds": 3,
            "service_timeout_seconds": 3,
            "max_attempts": 3,
        }
        wol_values.update(wol)
        return HostConfig(
            id=self.worker_id,
            address=address,
            role="model_worker" if self.kind == WorkerKind.model else "execution_worker",
            worker_kind=self.kind.value,
            worker_api=f"http://{address}:{self.api_port}",
            ssh=SshConfig(port=self.ssh_port),
            wake_on_lan=WakeOnLanConfig(**wol_values),
            services=services
            if services is not None
            else [
                ServiceProbe(name="worker_api", port=self.api_port, http_path="/health"),
                ServiceProbe(name="ollama", port=self.ollama_port, http_path="/api/version"),
            ],
        )

    # ------------------------------------------------------------------ lifecycle
    async def start_nic(self) -> None:
        loop = asyncio.get_running_loop()
        self._nic, _ = await loop.create_datagram_endpoint(lambda: _Nic(self), local_addr=("127.0.0.1", self.wol_port))

    async def _ssh_handler(self, _r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.write(b"SSH-2.0-OpenSSH_9.6p1 FakeHost\r\n")
        with contextlib.suppress(Exception):
            await w.drain()
        w.close()

    async def _http(self, app: web.Application, port: int) -> None:
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        self._runners.append(runner)

    async def start_services(self) -> None:
        if self.with_ssh and self._ssh is None:
            self._ssh = await asyncio.start_server(self._ssh_handler, "127.0.0.1", self.ssh_port)
        if self.with_api:

            async def health(_req: web.Request) -> web.Response:
                self.health_calls += 1
                body = self.health or {
                    "status": "ok",
                    "worker_id": self.worker_id,
                    "kind": self.kind.value,
                    "state": "ready",
                    "worker_version": "0.1.0rc1",
                    "protocol_version": self.protocol_version,
                }
                return web.json_response(body, status=self.api_http_status)

            app = web.Application()
            app.router.add_get("/health", health)
            await self._http(app, self.api_port)
        if self.with_ollama:

            async def version(_req: web.Request) -> web.Response:
                return web.json_response({"version": "0.12.6"}, status=self.ollama_status)

            app = web.Application()
            app.router.add_get("/api/version", version)
            await self._http(app, self.ollama_port)

    async def boot(self) -> None:
        await asyncio.sleep(self.boot_delay)
        await self.start_services()
        if self.heartbeat_on_boot:
            await self.heartbeat()
        self.booted.set()

    async def heartbeat(
        self, *, state: WorkerState = WorkerState.ready, capabilities: Sequence[str] | None = None, received_at: datetime | None = None
    ) -> None:
        hb = WorkerHeartbeat(
            worker_id=self.worker_id,
            hostname=f"{self.worker_id}.lan",
            kind=self.kind,
            state=state,
            protocol_version=self.protocol_version,
            worker_version="0.1.0rc1",
            capabilities=list(self.capabilities if capabilities is None else capabilities),
            sent_at=received_at or datetime.now(UTC),
        )
        async with self.sm() as s:
            await WorkerRegistry().ingest_heartbeat(s, hb, received_at=received_at)
            await s.commit()

    async def stop(self) -> None:
        if self.boot_task is not None:
            with contextlib.suppress(Exception):
                await self.boot_task
        if self._nic is not None:
            self._nic.close()
        if self._ssh is not None:
            self._ssh.close()
            await self._ssh.wait_closed()
        for r in self._runners:
            await r.cleanup()


async def register(sessionmaker: async_sessionmaker[AsyncSession], host: HostConfig) -> None:
    async with sessionmaker() as s:
        await WorkerRegistry().register_from_config(s, HostsConfig(hosts=[host]))
        await s.commit()


@contextlib.asynccontextmanager
async def fake_host(sessionmaker: async_sessionmaker[AsyncSession], **kw: Any) -> AsyncIterator[FakeHost]:
    host = FakeHost(sessionmaker, wid(), **kw)
    await host.start_nic()
    try:
        yield host
    finally:
        await host.stop()


def controller(
    sessionmaker: async_sessionmaker[AsyncSession], *hosts: HostConfig, settings: WakeSettings = FAST, **kw: Any
) -> WakeController:
    return WakeController(sessionmaker, HostsConfig(hosts=list(hosts)), settings=settings, **kw)


async def wake_rows(sessionmaker: async_sessionmaker[AsyncSession], worker_id: str) -> list[WakeEvent]:
    async with sessionmaker() as s:
        rows = await s.execute(select(WakeEvent).where(WakeEvent.worker_id == worker_id).order_by(WakeEvent.created_at))
        return list(rows.scalars())


async def worker_events(sessionmaker: async_sessionmaker[AsyncSession], worker_id: str, *types: str) -> list[Event]:
    async with sessionmaker() as s:
        stmt = select(Event).where(Event.source_id == worker_id).order_by(Event.sequence)
        if types:
            stmt = stmt.where(Event.event_type.in_(types))
        return list((await s.execute(stmt)).scalars())


async def worker_state(sessionmaker: async_sessionmaker[AsyncSession], worker_id: str) -> str | None:
    async with sessionmaker() as s:
        row = await s.get(Worker, worker_id)
        return row.state if row else None


# ============================================================================================== happy paths
async def test_full_wake_pipeline_from_offline(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, boot_delay=0.3) as fh:
        cfg = fh.host_config()
        await register(sessionmaker, cfg)
        assert await worker_state(sessionmaker, fh.worker_id) == "offline"
        wc = controller(sessionmaker, cfg)
        job_id = uuid.uuid4()
        try:
            result = await wc.ensure_worker_ready(fh.worker_id, job_id, required_capabilities=["model_inference"])
        finally:
            await wc.aclose()

        assert result.ready, result.message
        assert result.status == WorkerUiStatus.READY
        assert result.error_code is None
        assert result.woke and result.packets_sent == 1
        assert result.worker_state == WorkerState.ready
        assert [s.stage for s in result.stages] == ALL_STAGES
        assert all(s.status == StageStatus.ok for s in result.stages)
        assert result.raise_for_failure() is result
        # the NIC got exactly one well-formed magic packet for its MAC
        assert fh.packets == [build_magic_packet(MAC)]
        ssh = result.stage(WakeStage.ssh)
        assert ssh is not None and ssh.attempts >= 2  # the host needed boot_delay to come up
        assert ssh.data["banner"].startswith("SSH-2.0-")
        services = result.stage(WakeStage.services)
        assert services is not None and services.data["services"] == ["ollama"]
        assert services.data["skipped"] == ["worker_api"]  # verified by the worker_api stage already
        api = result.stage(WakeStage.worker_api)
        assert api is not None and api.data["status"] == "ok" and api.data["worker_id"] == fh.worker_id

        rows = await wake_rows(sessionmaker, fh.worker_id)
        assert [(r.stage, r.status) for r in rows] == [(s.value, "ok") for s in ALL_STAGES]
        assert all(r.job_id == job_id for r in rows)
        assert all(r.error_code is None for r in rows)

        evs = await worker_events(sessionmaker, fh.worker_id)
        types = [e.event_type for e in evs]
        assert types.count(EventType.WORKER_WAKE_SENT) == 1
        assert types.count(EventType.WORKER_WAKE_STAGE) == 7
        assert types.count(EventType.WORKER_READY) == 1
        assert EventType.WORKER_WAKE_FAILED not in types
        sent = next(e for e in evs if e.event_type == EventType.WORKER_WAKE_SENT)
        assert sent.payload["mac"] == MAC and sent.payload["attempt"] == 1 and sent.payload["ui_status"] == "WAKING"
        assert sent.job_id == job_id
        ready = next(e for e in evs if e.event_type == EventType.WORKER_READY)
        assert ready.payload["ui_status"] == "READY" and ready.payload["woke"] is True
        transitions = [(e.payload["from"], e.payload["to"]) for e in evs if e.event_type == EventType.WORKER_STATE]
        assert ("offline", "waking") in transitions and ("waking", "ready") in transitions
        status_lines = [e.payload["text"] for e in evs if e.event_type == EventType.STATUS]
        assert any("Wake-on-LAN" in t for t in status_lines) and any("bereit" in t for t in status_lines)
        assert await worker_state(sessionmaker, fh.worker_id) == "ready"


async def test_already_ready_worker_is_only_probed(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config()
        await register(sessionmaker, cfg)
        await fh.start_services()
        await fh.heartbeat()
        wc = controller(sessionmaker, cfg)
        try:
            result = await wc.ensure_worker_ready(fh.worker_id)
            assert await wc.worker_ui_status(fh.worker_id) == WorkerUiStatus.READY
            assert await wc.ui_statuses() == {fh.worker_id: WorkerUiStatus.READY}
        finally:
            await wc.aclose()
    assert result.ready and not result.woke and result.packets_sent == 0
    assert fh.packets == []
    assert result.stage(WakeStage.wol_send).status == StageStatus.skipped  # type: ignore[union-attr]
    assert result.stage(WakeStage.ping).status == StageStatus.skipped  # type: ignore[union-attr]
    detect = result.stage(WakeStage.detect)
    assert detect is not None and detect.data["action"] == "probe" and detect.data["state"] == "ready"
    assert not await worker_events(sessionmaker, fh.worker_id, EventType.WORKER_WAKE_SENT)
    # a live worker is never shown as STARTING/WAKING while it is only probed (10.8)
    stage_events = await worker_events(sessionmaker, fh.worker_id, EventType.WORKER_WAKE_STAGE)
    assert {e.payload["ui_status"] for e in stage_events} == {"READY"}


async def test_busy_worker_is_ready_for_dispatch_and_keeps_busy(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config()
        await register(sessionmaker, cfg)
        await fh.start_services()
        await fh.heartbeat(state=WorkerState.busy)
        wc = controller(sessionmaker, cfg)
        try:
            result = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert result.ready and result.status == WorkerUiStatus.BUSY
    assert await worker_state(sessionmaker, fh.worker_id) == "busy"


async def test_host_up_but_registry_offline_waits_for_fresh_heartbeat(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """The registry must be *current* (Bauplan §2.7 step 6): an old heartbeat is not enough."""
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config()
        await register(sessionmaker, cfg)
        await fh.start_services()
        await fh.heartbeat(received_at=datetime(2026, 1, 1, tzinfo=UTC))
        async with sessionmaker() as s:
            await WorkerRegistry().set_state(s, fh.worker_id, WorkerState.offline, reason="test")
            await s.commit()
        wc = controller(sessionmaker, cfg)

        async def late_heartbeat() -> None:
            await asyncio.sleep(0.4)
            await fh.heartbeat()

        try:
            hb_task = asyncio.create_task(late_heartbeat())
            result = await wc.ensure_worker_ready(fh.worker_id)
            await hb_task
        finally:
            await wc.aclose()
    assert result.ready and not result.woke
    caps = result.stage(WakeStage.capabilities)
    assert caps is not None and caps.attempts >= 2


async def test_concurrent_calls_share_one_wake(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, boot_delay=0.2) as fh:
        cfg = fh.host_config()
        await register(sessionmaker, cfg)
        wc = controller(sessionmaker, cfg)
        try:
            r1, r2 = await asyncio.gather(wc.ensure_worker_ready(fh.worker_id), wc.ensure_worker_ready(fh.worker_id))
        finally:
            await wc.aclose()
    assert r1.ready and r1 == r2
    assert len(fh.packets) == 1
    rows = await wake_rows(sessionmaker, fh.worker_id)
    assert [r.stage for r in rows].count("detect") == 1


async def test_cancelled_waiter_does_not_abort_the_shared_wake(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, boot_delay=0.3) as fh:
        cfg = fh.host_config()
        await register(sessionmaker, cfg)
        wc = controller(sessionmaker, cfg)
        try:
            first = asyncio.create_task(wc.ensure_worker_ready(fh.worker_id))
            second = asyncio.create_task(wc.ensure_worker_ready(fh.worker_id))
            await asyncio.sleep(0.1)
            first.cancel()
            result = await second
            with pytest.raises(asyncio.CancelledError):
                await first
        finally:
            await wc.aclose()
    assert result.ready and result.woke


async def test_ensure_workers_ready_wakes_several_hosts(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as a, fake_host(sessionmaker, kind=WorkerKind.execution, capabilities=["sandbox"]) as b:
        ca, cb = a.host_config(), b.host_config()
        await register(sessionmaker, ca)
        await register(sessionmaker, cb)
        wc = controller(sessionmaker, ca, cb)
        try:
            results = await ensure_workers_ready(wc, [a.worker_id, b.worker_id])
        finally:
            await wc.aclose()
    assert set(results) == {a.worker_id, b.worker_id}
    assert all(isinstance(r, ReadyResult) and r.ready and r.woke for r in results.values())


async def test_registry_lookup_is_injectable(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """A non-registry lookup (e.g. a cached registry view) works; the controller then promotes the row."""
    from hermclaw.wol import RegistrySnapshot

    seen: list[str] = []

    async def lookup(_s: AsyncSession, worker_id: str) -> RegistrySnapshot:
        seen.append(worker_id)
        return RegistrySnapshot(state=WorkerState.ready, capabilities=frozenset({"x"}), last_heartbeat_at=datetime.now(UTC))

    async with fake_host(sessionmaker, heartbeat_on_boot=False) as fh:
        cfg = fh.host_config()
        await register(sessionmaker, cfg)
        wc = controller(sessionmaker, cfg, registry_lookup=lookup)
        try:
            result = await wc.ensure_worker_ready(fh.worker_id, required_capabilities=["x"])
        finally:
            await wc.aclose()
    assert result.ready and seen
    assert await worker_state(sessionmaker, fh.worker_id) == "ready"
    transitions = [
        (e.payload["from"], e.payload["to"], e.payload["reason"])
        for e in await worker_events(sessionmaker, fh.worker_id, EventType.WORKER_STATE)
    ]
    assert ("waking", "ready", "wake_ready") in transitions
