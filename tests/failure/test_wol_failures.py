"""P10 10.7: every failure code of the readiness pipeline, produced by real unreachable/broken services."""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.contracts.events import EventType
from hermclaw.core.config import HostsConfig, ServiceProbe
from hermclaw.core.errors import ConfigError
from hermclaw.wol import StageStatus, WakeFailureCode, WakeSettings, WakeStage, WolError, WorkerUiStatus
from hermclaw.workers.errors import WorkerNotFound
from tests.integration.test_wol_readiness import FAST, controller, fake_host, free_port, register, wake_rows, worker_events, worker_state

pytestmark = pytest.mark.integration
UNROUTABLE = "240.0.0.1"  # reserved (class E), connect never succeeds


async def _assert_failed(sm: async_sessionmaker[AsyncSession], worker_id: str, code: WakeFailureCode, stage: WakeStage) -> None:
    rows = await wake_rows(sm, worker_id)
    failed = [r for r in rows if r.status == "failed"]
    assert len(failed) == 1 and failed[0].stage == stage.value and failed[0].error_code == code.value
    assert not [r for r in rows if r.stage == WakeStage.ready.value]
    evs = await worker_events(sm, worker_id, EventType.WORKER_WAKE_FAILED, EventType.WORKER_READY)
    assert [e.event_type for e in evs] == [EventType.WORKER_WAKE_FAILED]
    assert evs[0].payload["error_code"] == code.value and evs[0].payload["stage"] == stage.value
    assert evs[0].payload["ui_status"] == WorkerUiStatus.ERROR.value
    assert evs[0].severity == "error"


