"""P07 failure behaviour with real processes and real TCP:

- a real model-worker daemon process (``python -m worker.model``) heartbeats over TCP into a real
  uvicorn orchestrator (PostgreSQL registry); SIGTERM -> final ``offline`` heartbeat
- SIGKILL (no goodbye) -> the offline sweep declares the worker offline (``worker.offline`` event)
- orchestrator unreachable -> heartbeat sender backs off and keeps running
- daemon refuses to start without a valid credential
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import WorkerKind
from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import ModelLoadRequest
from hermclaw.persistence.models import Event, Worker, WorkerHealth
from hermclaw.workers.api import WorkerApiContext, install_workers_api
from hermclaw.workers.auth import StaticTokenStore
from hermclaw.workers.client import ModelWorkerClient
from hermclaw.workers.errors import WorkerUnreachable
from hermclaw.workers.registry import RegistrySettings, WorkerRegistry
from tests.integration.test_workers_support import TOKEN, FakeOllama, free_port, make_settings, write_token
from tests.unit.test_workers_daemon_common import write_fake_nvidia_smi
from worker.common.heartbeat import HeartbeatError, HeartbeatSender
from worker.common.state import DaemonState

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def orchestrator(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[tuple[str, WorkerRegistry, list[str]]]:
    """Real uvicorn server on 127.0.0.1 serving /api/workers; yields (url, registry, worker ids to clean)."""
    ids: list[str] = []
    registry = WorkerRegistry(RegistrySettings(heartbeat_interval_seconds=1, offline_after_missed=3))
    app = FastAPI()

    class _Store(StaticTokenStore):
        def tokens_for(self, worker_id: str) -> list[str]:
            return [TOKEN] if worker_id in ids else []

    install_workers_api(app, WorkerApiContext(sessionmaker=sessionmaker, token_store=_Store({}), registry=registry))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False, lifespan="off"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", registry, ids
    finally:
        server.should_exit = True
        await task
        async with sessionmaker() as s:
            for wid in ids:
                await s.execute(Worker.__table__.delete().where(Worker.id == wid))
            await s.commit()


@pytest.fixture
async def ollama() -> AsyncIterator[FakeOllama]:
    fake = await FakeOllama().start()
    yield fake
    await fake.stop()


def _spawn_model_daemon(tmp_path: Path, worker_id: str, orchestrator_url: str, ollama_url: str, port: int) -> subprocess.Popen[bytes]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "WORKER_ID": worker_id,
        "WORKER_KIND": "model",
        "WORKER_TOKEN_FILE": str(write_token(tmp_path)),
        "ORCHESTRATOR_URL": orchestrator_url,
        "WORKER_BIND": f"127.0.0.1:{port}",
        "WORKER_HEARTBEAT_SECONDS": "1",
        "WORKER_DATA_DIR": str(tmp_path / "data"),
        "WORKER_CAPABILITIES": "embedding,chat",
        "OLLAMA_URL": ollama_url,
        "WORKER_NVIDIA_SMI": str(write_fake_nvidia_smi(bin_dir)),
        "WORKER_LOG_LEVEL": "INFO",
    }
    log = (tmp_path / "daemon.log").open("wb")
    return subprocess.Popen([sys.executable, "-m", "worker.model"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


async def _wait_state(sessionmaker: async_sessionmaker[AsyncSession], worker_id: str, state: str, timeout: float = 20.0) -> Worker:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with sessionmaker() as s:
            row = await s.get(Worker, worker_id)
            if row is not None and row.state == state:
                return row
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"worker {worker_id} did not reach state {state} (last: {row.state if row else None})")
        await asyncio.sleep(0.1)


async def _stop(proc: subprocess.Popen[bytes], sig: int) -> int:
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(sig)
    for _ in range(200):
        if proc.poll() is not None:
            return proc.returncode
        await asyncio.sleep(0.05)
    proc.kill()
    return proc.wait()


async def test_real_daemon_process_heartbeats_and_graceful_shutdown(
    orchestrator: tuple[str, WorkerRegistry, list[str]],
    ollama: FakeOllama,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    url, _registry, ids = orchestrator
    wid = f"model-{uuid.uuid4().hex[:8]}"
    ids.append(wid)
    port = free_port()
    proc = _spawn_model_daemon(tmp_path, wid, url, ollama.url, port)
    try:
        row = await _wait_state(sessionmaker, wid, "ready")
        assert row.kind == "model" and row.metadata_["service_versions"]["ollama"] == "0.32.12"
        async with ModelWorkerClient(f"http://127.0.0.1:{port}", worker_id=wid, token=TOKEN) as client:
            health = await client.health()
            assert health.status == "ok" and health.orchestrator_reachable is True
            res = await client.load_model(ModelLoadRequest(model="qwen3:8b", context_tokens=8192))
            assert res.loaded
        # the next heartbeats carry the resident model and GPU telemetry
        for _ in range(100):
            async with sessionmaker() as s:
                last = (
                    await s.execute(select(WorkerHealth).where(WorkerHealth.worker_id == wid).order_by(WorkerHealth.id.desc()).limit(1))
                ).scalar_one_or_none()
            if last is not None and last.loaded_models:
                break
            await asyncio.sleep(0.1)
        assert last is not None and last.loaded_models[0]["name"] == "qwen3:8b" and last.gpus[0]["name"] == "NVIDIA GeForce GTX 1080"
        caps = (await _registry.get_worker(s, wid)) if False else None
        async with sessionmaker() as s:
            info = await _registry.get_worker(s, wid)
        assert {"chat", "embedding", "ollama", "gpu_telemetry"} <= set(info.capabilities) and caps is None
        # uvicorn re-raises the captured SIGTERM after its graceful shutdown (systemd treats that as a clean stop)
        assert await _stop(proc, signal.SIGTERM) in (0, -signal.SIGTERM)
        row = await _wait_state(sessionmaker, wid, "offline", timeout=5)
        assert row.metadata_["reported_state"] == "offline"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    log = (tmp_path / "daemon.log").read_text(encoding="utf-8", errors="replace")
    assert TOKEN not in log  # the credential never reaches the logs


async def test_killed_daemon_is_detected_by_offline_sweep(
    orchestrator: tuple[str, WorkerRegistry, list[str]],
    ollama: FakeOllama,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    url, registry, ids = orchestrator
    wid = f"model-{uuid.uuid4().hex[:8]}"
    ids.append(wid)
    port = free_port()
    proc = _spawn_model_daemon(tmp_path, wid, url, ollama.url, port)
    try:
        await _wait_state(sessionmaker, wid, "ready")
        await _stop(proc, signal.SIGKILL)  # no final heartbeat
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    async with sessionmaker() as s:
        assert (await s.get(Worker, wid)).state == "ready"  # type: ignore[union-attr]
    async with sessionmaker() as s, s.begin():
        assert await registry.sweep_offline(s, now=datetime.now(UTC) + timedelta(seconds=4)) == [wid]
    async with sessionmaker() as s:
        evs = (await s.execute(select(Event).where(Event.source_id == wid, Event.event_type == EventType.WORKER_OFFLINE))).scalars().all()
        assert len(evs) == 1 and evs[0].payload["reason"] == "heartbeat_timeout"
    with pytest.raises(WorkerUnreachable):
        async with ModelWorkerClient(f"http://127.0.0.1:{port}", worker_id=wid, token=TOKEN, get_retries=1, retry_backoff_seconds=0) as c:
            await c.health()


async def test_heartbeat_sender_backs_off_when_orchestrator_unreachable(tmp_path: Path) -> None:
    wid = "exec-unreachable"
    settings = make_settings(tmp_path, WorkerKind.execution, wid, orchestrator_url=f"http://127.0.0.1:{free_port()}", heartbeat_seconds=8)
    state = DaemonState(wid, WorkerKind.execution)
    sender = HeartbeatSender(settings, state, lambda: TOKEN)
    for expected in (1.0, 2.0, 4.0, 8.0, 8.0):
        with pytest.raises(HeartbeatError) as exc:
            await sender.send_once()
        assert exc.value.code == "ORCHESTRATOR_UNREACHABLE"
        assert sender.next_delay() == expected
    assert state.orchestrator_reachable is False and sender.failures == 5
    sender.start()
    await asyncio.sleep(0.05)
    assert sender.running  # the loop survives failures
    await sender.stop(final=True, final_timeout_seconds=1)
    assert not sender.running


def test_daemon_refuses_to_start_without_credential(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "WORKER_ID": "model-nocred",
        "WORKER_KIND": "model",
        "WORKER_TOKEN_FILE": str(tmp_path / "missing-token"),
        "WORKER_BIND": f"127.0.0.1:{free_port()}",
    }
    out = subprocess.run([sys.executable, "-m", "worker.model"], cwd=ROOT, env=env, capture_output=True, timeout=60, check=False)
    assert out.returncode == 2 and b"WORKER_TOKEN_MISSING" in out.stderr
    env["WORKER_KIND"] = "execution"
    out = subprocess.run([sys.executable, "-m", "worker.model"], cwd=ROOT, env=env, capture_output=True, timeout=60, check=False)
    assert out.returncode == 2 and b"CONFIG_INVALID" in out.stderr


async def test_orchestrator_rejects_heartbeats_with_rotated_out_token(
    orchestrator: tuple[str, WorkerRegistry, list[str]], tmp_path: Path
) -> None:
    url, _registry, ids = orchestrator
    wid = f"exec-{uuid.uuid4().hex[:8]}"
    ids.append(wid)
    settings = make_settings(tmp_path, WorkerKind.execution, wid, orchestrator_url=url)
    sender = HeartbeatSender(settings, DaemonState(wid, WorkerKind.execution), lambda: "r" * 64)
    with pytest.raises(HeartbeatError) as exc:
        await sender.send_once()
    assert exc.value.status_code == 401 and exc.value.code.startswith("WORKER_AUTH_")
    await sender.stop(final=False)
    async with httpx.AsyncClient() as raw:
        assert (await raw.post(f"{url}/api/workers/heartbeat", content=b"{}")).status_code == 401
