"""Model residency (8.10) against a real local HTTP server emulating Ollama, plus health checks (8.8) without the
real proxy. Events are written to the real PostgreSQL test database."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import LoadedModel, ModelLoadRequest
from hermclaw.core.errors import ModelError
from hermclaw.models.health import ModelHealthChecker
from hermclaw.models.residency import MODEL_CONTEXT_MISMATCH, ModelHostClient, ModelResidency, OllamaHostClient, WorkerModelHostClient
from hermclaw.persistence.models import Event
from tests.integration.test_models_support import MASTER_KEY, FakeLiteLLM, FakeOllama, models_config, serve

pytestmark = pytest.mark.integration

INSTALLED = ["qwen3:8b", "gemma4:26b", "gemma4:12b", "qwen3-coder:30b", "qwen3.8:27b", "embeddinggemma-2:740m"]


@pytest.fixture
async def ollama() -> AsyncIterator[tuple[FakeOllama, str]]:
    fake = FakeOllama(INSTALLED)
    async with serve(fake.app) as url:
        yield fake, url


async def events(sm: Any, job_id: uuid.UUID) -> list[Event]:
    async with sm() as s:
        return list((await s.execute(select(Event).where(Event.job_id == job_id).order_by(Event.sequence))).scalars())


async def test_ensure_loaded_switches_exclusive_group(ollama: tuple[FakeOllama, str], sessionmaker: Any) -> None:
    fake, url = ollama
    fake.loaded = {"gemma4:26b": 32768, "qwen3:8b": 16384, "embeddinggemma-2:740m": 2048}
    client = OllamaHostClient(url, poll_seconds=0.05)
    assert isinstance(client, ModelHostClient)
    res = ModelResidency(models_config(), client, session_factory=sessionmaker, keep_alive="30m")
    job = uuid.uuid4()
    lease = uuid.uuid4()
    result = await res.ensure_loaded("coder-main", lease_id=lease, job_id=job)
    # the other large-group member is gone, small-group models stay resident
    assert set(fake.loaded) == {"qwen3-coder:30b", "qwen3:8b", "embeddinggemma-2:740m"}
    assert fake.loaded["qwen3-coder:30b"] == 32768
    assert result.unloaded == ["gemma4:26b"] and not result.already_loaded
    assert result.context_length == 32768 and result.context_verified
    assert result.resident_memory_gb == pytest.approx(23 + 7 + 2)
    gen = fake.bodies("/api/generate")
    assert gen[0] == {"model": "gemma4:26b", "keep_alive": 0, "stream": False}
    assert gen[1] == {"model": "qwen3-coder:30b", "prompt": "", "stream": False, "keep_alive": "30m", "options": {"num_ctx": 32768}}
    evs = await events(sessionmaker, job)
    assert [e.event_type for e in evs] == [EventType.MODEL_UNLOADED, EventType.MODEL_LOAD_STARTED, EventType.MODEL_LOAD_FINISHED]
    assert evs[0].payload["model"] == "gemma4:26b" and evs[0].payload["aliases"] == ["planner-gemma"]
    assert all(e.payload["lease_id"] == str(lease) for e in evs)
    assert evs[1].payload["num_ctx"] == 32768 and evs[1].payload["unloaded"] == ["gemma4:26b"]
    assert evs[2].payload["ok"] is True and evs[2].payload["context_verified"] is True
    # idempotent: second call does nothing
    fake.requests.clear()
    again = await res.ensure_loaded("coder-main", job_id=job)
    assert again.already_loaded and fake.bodies("/api/generate") == []
    status = {m.name: m for m in await res.status()}
    assert status["qwen3-coder:30b"].aliases == ["coder-main"] and status["qwen3-coder:30b"].context_length == 32768
    await client.aclose()


async def test_non_exclusive_profile_keeps_others(ollama: tuple[FakeOllama, str]) -> None:
    fake, url = ollama
    fake.loaded = {"qwen3.8:27b": 24576}
    res = ModelResidency(models_config(), OllamaHostClient(url))
    out = await res.ensure_loaded("fast-router")
    assert out.unloaded == [] and set(fake.loaded) == {"qwen3.8:27b", "qwen3:8b"}


async def test_wrong_context_is_reloaded(ollama: tuple[FakeOllama, str]) -> None:
    fake, url = ollama
    fake.loaded = {"qwen3.8:27b": 4096}
    res = ModelResidency(models_config(), OllamaHostClient(url, poll_seconds=0.05))
    out = await res.ensure_loaded("heavy-review")
    assert out.unloaded == ["qwen3.8:27b"] and out.context_length == 24576 and fake.loaded == {"qwen3.8:27b": 24576}


async def test_context_mismatch_after_load_is_an_error(ollama: tuple[FakeOllama, str], sessionmaker: Any) -> None:
    fake, url = ollama
    fake.context_override["gemma4:26b"] = 8192  # e.g. Ollama clamped num_ctx because of memory
    job = uuid.uuid4()
    res = ModelResidency(models_config(), OllamaHostClient(url), session_factory=sessionmaker)
    with pytest.raises(ModelError) as exc:
        await res.ensure_loaded("planner-gemma", job_id=job)
    assert exc.value.code == MODEL_CONTEXT_MISMATCH and exc.value.details["context_length"] == 8192
    evs = await events(sessionmaker, job)
    assert evs[-1].event_type == EventType.MODEL_LOAD_FINISHED and evs[-1].payload["ok"] is False
    assert evs[-1].severity == "error" and evs[-1].payload["error_code"] == MODEL_CONTEXT_MISMATCH
    lenient = ModelResidency(models_config(), OllamaHostClient(url), require_context_match=False)
    fake.loaded.clear()
    out = await lenient.ensure_loaded("planner-gemma")
    assert out.context_length == 8192 and not out.context_verified


async def test_missing_context_report_is_tolerated(ollama: tuple[FakeOllama, str]) -> None:
    fake, url = ollama
    fake.report_context = False
    out = await ModelResidency(models_config(), OllamaHostClient(url)).ensure_loaded("planner-gemma")
    assert out.context_length is None and not out.context_verified and "gemma4:26b" in fake.loaded


async def test_load_failure_and_embedding_load(ollama: tuple[FakeOllama, str]) -> None:
    fake, url = ollama
    fake.installed.remove("qwen3-coder:30b")
    res = ModelResidency(models_config(), OllamaHostClient(url))
    with pytest.raises(ModelError) as exc:
        await res.ensure_loaded("coder-main")
    assert exc.value.code == "MODEL_LOAD_FAILED" and exc.value.details["http_status"] == 404
    out = await res.ensure_loaded("embedding")  # /api/generate rejected -> /api/embed load
    assert out.context_length == 2048
    assert fake.bodies("/api/embed")[-1] == {
        "model": "embeddinggemma-2:740m",
        "input": [],
        "keep_alive": "10m",
        "options": {"num_ctx": 2048},
    }


async def test_unload_and_unload_group(ollama: tuple[FakeOllama, str], sessionmaker: Any) -> None:
    fake, url = ollama
    fake.loaded = {"gemma4:26b": 32768, "qwen3:8b": 16384}
    res = ModelResidency(models_config(), OllamaHostClient(url, poll_seconds=0.05), session_factory=sessionmaker)
    assert await res.unload("planner-gemma", reason="video job needs the GPU") is True
    assert await res.unload("planner-gemma") is False
    fake.loaded = {"gemma4:12b": 32768, "qwen3:8b": 16384, "embeddinggemma-2:740m": 2048}
    assert sorted(await res.unload_group("small-model-224")) == ["embeddinggemma-2:740m", "qwen3:8b"]
    assert set(fake.loaded) == {"gemma4:12b"}


async def test_unload_wait_timeout(ollama: tuple[FakeOllama, str]) -> None:
    fake, url = ollama

    class Sticky(OllamaHostClient):
        async def loaded_models(self) -> list[LoadedModel]:
            return [LoadedModel(name="gemma4:26b", context_length=32768)]

    client = Sticky(url, unload_wait_seconds=0.3, poll_seconds=0.05)
    with pytest.raises(ModelError) as exc:
        await client.unload("gemma4:26b")
    assert exc.value.code == "MODEL_UNLOAD_FAILED"


async def test_host_unreachable() -> None:
    client = OllamaHostClient("http://127.0.0.1:9")
    with pytest.raises(ModelError) as exc:
        await client.loaded_models()
    assert exc.value.code == "MODEL_HOST_UNAVAILABLE"
    res = ModelResidency(models_config(), {"other-host": client})
    with pytest.raises(ModelError):
        await res.ensure_loaded("coder-main")


class FakeWorker:
    """Stands in for hermclaw.workers.client.ModelWorkerClient (same method signatures)."""

    def __init__(self) -> None:
        self.loaded: dict[str, int] = {}
        self.calls: list[tuple[str, Any]] = []

    async def loaded_models(self) -> list[LoadedModel]:
        return [LoadedModel(name=n, context_length=c) for n, c in self.loaded.items()]

    async def load_model(
        self, request: ModelLoadRequest, *, exclusive: bool = False, keep: Sequence[str] = (), timeout_seconds: float | None = 900.0
    ) -> Any:
        self.calls.append(("load", (request, exclusive)))
        self.loaded[request.model] = request.context_tokens
        return {"model": request.model, "loaded": True}

    async def unload_model(self, model: str, *, timeout_seconds: float | None = 180.0) -> Any:
        self.calls.append(("unload", model))
        self.loaded.pop(model, None)
        return {"model": model, "unloaded": True}


async def test_worker_adapter() -> None:
    worker = FakeWorker()
    worker.loaded = {"qwen3.8:27b": 24576}
    res = ModelResidency(models_config(), WorkerModelHostClient(worker))
    out = await res.ensure_loaded("planner-gemma", lease_id="lease-1")
    assert out.unloaded == ["qwen3.8:27b"] and out.context_verified
    assert worker.calls[0] == ("unload", "qwen3.8:27b")
    kind, (request, exclusive) = worker.calls[1]
    assert kind == "load" and request == ModelLoadRequest(model="gemma4:26b", context_tokens=32768, keep_alive="10m") and exclusive is False


# ----------------------------------------------------------------------------------------------- health (8.8)
async def test_health_report_with_fakes(ollama: tuple[FakeOllama, str]) -> None:
    fake, ollama_url = ollama
    fake.installed.remove("gemma4:12b")
    fake.loaded = {"gemma4:26b": 32768, "qwen3:8b": 4096}
    proxy = FakeLiteLLM(aliases=["fast-router", "planner-gemma", "planner-gemma-fallback", "coder-main", "embedding"])
    async with serve(proxy.app) as proxy_url:
        checker = ModelHealthChecker(models_config(proxy_url), ollama_urls={"model-224": ollama_url}, api_key=MASTER_KEY)
        report = await checker.check()
        await checker.aclose()
    assert report.litellm_liveliness.ok and report.litellm_readiness.ok and report.litellm_readiness.details["status"] == "healthy"
    assert report.ollama[0].version == "0.32.12" and report.ollama[0].endpoint.ok
    p = {x.alias: x for x in report.profiles}
    assert p["planner-gemma"].available and p["planner-gemma"].loaded and p["planner-gemma"].context_matches is True
    assert p["fast-router"].loaded and p["fast-router"].context_matches is False and p["fast-router"].available
    assert not p["planner-gemma-fallback"].available and p["planner-gemma-fallback"].installed is False
    assert not p["heavy-review"].available and p["heavy-review"].registered_in_proxy is False
    assert p["coder-main"].available and not p["coder-main"].loaded
    assert not report.healthy and len(report.issues) == 2
    assert all(r.endpoint for r in report.ollama)


async def test_health_unreachable_and_not_ready() -> None:
    proxy = FakeLiteLLM(ready=False)
    async with serve(proxy.app) as proxy_url:
        checker = ModelHealthChecker(models_config(proxy_url), ollama_urls={"model-224": "http://127.0.0.1:9"}, timeout_seconds=2)
        report = await checker.check()
        await checker.aclose()
    assert report.litellm_liveliness.ok and not report.litellm_readiness.ok and report.litellm_readiness.status_code == 503
    assert report.litellm_models is not None and not report.litellm_models.ok  # no api key -> not checked
    assert all(not p.available and p.registered_in_proxy is None for p in report.profiles)
    assert any("unreachable" in (p.reason or "") for p in report.profiles)
    assert not report.ollama[0].endpoint.ok
    down = ModelHealthChecker(models_config("http://127.0.0.1:9"), timeout_seconds=1)
    rep = await down.check()
    await down.aclose()
    assert not rep.litellm_liveliness.ok and not rep.healthy
    assert all("no ollama url" in (p.reason or "") for p in rep.profiles)


async def test_health_wrong_key_does_not_leak() -> None:
    proxy = FakeLiteLLM()
    async with serve(proxy.app) as proxy_url, httpx.AsyncClient() as client:
        checker = ModelHealthChecker(models_config(proxy_url), api_key="sk-not-the-right-key-1234567890", http_client=client)
        report = await checker.check()
    assert report.litellm_models is not None and report.litellm_models.status_code == 401
    assert "sk-not-the-right-key" not in report.model_dump_json()
