"""P07 live verification against the real worker hosts ``.222`` / ``.224`` (BLOCKER-001: skipped by default).

Run on the LAN (from ``.225``) with::

    HERMCLAW_LIVE_EXEC_TOKEN_FILE=/etc/hermclaw/secrets/worker-exec-222 \\
    HERMCLAW_LIVE_MODEL_TOKEN_FILE=/etc/hermclaw/secrets/worker-model-224 \\
    HERMCLAW_LIVE_DATABASE_URL=postgresql+asyncpg://... \\
    .venv/bin/pytest -m live tests/integration/test_workers_live.py

Optional overrides: ``HERMCLAW_LIVE_EXEC_URL`` (default ``http://192.168.178.222:8787``),
``HERMCLAW_LIVE_EXEC_ID`` (``exec-222``), ``HERMCLAW_LIVE_MODEL_URL`` (``http://192.168.178.224:8787``),
``HERMCLAW_LIVE_MODEL_ID`` (``model-224``), ``HERMCLAW_LIVE_MODEL`` (``qwen3:8b``).
"""

from __future__ import annotations

import io
import os
import tarfile
import uuid
from pathlib import Path

import pytest

from hermclaw.contracts.common import WorkerState
from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION, CommandRequest, ModelLoadRequest
from hermclaw.workers.auth import load_token_file
from hermclaw.workers.client import ExecutionWorkerClient, ModelWorkerClient
from hermclaw.workers.registry import RegistrySettings, WorkerRegistry
from hermclaw.workers.schemas import SelftestRequest

pytestmark = [pytest.mark.live, pytest.mark.integration]


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default or "")
    if not value:
        pytest.skip(f"{name} not set (live test, BLOCKER-001)")
    return value


def _token(env_name: str) -> str:
    return load_token_file(Path(_env(env_name)))[0]


def _exec_client() -> ExecutionWorkerClient:
    return ExecutionWorkerClient(
        _env("HERMCLAW_LIVE_EXEC_URL", "http://192.168.178.222:8787"),
        worker_id=_env("HERMCLAW_LIVE_EXEC_ID", "exec-222"),
        token=_token("HERMCLAW_LIVE_EXEC_TOKEN_FILE"),
    )


def _model_client() -> ModelWorkerClient:
    return ModelWorkerClient(
        _env("HERMCLAW_LIVE_MODEL_URL", "http://192.168.178.224:8787"),
        worker_id=_env("HERMCLAW_LIVE_MODEL_ID", "model-224"),
        token=_token("HERMCLAW_LIVE_MODEL_TOKEN_FILE"),
    )


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def test_live_execution_worker_222_roundtrip() -> None:
    async with _exec_client() as c:
        health = await c.ensure_compatible()
        assert health.protocol_version == WORKER_PROTOCOL_VERSION and health.status == "ok"
        selftest = await c.selftest()
        assert selftest.ok, [ch.model_dump() for ch in selftest.checks if not ch.ok]
        ws = f"live-{uuid.uuid4().hex[:12]}"
        try:
            info = await c.upload_workspace(ws, _tar({"hello.txt": b"hermclaw live\n"}))
            assert info.files == 1
            result = await c.run_command(
                CommandRequest(request_id=f"live-{uuid.uuid4().hex}", job_id="live", step_id="live", workspace=ws, command="cat hello.txt")
            )
            assert result.exit_code == 0 and "hermclaw live" in result.stdout and result.sandbox in ("podman", "docker")
            assert (await c.workspace_manifest(ws)).files.keys() == {"hello.txt"}
            recovered = await c.recover_containers()
            assert recovered.errors == []
        finally:
            await c.delete_workspace(ws)


async def test_live_model_worker_224_gpu_and_residency() -> None:
    model = os.environ.get("HERMCLAW_LIVE_MODEL", "qwen3:8b")
    async with _model_client() as c:
        await c.ensure_compatible()
        gpu = await c.gpu_info()
        assert gpu.available and any("1080" in g.name for g in gpu.gpus), gpu.model_dump()
        loaded = await c.load_model(ModelLoadRequest(model=model, context_tokens=4096, keep_alive="2m"))
        assert loaded.loaded and (loaded.context_length is None or loaded.context_length >= 4096)
        assert any(m.name.startswith(model.split(":")[0]) for m in await c.loaded_models())
        unloaded = await c.unload_model(model)
        assert unloaded.unloaded
        assert all(not m.name.startswith(model.split(":")[0]) for m in await c.loaded_models())
        selftest = await c.selftest(SelftestRequest(model=model, context_tokens=2048))
        assert selftest.ok, [ch.model_dump() for ch in selftest.checks if not ch.ok]


async def test_live_registry_sees_fresh_heartbeats() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(_env("HERMCLAW_LIVE_DATABASE_URL"))
    try:
        reg = WorkerRegistry(RegistrySettings())
        async with async_sessionmaker(engine)() as session:
            workers = {w.id: w for w in await reg.list_workers(session)}
        for wid in (os.environ.get("HERMCLAW_LIVE_EXEC_ID", "exec-222"), os.environ.get("HERMCLAW_LIVE_MODEL_ID", "model-224")):
            w = workers.get(wid)
            assert w is not None, f"{wid} not registered"
            assert w.state in (WorkerState.ready, WorkerState.busy) and w.compatible
            assert w.heartbeat_age_seconds is not None and w.heartbeat_age_seconds < reg.settings.offline_after.total_seconds()
    finally:
        await engine.dispose()
