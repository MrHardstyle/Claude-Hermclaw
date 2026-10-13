"""P10 self-review regressions of the readiness pipeline (draining, live-state clobbering, deadlines, serialisation)."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import WorkerKind
from hermclaw.contracts.events import EventType
from hermclaw.wol import StageStatus, WakeFailureCode, WakeSettings, WakeStage
from hermclaw.workers.registry import WorkerRegistry
from tests.integration.test_wol_readiness import FAST, controller, fake_host, register, wake_rows, worker_events, worker_state

pytestmark = pytest.mark.integration


async def test_draining_worker_fails_fast_and_keeps_draining(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """A drained worker never becomes dispatchable: no waiting out the whole service budget."""
    async with fake_host(sessionmaker, kind=WorkerKind.execution, capabilities=["sandbox"]) as fh:
        cfg = fh.host_config(service_timeout_seconds=30)
        await register(sessionmaker, cfg)
        await fh.start_services()
        await fh.heartbeat()
        async with sessionmaker() as s:
            await WorkerRegistry().set_drain(s, fh.worker_id, True)
            await s.commit()
        wc = controller(sessionmaker, cfg)
        t0 = time.monotonic()
        try:
            r = await wc.ensure_worker_ready(fh.worker_id, required_capabilities=["sandbox"])
        finally:
            await wc.aclose()
        assert time.monotonic() - t0 < 5.0
    assert not r.ready and r.error_code == WakeFailureCode.CAPABILITY_MISSING
    assert "draining" in r.message
    caps = r.stage(WakeStage.capabilities)
    assert caps is not None and caps.attempts == 1
    assert await worker_state(sessionmaker, fh.worker_id) == "draining"  # live state is not clobbered to error


async def test_failed_quick_probe_does_not_clobber_a_live_worker(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Heartbeats say ready but the worker API port is closed: a packet goes out, yet the registry state
    stays what the heartbeat reported (no ``waking``/``error`` from the orchestrator)."""
    async with fake_host(sessionmaker, with_api=False, boot_on_wake=False) as fh:
        cfg = fh.host_config(service_timeout_seconds=1)
        await register(sessionmaker, cfg)
        await fh.start_services()  # ssh + ollama, no worker API
        await fh.heartbeat()
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.error_code == WakeFailureCode.WORKER_API_TIMEOUT
    assert r.woke and len(fh.packets) >= 1
    assert await worker_state(sessionmaker, fh.worker_id) == "ready"
    targets = [e.payload["to"] for e in await worker_events(sessionmaker, fh.worker_id, EventType.WORKER_STATE)]
    assert "waking" not in targets and "error" not in targets
    failed = await worker_events(sessionmaker, fh.worker_id, EventType.WORKER_WAKE_FAILED)
    assert failed[0].payload["worker_state"] == "ready"


async def test_send_retries_stop_at_the_overall_deadline(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config(max_attempts=50)
        await register(sessionmaker, cfg)
        # every send fails for real (EADDRNOTAVAIL from a non-local source address)
        settings = WakeSettings(**{**FAST.__dict__, "source_address": "192.0.2.77", "send_retry_backoff_seconds": 0.1})
        wc = controller(sessionmaker, cfg, settings=settings)
        t0 = time.monotonic()
        try:
            r = await wc.ensure_worker_ready(fh.worker_id, deadline_seconds=0.35)
        finally:
            await wc.aclose()
        assert time.monotonic() - t0 < 3.0
    assert r.error_code == WakeFailureCode.WOL_SEND_FAILED
    stage = r.stage(WakeStage.wol_send)
    assert stage is not None and 1 <= stage.attempts < 10
    assert stage.data["errors"][-1] == "overall deadline reached"


async def test_runs_with_different_capability_sets_are_serialised(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Two callers needing different capabilities: the host is woken once, the second run finds it up."""
    async with fake_host(sessionmaker, boot_delay=0.2, capabilities=["model_inference", "embeddings"]) as fh:
        cfg = fh.host_config()
        await register(sessionmaker, cfg)
        wc = controller(sessionmaker, cfg)
        try:
            r1, r2 = await asyncio.gather(
                wc.ensure_worker_ready(fh.worker_id, required_capabilities=["model_inference"]),
                wc.ensure_worker_ready(fh.worker_id, required_capabilities=["embeddings"]),
            )
        finally:
            await wc.aclose()
    assert r1.ready and r2.ready
    assert len(fh.packets) == 1
    assert sorted([r1.woke, r2.woke]) == [False, True]
    second = r2 if r1.woke else r1
    assert second.stage(WakeStage.wol_send).status == StageStatus.skipped  # type: ignore[union-attr]
    rows = await wake_rows(sessionmaker, fh.worker_id)
    stages = [r.stage for r in rows]
    assert stages.count("detect") == 2 and stages.count("ready") == 2
    # serialised: the second run's detect comes after the first run's ready row
    last_detect = max(i for i, st in enumerate(stages) if st == "detect")
    assert stages.index("ready") < last_detect
