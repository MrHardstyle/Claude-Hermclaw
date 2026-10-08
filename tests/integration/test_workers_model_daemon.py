"""P07 7.7 model worker daemon (``.224``) through the real :class:`ModelWorkerClient`, talking over real
HTTP to a local fake Ollama server (aiohttp) and a test-only ``nvidia-smi`` script."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.contracts.worker import ModelLoadRequest
from hermclaw.workers.client import ModelWorkerClient
from hermclaw.workers.errors import WorkerRemoteError
from hermclaw.workers.schemas import SelftestRequest
from tests.integration.test_workers_support import TOKEN, FakeOllama, free_port, lifespan, make_settings
from tests.unit.test_workers_daemon_common import write_fake_nvidia_smi
from worker.common.server import DaemonContext
from worker.model.app import create_app

pytestmark = pytest.mark.integration
WORKER = "model-test-224"


@pytest.fixture
async def ollama() -> AsyncIterator[FakeOllama]:
    fake = await FakeOllama().start()
    yield fake
    await fake.stop()


def _app(tmp_path: Path, ollama_url: str, **kw: Any) -> FastAPI:
    smi_dir = tmp_path / "bin"
    smi_dir.mkdir(exist_ok=True)
    smi = write_fake_nvidia_smi(smi_dir)
    settings = make_settings(tmp_path, WorkerKind.model, WORKER, ollama_url=ollama_url, nvidia_smi=str(smi))
    kw.setdefault("poll_seconds", 0.01)
    return create_app(settings, heartbeat=False, **kw)


def _client(app: FastAPI) -> ModelWorkerClient:
    return ModelWorkerClient("http://model", worker_id=WORKER, token=TOKEN, transport=httpx.ASGITransport(app=app), get_retries=0)


@pytest.fixture
async def daemon(tmp_path: Path, ollama: FakeOllama) -> AsyncIterator[tuple[FastAPI, ModelWorkerClient]]:
    app = _app(tmp_path, ollama.url)
    async with lifespan(app), _client(app) as c:
        yield app, c


async def test_health_and_gpu(daemon: tuple[FastAPI, ModelWorkerClient]) -> None:
    _app_, c = daemon
    h = await c.health()
    assert h.status == "ok" and h.checks == {"ollama": True, "gpu": True} and h.kind == WorkerKind.model
    gpu = await c.gpu_info()
    assert gpu.available and gpu.gpus[0].name == "NVIDIA GeForce GTX 1080" and gpu.gpus[0].memory_total_mb == 8192
    assert (await c.gpus())[0].driver == "550.163.01"


async def test_models_load_unload_cycle(daemon: tuple[FastAPI, ModelWorkerClient], ollama: FakeOllama) -> None:
    _app_, c = daemon
    models = await c.models()
    assert models.loaded == [] and "gemma4:26b" in models.installed
    res = await c.load_model(ModelLoadRequest(model="gemma4:26b", context_tokens=16384, keep_alive="15m"))
    assert res.loaded and res.model == "gemma4:26b" and res.context_length == 16384 and res.size_vram_bytes > 0
    gen = [b for kind, b in ollama.requests if kind == "generate"][-1]
    assert gen == {"model": "gemma4:26b", "prompt": "", "stream": False, "keep_alive": "15m", "options": {"num_ctx": 16384}}
    loaded = await c.loaded_models()
    assert [m.name for m in loaded] == ["gemma4:26b"] and loaded[0].context_length == 16384
    # name normalisation: "qwen3:8b" vs "embeddinggemma" (implicit :latest)
    await c.load_model(ModelLoadRequest(model="embeddinggemma", context_tokens=2048))
    assert {m.name for m in await c.loaded_models()} == {"gemma4:26b", "embeddinggemma:latest"}
    un = await c.unload_model("gemma4:26b")
    assert un.unloaded and un.was_loaded
    unload_req = [b for kind, b in ollama.requests if kind == "generate"][-1]
    assert unload_req["keep_alive"] == 0 and "prompt" not in unload_req
    assert {m.name for m in await c.loaded_models()} == {"embeddinggemma:latest"}
    again = await c.unload_model("gemma4:26b")
    assert again.unloaded and not again.was_loaded


async def test_exclusive_load_unloads_others_except_keep(daemon: tuple[FastAPI, ModelWorkerClient], ollama: FakeOllama) -> None:
    _app_, c = daemon
    await c.load_model(ModelLoadRequest(model="gemma4:26b", context_tokens=8192))
    await c.load_model(ModelLoadRequest(model="embeddinggemma:latest", context_tokens=2048))
    res = await c.load_model(ModelLoadRequest(model="qwen3:8b", context_tokens=8192), exclusive=True, keep=["embeddinggemma"])
    assert res.unloaded_others == ["gemma4:26b"]
    assert {m.name for m in await c.loaded_models()} == {"qwen3:8b", "embeddinggemma:latest"}


async def test_load_errors(daemon: tuple[FastAPI, ModelWorkerClient], ollama: FakeOllama) -> None:
    _app_, c = daemon
    with pytest.raises(WorkerRemoteError) as exc:
        await c.load_model(ModelLoadRequest(model="not-installed:1b", context_tokens=2048))
    assert exc.value.status_code == 404 and exc.value.remote_code == "MODEL_NOT_INSTALLED"
    ollama.fail_load = "gemma4:26b"
    with pytest.raises(WorkerRemoteError) as exc:
        await c.load_model(ModelLoadRequest(model="gemma4:26b", context_tokens=65536))
    assert exc.value.status_code == 502 and exc.value.remote_code == "OLLAMA_ERROR" and "memory" in exc.value.message
    # the daemon validates independently of the client (raw body below the contract minimum)
    with pytest.raises(WorkerRemoteError) as exc:
        await c._request("POST", "/v1/models/load", json_body={"model": "gemma4:26b", "context_tokens": 10})
    assert exc.value.status_code == 422 and exc.value.remote_code == "VALIDATION_FAILED"


async def test_unload_timeout(tmp_path: Path, ollama: FakeOllama) -> None:
    app = _app(tmp_path, ollama.url, unload_timeout_seconds=0.2)
    async with lifespan(app), _client(app) as c:
        await c.load_model(ModelLoadRequest(model="qwen3:8b", context_tokens=4096))
        ollama.stuck_unload = True
        with pytest.raises(WorkerRemoteError) as exc:
            await c.unload_model("qwen3:8b")
    assert exc.value.status_code == 504 and exc.value.remote_code == "MODEL_UNLOAD_TIMEOUT"


async def test_selftest_with_probe(daemon: tuple[FastAPI, ModelWorkerClient]) -> None:
    _app_, c = daemon
    basic = await c.selftest()
    assert [ch.name for ch in basic.checks] == ["ollama_version", "ollama_ps", "gpu", "disk_free"]
    assert basic.checks[0].detail == "0.32.12"
    full = await c.selftest(SelftestRequest(model="qwen3:8b", context_tokens=2048))
    probe = full.checks[-1]
    assert probe.name == "inference:qwen3:8b" and probe.ok and "eval_count=2" in probe.detail
    assert "OK" not in probe.detail  # generated text is never returned


async def test_ollama_down_degrades_health_and_heartbeat_state(tmp_path: Path) -> None:
    app = _app(tmp_path, f"http://127.0.0.1:{free_port()}")
    async with lifespan(app), _client(app) as c:
        h = await c.health()
        assert h.status == "degraded" and h.checks["ollama"] is False
        extras = await app.state.model.extras()
        assert extras.readiness_error and "ollama unavailable" in extras.readiness_error and extras.gpus
        app.state.daemon.state.readiness_error = extras.readiness_error
        assert (await c.health()).state == WorkerState.error
        with pytest.raises(WorkerRemoteError) as exc:
            await c.models()
        assert exc.value.status_code == 503 and exc.value.remote_code == "OLLAMA_UNREACHABLE"
        st = await c.selftest(SelftestRequest(model="qwen3:8b"))
        assert not st.ok and [ch.name for ch in st.checks] == ["ollama_version", "gpu", "disk_free"]


async def test_heartbeat_extras_report_models_gpu_and_versions(daemon: tuple[FastAPI, ModelWorkerClient]) -> None:
    app, c = daemon
    await c.load_model(ModelLoadRequest(model="qwen3:8b", context_tokens=8192))
    extras = await app.state.model.extras()
    assert extras.readiness_error is None
    assert extras.service_versions == {"ollama": "0.32.12", "nvidia_driver": "550.163.01"}
    assert [m.name for m in extras.loaded_models] == ["qwen3:8b"] and extras.gpus[0].index == 0


async def test_media_extension_point_routes_are_signed(tmp_path: Path, ollama: FakeOllama) -> None:
    router = APIRouter(prefix="/v1/media")

    @router.get("/ping")
    async def ping(request: Request) -> dict[str, Any]:
        ctx = request.app.state.daemon
        assert isinstance(ctx, DaemonContext)
        return {"worker": ctx.settings.worker_id}

    app = _app(tmp_path, ollama.url, extra_routers=[router])
    async with lifespan(app), _client(app) as c:
        r = await c._request("GET", "/v1/media/ping")
        assert json.loads(r.content) == {"worker": WORKER}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://model") as raw:
        assert (await raw.get("/v1/media/ping")).status_code == 401