# ---------------------------------------------------------------------------------------------- WOL_SEND_FAILED
async def test_wol_disabled_and_host_down(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config(enabled=False)
        await register(sessionmaker, cfg)
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert not r.ready and r.error_code == WakeFailureCode.WOL_SEND_FAILED
    assert r.status == WorkerUiStatus.ERROR and r.packets_sent == 0
    assert r.stage(WakeStage.wol_send).data["reason"] == "wol_not_configured"  # type: ignore[union-attr]
    assert fh.packets == []
    await _assert_failed(sessionmaker, fh.worker_id, WakeFailureCode.WOL_SEND_FAILED, WakeStage.wol_send)
    assert await worker_state(sessionmaker, fh.worker_id) == "error"
    with pytest.raises(WolError) as ei:
        r.raise_for_failure()
    assert ei.value.code == "WOL_SEND_FAILED"


async def test_socket_errors_are_retried_up_to_max_attempts(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config(max_attempts=3)
        await register(sessionmaker, cfg)
        # a non-local source address makes every send fail with a real EADDRNOTAVAIL
        settings = WakeSettings(**{**FAST.__dict__, "source_address": "192.0.2.77"})
        wc = controller(sessionmaker, cfg, settings=settings)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.error_code == WakeFailureCode.WOL_SEND_FAILED and r.packets_sent == 0
    stage = r.stage(WakeStage.wol_send)
    assert stage is not None and stage.attempts == 3 and len(stage.data["errors"]) == 3
    assert fh.packets == []
    await _assert_failed(sessionmaker, fh.worker_id, WakeFailureCode.WOL_SEND_FAILED, WakeStage.wol_send)


async def test_invalid_broadcast_is_not_retried(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config(broadcast="999.0.0.1", max_attempts=5)
        await register(sessionmaker, cfg)
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.error_code == WakeFailureCode.WOL_SEND_FAILED
    assert r.stage(WakeStage.wol_send).attempts == 1  # type: ignore[union-attr]


# ---------------------------------------------------------------------------------------------- PING_TIMEOUT
async def test_ping_timeout_with_bounded_resends(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, boot_on_wake=False) as fh:
        cfg = fh.host_config(address=UNROUTABLE, ping_timeout_seconds=2, max_attempts=3)
        await register(sessionmaker, cfg)
        wc = controller(sessionmaker, cfg)
        t0 = time.monotonic()
        try:
            r = await wc.ensure_worker_ready(fh.worker_id, uuid.uuid4())
        finally:
            await wc.aclose()
        elapsed = time.monotonic() - t0
    assert r.error_code == WakeFailureCode.PING_TIMEOUT
    assert 1.8 <= elapsed < 6.0
    # first packet + re-sends while waiting, never more than max_attempts
    assert r.packets_sent == 3 and len(fh.packets) == 3
    assert len(await worker_events(sessionmaker, fh.worker_id, EventType.WORKER_WAKE_SENT)) == 3
    await _assert_failed(sessionmaker, fh.worker_id, WakeFailureCode.PING_TIMEOUT, WakeStage.ping)
    assert await worker_state(sessionmaker, fh.worker_id) == "error"
    statuses = [e.payload["text"] for e in await worker_events(sessionmaker, fh.worker_id, EventType.STATUS)]
    assert any("PING_TIMEOUT" in t for t in statuses)


# ---------------------------------------------------------------------------------------------- SSH_TIMEOUT
async def test_ssh_timeout(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, with_ssh=False) as fh:
        cfg = fh.host_config(service_timeout_seconds=1)
        await register(sessionmaker, cfg)
        await fh.start_services()  # API answers, so the host counts as awake; sshd never comes up
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.error_code == WakeFailureCode.SSH_TIMEOUT and not r.woke
    ssh = r.stage(WakeStage.ssh)
    assert ssh is not None and ssh.status == StageStatus.failed and ssh.attempts > 1
    await _assert_failed(sessionmaker, fh.worker_id, WakeFailureCode.SSH_TIMEOUT, WakeStage.ssh)


async def test_overall_deadline_caps_every_stage(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, with_ssh=False) as fh:
        cfg = fh.host_config(service_timeout_seconds=30)
        await register(sessionmaker, cfg)
        await fh.start_services()
        wc = controller(sessionmaker, cfg)
        t0 = time.monotonic()
        try:
            r = await wc.ensure_worker_ready(fh.worker_id, deadline_seconds=0.6)
        finally:
            await wc.aclose()
        assert time.monotonic() - t0 < 3.0
    assert r.error_code == WakeFailureCode.SSH_TIMEOUT


# ---------------------------------------------------------------------------------------------- WORKER_API_TIMEOUT
async def test_worker_api_unhealthy(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, api_http_status=503) as fh:
        cfg = fh.host_config(service_timeout_seconds=1)
        await register(sessionmaker, cfg)
        await fh.start_services()
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.error_code == WakeFailureCode.WORKER_API_TIMEOUT
    assert r.stage(WakeStage.worker_api).data["status_code"] == 503  # type: ignore[union-attr]
    assert fh.health_calls > 1
    await _assert_failed(sessionmaker, fh.worker_id, WakeFailureCode.WORKER_API_TIMEOUT, WakeStage.worker_api)


async def test_worker_api_answers_as_another_worker_fails_fast(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, health={"status": "ok", "worker_id": "someone-else"}) as fh:
        cfg = fh.host_config(service_timeout_seconds=20)
        await register(sessionmaker, cfg)
        await fh.start_services()
        wc = controller(sessionmaker, cfg)
        t0 = time.monotonic()
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
        assert time.monotonic() - t0 < 5.0  # fatal: no waiting for the 20 s budget
    assert r.error_code == WakeFailureCode.WORKER_API_TIMEOUT and "someone-else" in r.message
    assert fh.health_calls == 1


async def test_worker_api_reporting_unknown_status_keeps_waiting(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as fh:
        fh.health = {"status": "starting"}
        cfg = fh.host_config(service_timeout_seconds=1)
        await register(sessionmaker, cfg)
        await fh.start_services()
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.error_code == WakeFailureCode.WORKER_API_TIMEOUT and fh.health_calls > 1


# ---------------------------------------------------------------------------------------------- MODEL_SERVICE_TIMEOUT
async def test_model_service_error_status(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, ollama_status=500) as fh:
        cfg = fh.host_config(service_timeout_seconds=1)
        await register(sessionmaker, cfg)
        await fh.start_services()
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.error_code == WakeFailureCode.MODEL_SERVICE_TIMEOUT
    assert r.stage(WakeStage.services).data["pending"] == ["ollama"]  # type: ignore[union-attr]
    await _assert_failed(sessionmaker, fh.worker_id, WakeFailureCode.MODEL_SERVICE_TIMEOUT, WakeStage.services)


async def test_model_service_port_closed_tcp_only_probe(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, with_ollama=False) as fh:
        services = [
            ServiceProbe(name="ollama", port=fh.ollama_port, http_path="/api/version"),
            ServiceProbe(name="redis", port=free_port()),
        ]
        cfg = fh.host_config(service_timeout_seconds=1, services=services)
        await register(sessionmaker, cfg)
        await fh.start_services()
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.error_code == WakeFailureCode.MODEL_SERVICE_TIMEOUT
    assert r.stage(WakeStage.services).data["pending"] == ["ollama", "redis"]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------------------------- CAPABILITY_MISSING
async def test_capability_missing_does_not_clobber_a_live_state(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, kind=WorkerKind.execution, capabilities=["sandbox"]) as fh:
        cfg = fh.host_config(service_timeout_seconds=1)
        await register(sessionmaker, cfg)
        await fh.start_services()
        await fh.heartbeat()
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id, required_capabilities=["sandbox", "gpu_video"])
        finally:
            await wc.aclose()
    assert r.error_code == WakeFailureCode.CAPABILITY_MISSING
    caps = r.stage(WakeStage.capabilities)
    assert caps is not None and caps.data["missing"] == ["gpu_video"]
    await _assert_failed(sessionmaker, fh.worker_id, WakeFailureCode.CAPABILITY_MISSING, WakeStage.capabilities)
    # the worker itself is fine – it just cannot serve this step
    assert await worker_state(sessionmaker, fh.worker_id) == "ready"
    assert r.worker_state == WorkerState.ready


async def test_woken_worker_without_heartbeat_is_capability_missing(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, heartbeat_on_boot=False) as fh:
        cfg = fh.host_config(service_timeout_seconds=1)
        await register(sessionmaker, cfg)
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.woke and r.error_code == WakeFailureCode.CAPABILITY_MISSING
    assert "heartbeat" in r.message
    assert await worker_state(sessionmaker, fh.worker_id) == "error"  # waking → error


async def test_incompatible_worker_fails_fast(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker, protocol_version=999) as fh:
        cfg = fh.host_config(service_timeout_seconds=20)
        await register(sessionmaker, cfg)
        await fh.start_services()
        await fh.heartbeat()  # registry forces error + compatible=false
        wc = controller(sessionmaker, cfg)
        t0 = time.monotonic()
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
        assert time.monotonic() - t0 < 5.0
    assert r.error_code == WakeFailureCode.CAPABILITY_MISSING and "incompatible" in r.message


# ---------------------------------------------------------------------------------------------- configuration errors
async def test_unknown_worker_and_bad_config_raise(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config()
        wc = controller(sessionmaker, cfg)
        try:
            with pytest.raises(WorkerNotFound):
                await wc.ensure_worker_ready("not-configured")
        finally:
            await wc.aclose()
        bad = cfg.model_copy(update={"worker_api": "http://user:secret@127.0.0.1:1"})
        wc = controller(sessionmaker, bad)
        try:
            with pytest.raises(ConfigError):
                await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()


async def test_invalid_service_probe_path_is_a_config_error(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config(services=[ServiceProbe(name="ollama", port=fh.ollama_port, http_path="//169.254.169.254/latest")])
        wc = controller(sessionmaker, cfg)
        try:
            with pytest.raises(ConfigError):
                await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert await wake_rows(sessionmaker, fh.worker_id) == []  # rejected before any stage ran


async def test_unregistered_but_configured_host_still_wakes(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """No registry row yet (fresh install): the wake runs, the registry row appears with the first heartbeat."""
    async with fake_host(sessionmaker) as fh:
        cfg = fh.host_config()
        wc = controller(sessionmaker, cfg)
        try:
            r = await wc.ensure_worker_ready(fh.worker_id)
        finally:
            await wc.aclose()
    assert r.ready and r.woke
    assert r.stage(WakeStage.detect).data["registered"] is False  # type: ignore[union-attr]


def test_failure_codes_are_exactly_the_architecture_codes() -> None:
    assert {c.value for c in WakeFailureCode} == {
        "WOL_SEND_FAILED",
        "PING_TIMEOUT",
        "SSH_TIMEOUT",
        "WORKER_API_TIMEOUT",
        "MODEL_SERVICE_TIMEOUT",
        "CAPABILITY_MISSING",
    }
    assert HostsConfig(hosts=[]).hosts == []
