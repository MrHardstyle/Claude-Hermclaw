"""P07 registry against PostgreSQL: registration, heartbeat ingest, capability registry, health,
offline detection (7.8) and version compatibility (7.9)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION, GpuInfo, LoadedModel, WorkerHeartbeat
from hermclaw.core.config import CapabilitiesConfig, HostConfig, HostsConfig
from hermclaw.persistence.models import Event, Worker, WorkerCapability, WorkerHealth
from hermclaw.workers.errors import WorkerNotFound
from hermclaw.workers.monitor import OfflineMonitor
from hermclaw.workers.registry import RegistrySettings, WorkerRegistry

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _wid(prefix: str = "w") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def hb(worker_id: str, *, kind: WorkerKind = WorkerKind.execution, state: WorkerState = WorkerState.ready, **kw: Any) -> WorkerHeartbeat:
    values: dict[str, Any] = {
        "worker_id": worker_id,
        "hostname": f"{worker_id}.lan",
        "kind": kind,
        "state": state,
        "worker_version": "0.1.0rc1",
        "capabilities": ["sandbox", "testing"],
        "cpu_percent": 12.5,
        "load_avg": [0.1, 0.2, 0.3],
        "ram_total_mb": 32000,
        "ram_used_mb": 8000,
        "disk_free_mb": 100000,
        "uptime_seconds": 42,
        "service_versions": {"podman": "4.9.3"},
        "sent_at": datetime.now(UTC),
    }
    values.update(kw)
    return WorkerHeartbeat(**values)


async def events(session: AsyncSession, worker_id: str, event_type: str | None = None) -> list[Event]:
    stmt = select(Event).where(Event.source_id == worker_id).order_by(Event.sequence)
    if event_type:
        stmt = stmt.where(Event.event_type == event_type)
    return list((await session.execute(stmt)).scalars())


def hosts(*entries: tuple[str, str]) -> HostsConfig:
    items = []
    for wid, role in entries:
        items.append(
            HostConfig(
                id=wid,
                address="192.168.178.222",
                role=role,  # type: ignore[arg-type]
                worker_api="http://192.168.178.222:8787/",
                wake_on_lan={"enabled": True, "mac": "00:11:22:33:44:55"},  # type: ignore[arg-type]
                labels={"rack": "a"},
            )
        )
    return HostsConfig(hosts=items)


async def test_register_from_example_config(session: AsyncSession) -> None:
    cfg = HostsConfig.model_validate(yaml.safe_load((ROOT / "config/hosts.example.yaml").read_text()))
    caps = CapabilitiesConfig.model_validate(yaml.safe_load((ROOT / "config/capabilities.example.yaml").read_text()))
    reg = WorkerRegistry()
    rows = await reg.register_from_config(session, cfg, caps)
    ids = {r.id: r for r in rows}
    assert set(ids) == {"exec-222", "model-224"}  # only execution/model worker roles
    assert ids["exec-222"].kind == "execution" and ids["model-224"].kind == "model"
    assert ids["exec-222"].api_url == "http://192.168.178.222:8787"
    assert ids["exec-222"].state == "offline" and ids["exec-222"].wol["enabled"] is True
    info = await reg.get_worker(session, "exec-222")
    assert "testing" in info.declared_capabilities and info.wol_enabled and info.capabilities == []
    assert len(await events(session, "exec-222", EventType.WORKER_REGISTERED)) == 1


async def test_register_idempotent_update_and_removal(session: AsyncSession) -> None:
    a, b = _wid("exec"), _wid("model")
    reg = WorkerRegistry()
    await reg.register_from_config(session, hosts((a, "execution_worker"), (b, "model_worker")))
    await reg.register_from_config(session, hosts((a, "execution_worker"), (b, "model_worker")))
    assert len(await events(session, a, EventType.WORKER_REGISTERED)) == 1  # no change -> no event
    changed = hosts((a, "execution_worker"))
    changed.hosts[0].worker_api = "http://192.168.178.222:9999"
    await reg.register_from_config(session, changed)
    evs = await events(session, a, EventType.WORKER_REGISTERED)
    assert evs[-1].payload["action"] == "config_updated" and "api_url" in evs[-1].payload["changed"]
    removed = await events(session, b, EventType.WORKER_REGISTERED)
    assert removed[-1].payload["action"] == "removed_from_config"
    assert not (await reg.get_worker(session, b)).in_config


async def test_heartbeat_ingest_updates_row_capabilities_health_and_events(session: AsyncSession) -> None:
    wid = _wid("exec")
    reg = WorkerRegistry()
    await reg.register_from_config(session, hosts((wid, "execution_worker")))
    out = await reg.ingest_heartbeat(
        session,
        hb(
            wid,
            active_job="job-1",
            active_step="step-1",
            state=WorkerState.busy,
            gpus=[GpuInfo(index=0, name="GTX 1080", memory_total_mb=8192)],
            loaded_models=[LoadedModel(name="qwen3:8b", size_bytes=1)],
        ),
        remote_addr="192.168.178.222",
    )
    assert out.accepted and out.compatible and out.state == WorkerState.busy and out.previous_state == WorkerState.offline
    assert out.capabilities_added == ["sandbox", "testing"]
    row = await session.get(Worker, wid)
    assert row is not None
    assert (row.state, row.worker_version, row.protocol_version, row.active_job_id, row.active_step_id) == (
        "busy",
        "0.1.0rc1",
        WORKER_PROTOCOL_VERSION,
        "job-1",
        "step-1",
    )
    assert row.last_heartbeat_at is not None and row.hostname == f"{wid}.lan"
    caps = (await session.execute(select(WorkerCapability).where(WorkerCapability.worker_id == wid))).scalars().all()
    assert {c.capability for c in caps} == {"sandbox", "testing"} and all(c.version == "0.1.0rc1" for c in caps)
    health = (await session.execute(select(WorkerHealth).where(WorkerHealth.worker_id == wid))).scalars().all()
    assert len(health) == 1 and health[0].cpu_percent == 12.5 and health[0].gpus[0]["name"] == "GTX 1080"
    state_events = await events(session, wid, EventType.WORKER_STATE)
    transition = [e for e in state_events if e.payload["reason"] == "heartbeat"]
    assert len(transition) == 1 and transition[0].payload["from"] == "offline" and transition[0].payload["to"] == "busy"
    assert [e.payload["added"] for e in state_events if e.payload["reason"] == "capabilities_changed"] == [["sandbox", "testing"]]

    # same state again: health row added, no new state event; capability change -> capabilities_changed event
    await reg.ingest_heartbeat(session, hb(wid, state=WorkerState.busy, capabilities=["sandbox", "docker"]))
    caps2 = (await session.execute(select(WorkerCapability.capability).where(WorkerCapability.worker_id == wid))).scalars().all()
    assert set(caps2) == {"sandbox", "docker"}
    state_events2 = await events(session, wid, EventType.WORKER_STATE)
    assert len(state_events2) == len(state_events) + 1
    assert state_events2[-1].payload["reason"] == "capabilities_changed"
    assert state_events2[-1].payload["added"] == ["docker"] and state_events2[-1].payload["removed"] == ["testing"]
    n_health = await session.scalar(select(func.count()).select_from(WorkerHealth).where(WorkerHealth.worker_id == wid))
    assert n_health == 2

    detail = await reg.get_worker(session, wid, health_limit=10)
    assert len(detail.health) == 2 and detail.resources is not None and detail.resources.ram_total_mb == 32000
    assert detail.service_versions == {"podman": "4.9.3"} and detail.heartbeat_age_seconds is not None


async def test_stale_and_duplicate_heartbeats_are_ignored(session: AsyncSession) -> None:
    wid = _wid("exec")
    reg = WorkerRegistry()
    t = datetime.now(UTC)
    await reg.ingest_heartbeat(session, hb(wid, sent_at=t, state=WorkerState.busy))
    dup = await reg.ingest_heartbeat(session, hb(wid, sent_at=t, state=WorkerState.ready))
    older = await reg.ingest_heartbeat(session, hb(wid, sent_at=t - timedelta(seconds=5), state=WorkerState.ready))
    assert not dup.accepted and dup.stale and not older.accepted
    row = await session.get(Worker, wid)
    assert row is not None and row.state == "busy"


async def test_auto_registration_and_strict_mode(session: AsyncSession) -> None:
    wid = _wid("exec")
    out = await WorkerRegistry().ingest_heartbeat(session, hb(wid), remote_addr="10.0.0.9")
    assert out.created and out.state == WorkerState.ready
    reg_events = await events(session, wid, EventType.WORKER_REGISTERED)
    assert reg_events[0].payload["source"] == "heartbeat" and reg_events[0].severity == "warning"
    info = await WorkerRegistry().get_worker(session, wid)
    assert info.address == "10.0.0.9" and not info.in_config
    with pytest.raises(WorkerNotFound):
        await WorkerRegistry(RegistrySettings(auto_register=False)).ingest_heartbeat(session, hb(_wid("exec")))


async def test_protocol_version_mismatch_forces_error_state(session: AsyncSession) -> None:
    wid = _wid("exec")
    reg = WorkerRegistry()
    await reg.ingest_heartbeat(session, hb(wid))
    out = await reg.ingest_heartbeat(session, hb(wid, protocol_version=WORKER_PROTOCOL_VERSION + 1))
    assert not out.compatible and out.state == WorkerState.error and out.reason == "protocol_version_mismatch"
    ev = (await events(session, wid, EventType.WORKER_STATE))[-1]
    assert ev.severity == "error" and ev.payload["to"] == "error"
    assert ev.payload["incompatibility"] == {"reason": "protocol_version_mismatch", "expected": WORKER_PROTOCOL_VERSION, "got": 2}
    info = await reg.get_worker(session, wid)
    assert not info.compatible and info.state == WorkerState.error
    assert await reg.select_worker(session, "sandbox") is None or (await reg.select_worker(session, "sandbox")).id != wid  # type: ignore[union-attr]
    # upgrading back to the right protocol version recovers the worker
    out = await reg.ingest_heartbeat(session, hb(wid))
    assert out.compatible and out.state == WorkerState.ready


async def test_kind_mismatch_forces_error(session: AsyncSession) -> None:
    wid = _wid("exec")
    reg = WorkerRegistry()
    await reg.register_from_config(session, hosts((wid, "execution_worker")))
    out = await reg.ingest_heartbeat(session, hb(wid, kind=WorkerKind.model))
    assert out.state == WorkerState.error and out.reason == "kind_mismatch" and not out.compatible


async def test_offline_sweep_after_missed_heartbeats(session: AsyncSession) -> None:
    reg = WorkerRegistry(RegistrySettings(heartbeat_interval_seconds=10, offline_after_missed=3))
    silent, fresh = _wid("exec"), _wid("exec")
    now = datetime.now(UTC)
    await reg.ingest_heartbeat(session, hb(silent, active_job="job-x"), received_at=now - timedelta(seconds=31))
    await reg.ingest_heartbeat(session, hb(fresh), received_at=now - timedelta(seconds=29))
    offline = await reg.sweep_offline(session, now=now)
    assert silent in offline and fresh not in offline
    row = await session.get(Worker, silent)
    assert row is not None and row.state == "offline"
    ev = (await events(session, silent, EventType.WORKER_OFFLINE))[-1]
    assert ev.payload["reason"] == "heartbeat_timeout" and ev.payload["active_job"] == "job-x"
    assert ev.payload["threshold_seconds"] == 30 and ev.payload["silent_seconds"] >= 31
    assert (await events(session, silent, EventType.WORKER_STATE))[-1].payload["to"] == "offline"
    # second sweep: already offline -> no duplicate event
    assert silent not in await reg.sweep_offline(session, now=now)
    # it comes back
    out = await reg.ingest_heartbeat(session, hb(silent))
    assert out.previous_state == WorkerState.offline and out.state == WorkerState.ready


async def test_waking_timeout_and_orchestrator_states(session: AsyncSession) -> None:
    clock = [datetime.now(UTC)]
    reg = WorkerRegistry(RegistrySettings(waking_timeout_seconds=60), clock=lambda: clock[0])
    wid = _wid("model")
    await reg.register_from_config(session, hosts((wid, "model_worker")))
    assert await reg.set_state(session, wid, WorkerState.sleeping, reason="idle_sleep")
    assert not await reg.set_state(session, wid, WorkerState.sleeping, reason="idle_sleep")
    assert await reg.set_state(session, wid, WorkerState.waking, reason="wol_sent")
    assert await reg.sweep_offline(session, now=clock[0] + timedelta(seconds=30)) == []
    assert await reg.sweep_offline(session, now=clock[0] + timedelta(seconds=61)) == [wid]
    ev = (await events(session, wid, EventType.WORKER_OFFLINE))[-1]
    assert ev.payload["reason"] == "wake_timeout"
    with pytest.raises(ValueError):
        await reg.set_state(session, wid, WorkerState.ready, reason="nope")
    with pytest.raises(WorkerNotFound):
        await reg.set_state(session, _wid("none"), WorkerState.offline, reason="x")
    # a heartbeat during waking wins over the sweep
    assert await reg.set_state(session, wid, WorkerState.waking, reason="wol_sent")
    await reg.ingest_heartbeat(session, hb(wid, kind=WorkerKind.model), received_at=clock[0] + timedelta(seconds=1))
    assert await reg.sweep_offline(session, now=clock[0] + timedelta(seconds=20)) == []


async def test_select_worker_by_capability(session: AsyncSession) -> None:
    reg = WorkerRegistry()
    cap = f"cap-{uuid.uuid4().hex[:8]}"
    idle, busy, drained, stale = _wid("exec"), _wid("exec"), _wid("exec"), _wid("exec")
    now = datetime.now(UTC)
    await reg.ingest_heartbeat(session, hb(idle, capabilities=[cap]))
    await reg.ingest_heartbeat(session, hb(busy, capabilities=[cap], state=WorkerState.busy, active_job="j"))
    await reg.ingest_heartbeat(session, hb(drained, capabilities=[cap]))
    await reg.ingest_heartbeat(session, hb(stale, capabilities=[cap]), received_at=now - timedelta(minutes=5))
    await reg.set_drain(session, drained, True, reason="maintenance")
    chosen = await reg.select_worker(session, cap)
    assert chosen is not None and chosen.id == idle and cap in chosen.capabilities
    assert await reg.select_worker(session, cap, exclude=[idle]) is None
    with_busy = await reg.select_worker(session, cap, exclude=[idle], include_busy=True)
    assert with_busy is not None and with_busy.id == busy
    assert await reg.select_worker(session, cap, kind=WorkerKind.model) is None
    assert await reg.select_worker(session, f"{cap}-unknown") is None
    # drained worker reports ready but stays draining until undrained
    out = await reg.ingest_heartbeat(session, hb(drained, capabilities=[cap]))
    assert out.state == WorkerState.draining
    assert await reg.set_drain(session, drained, False) == WorkerState.ready
    assert (await reg.select_worker(session, cap, exclude=[idle])).id == drained  # type: ignore[union-attr]
    listed = {w.id for w in await reg.workers_for_capability(session, cap)}
    assert {idle, busy, drained, stale} <= listed


async def test_list_filters_and_prune(session: AsyncSession) -> None:
    reg = WorkerRegistry()
    wid = _wid("model")
    await reg.ingest_heartbeat(session, hb(wid, kind=WorkerKind.model, capabilities=["ollama"]))
    models = await reg.list_workers(session, kind=WorkerKind.model)
    assert wid in {w.id for w in models} and all(w.kind == WorkerKind.model for w in models)
    assert wid not in {w.id for w in await reg.list_workers(session, state=WorkerState.offline)}
    with pytest.raises(WorkerNotFound):
        await reg.get_worker(session, _wid("missing"))
    assert await reg.prune_health(session, older_than_days=0, now=datetime.now(UTC) + timedelta(seconds=5)) >= 1


async def test_offline_monitor_loop(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    reg = WorkerRegistry(RegistrySettings(heartbeat_interval_seconds=1, offline_after_missed=2))
    wid = _wid("exec")
    async with sessionmaker() as s, s.begin():
        await reg.ingest_heartbeat(s, hb(wid), received_at=datetime.now(UTC) - timedelta(seconds=10))
    monitor = OfflineMonitor(sessionmaker, reg, interval_seconds=0.05)
    assert monitor.interval_seconds == 0.05
    monitor.start()
    try:
        for _ in range(100):
            async with sessionmaker() as s:
                row = await s.get(Worker, wid)
                if row is not None and row.state == "offline":
                    break
            await asyncio.sleep(0.05)
    finally:
        await monitor.stop()
    assert not monitor.running and monitor.sweeps >= 1 and monitor.last_error is None
    async with sessionmaker() as s:
        assert (await s.get(Worker, wid)).state == "offline"  # type: ignore[union-attr]
        assert len(await events(s, wid, EventType.WORKER_OFFLINE)) == 1
        await s.execute(Worker.__table__.delete().where(Worker.id == wid))
        await s.commit()
