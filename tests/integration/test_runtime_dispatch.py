"""P25.3/25.5 worker dispatch through the composition root's pools: config registration → signed heartbeat →
capability-based selection (WorkerRegistry.select_worker) → signed client → remote sandbox command on the real
execution worker daemon app (ASGI transport) with workspace sync. Also: wake before dispatch, drained workers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from hermclaw.contracts.worker import WorkerHeartbeat
from hermclaw.core.config import HostConfig, HostsConfig, get_config
from hermclaw.core.interfaces import ExecutionRequest, WorkspaceHandle
from hermclaw.core.settings import Settings
from hermclaw.runtime.services import ExecutionPool, WorkerPool
from hermclaw.workers.auth import StaticTokenStore
from hermclaw.workers.client import ExecutionWorkerClient
from hermclaw.workers.errors import WorkerUnreachable
from hermclaw.workers.registry import WorkerRegistry
from tests.integration.test_workers_execution_daemon import WORKER, _app
from tests.integration.test_workers_support import TOKEN, lifespan

pytestmark = pytest.mark.asyncio(loop_scope="session")


class FakeWake:
    def __init__(self, registry: WorkerRegistry, sm: Any) -> None:
        self.registry, self.sm, self.calls = registry, sm, []

    async def ensure_worker_ready(self, worker_id: str, job_id: uuid.UUID | None = None, **kw: Any) -> Any:
        self.calls.append((worker_id, kw.get("required_capabilities")))
        await _heartbeat(self.sm, self.registry, worker_id, "ready")  # the woken daemon reports in
        return type("R", (), {"ready": True})()


async def _heartbeat(sm: Any, registry: WorkerRegistry, worker_id: str, state: str) -> None:
    hb = WorkerHeartbeat(
        worker_id=worker_id,
        hostname="exec",
        kind="execution",
        state=state,
        worker_version="0.1.0rc1",
        capabilities=["sandbox"],
        sent_at=datetime.now(UTC),
    )
    async with sm() as s, s.begin():
        await registry.ingest_heartbeat(s, hb)


async def _register(sm: Any, registry: WorkerRegistry) -> None:
    hosts = HostsConfig(
        hosts=[HostConfig(id=WORKER, address="127.0.0.1", role="execution_worker", worker_kind="execution", worker_api="http://exec")]
    )
    async with sm() as s, s.begin():
        await registry.register_from_config(s, hosts, get_config().capabilities)


def _handle(tmp_path: Path) -> WorkspaceHandle:
    ws = tmp_path / "orchestrator" / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / "a.txt").write_text("hello\n")
    return WorkspaceHandle(
        id=uuid.uuid4(), job_id=uuid.uuid4(), path=ws, branch="hermclaw/x", base_branch="main", base_sha="0" * 40, repository_key="demo"
    )


def _pool(sm: Any, registry: WorkerRegistry, app: Any, wake: Any = None) -> WorkerPool:
    return WorkerPool(
        sm,
        registry,
        StaticTokenStore({WORKER: TOKEN}),
        kind="execution",
        client_cls=ExecutionWorkerClient,
        wake=wake,
        client_kwargs={"transport": httpx.ASGITransport(app=app), "get_retries": 0},
    )


async def test_ready_worker_is_selected_and_runs_the_command_remotely(sessionmaker: Any, tmp_path: Path) -> None:
    registry = WorkerRegistry()
    await _register(sessionmaker, registry)
    await _heartbeat(sessionmaker, registry, WORKER, "ready")
    (tmp_path / "worker").mkdir()
    app = _app(tmp_path / "worker")
    async with lifespan(app):
        pool = _pool(sessionmaker, registry, app)
        execution = ExecutionPool(get_config(), Settings(env="production"), mode="auto", workers=pool)
        assert execution.mode == "remote"
        handle = _handle(tmp_path)
        ex = await execution.executor(handle, None)
        res = await ex.run(handle, ExecutionRequest(command="cat a.txt && echo made > b.txt", timeout_seconds=30))
        again = await execution.executor(handle, None)
        await pool.aclose()
    assert res.exit_code == 0 and "hello" in res.stdout
    assert (handle.path / "b.txt").read_text() == "made\n", "remote change synced back to the orchestrator workspace"
    assert ex.client is again.client, "one signed client per worker"  # type: ignore[attr-defined]


async def test_offline_worker_is_woken_before_dispatch_and_drained_worker_is_skipped(sessionmaker: Any, tmp_path: Path) -> None:
    registry = WorkerRegistry()
    await _register(sessionmaker, registry)
    await _heartbeat(sessionmaker, registry, WORKER, "ready")
    async with sessionmaker() as s, s.begin():
        await registry.set_state(s, WORKER, "offline", reason="test: host asleep")
    (tmp_path / "worker").mkdir()
    app = _app(tmp_path / "worker")
    async with lifespan(app):
        wake = FakeWake(registry, sessionmaker)
        pool = _pool(sessionmaker, registry, app, wake)
        client = await pool.client(("sandbox",))
        assert wake.calls == [(WORKER, ["sandbox"])] and client.worker_id == WORKER
        async with sessionmaker() as s, s.begin():
            await registry.set_drain(s, WORKER, True, reason="maintenance")
        with pytest.raises(WorkerUnreachable):
            await _pool(sessionmaker, registry, app).client(("sandbox",))
        async with sessionmaker() as s, s.begin():
            await registry.set_drain(s, WORKER, False)
        await pool.aclose()
