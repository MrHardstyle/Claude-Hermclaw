"""Model worker daemon for host ``.224`` (P07 7.7): Ollama residency + GPU telemetry.

Endpoints (everything except ``/health`` must be signed by the orchestrator):

=========================  ==========================================================================
``GET  /health``            liveness/readiness (unauthenticated): Ollama reachable, GPU visible
``GET  /v1/models``         resident (``/api/ps``) and installed (``/api/tags``) models
``POST /v1/models/load``    load a model with ``options.num_ctx`` and ``keep_alive``
                            (``?exclusive=true&keep=<model>`` unloads every other resident model first)
``POST /v1/models/unload``  ``keep_alive: 0``, then poll ``/api/ps`` until the model is gone
``GET  /v1/gpu``            ``nvidia-smi`` CSV telemetry
``POST /v1/selftest``       Ollama/GPU/disk checks, optional tiny inference on ``model``
=========================  ==========================================================================

Residency operations (load/unload) are serialized by one lock: the resource manager on the
orchestrator decides *what* is resident (leases, ``large-model-224``); this daemon only executes it.
Inference traffic does not pass through the daemon - the model gateway talks to Ollama directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Body, FastAPI, Query, Request

from hermclaw.contracts.worker import ModelLoadRequest, ModelUnloadRequest
from hermclaw.core.logging import get_logger
from hermclaw.workers.auth import TokenFile
from hermclaw.workers.schemas import (
    GpuResponse,
    ModelLoadResult,
    ModelsResponse,
    ModelUnloadResult,
    SelftestCheck,
    SelftestRequest,
    SelftestResult,
)
from worker.common.errors import unavailable
from worker.common.gpu import GpuQueryResult, query_gpus
from worker.common.heartbeat import HeartbeatExtras
from worker.common.server import create_daemon_app
from worker.common.settings import WorkerDaemonSettings
from worker.common.state import DaemonState
from worker.common.system import disk_free_mb
from worker.model.ollama import OllamaClient, OllamaError, same_model

log = get_logger(__name__)

MIN_FREE_DISK_MB = 2048


@dataclass
class ModelService:
    settings: WorkerDaemonSettings
    state: DaemonState
    ollama: OllamaClient
    load_timeout_seconds: float = 900.0
    unload_timeout_seconds: float = 120.0
    poll_seconds: float = 0.5
    gpu_timeout_seconds: float = 10.0
    residency_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_gpu: GpuQueryResult | None = None

    async def gpu(self) -> GpuQueryResult:
        self.last_gpu = await query_gpus(self.settings.nvidia_smi, timeout_seconds=self.gpu_timeout_seconds)
        return self.last_gpu

    async def extras(self) -> HeartbeatExtras:
        versions: dict[str, str] = {}
        error: str | None = None
        loaded = []
        try:
            versions["ollama"] = await self.ollama.version()
            loaded = await self.ollama.ps()
        except OllamaError as exc:
            error = f"ollama unavailable: {exc.message}"
        gpu = await self.gpu()
        if gpu.driver:
            versions["nvidia_driver"] = gpu.driver
        return HeartbeatExtras(gpus=gpu.gpus, loaded_models=loaded, service_versions=versions, readiness_error=error)

    async def health_checks(self) -> dict[str, bool]:
        try:
            await self.ollama.version()
            ollama_ok = True
        except OllamaError:
            ollama_ok = False
        gpu = await self.gpu()
        return {"ollama": ollama_ok, "gpu": gpu.available}

    # ------------------------------------------------------------------ residency
    async def models(self) -> ModelsResponse:
        loaded, installed = await asyncio.gather(self.ollama.ps(), self.ollama.tags())
        return ModelsResponse(loaded=loaded, installed=installed)

    async def _unload_and_wait(self, model: str) -> None:
        await self.ollama.request_unload(model)
        if not await self.ollama.wait_unloaded(model, timeout_seconds=self.unload_timeout_seconds, poll_seconds=self.poll_seconds):
            raise OllamaError(
                f"model '{model}' still resident after {self.unload_timeout_seconds:.0f} s",
                code="MODEL_UNLOAD_TIMEOUT",
                status=504,
                details={"model": model},
            )

    async def load(self, req: ModelLoadRequest, *, exclusive: bool, keep: Sequence[str]) -> ModelLoadResult:
        if not self.state.accepting_work:
            raise unavailable("worker is draining", "WORKER_DRAINING")
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with self.residency_lock:
            with self.state.work(f"load:{req.model}", kind="model_load"):
                unloaded: list[str] = []
                if exclusive:
                    for m in await self.ollama.ps():
                        if same_model(m.name, req.model) or any(same_model(m.name, k) for k in keep):
                            continue
                        await self._unload_and_wait(m.name)
                        unloaded.append(m.name)
                await self.ollama.load(
                    req.model, num_ctx=req.context_tokens, keep_alive=req.keep_alive, timeout_seconds=self.load_timeout_seconds
                )
                info = await self.ollama.loaded(req.model)
        if info is None:
            raise OllamaError(f"model '{req.model}' is not resident after loading", code="MODEL_LOAD_FAILED", status=502)
        if info.context_length is not None and info.context_length < req.context_tokens:
            log.warning(
                "model loaded with a smaller context than requested",
                extra={"model": req.model, "requested": req.context_tokens, "actual": info.context_length},
            )
        log.info("model loaded", extra={"model": req.model, "num_ctx": req.context_tokens, "unloaded_others": unloaded})
        return ModelLoadResult(
            model=info.name,
            loaded=True,
            context_length=info.context_length,
            size_vram_bytes=info.size_vram_bytes,
            unloaded_others=unloaded,
            duration_ms=int((loop.time() - started) * 1000),
        )

    async def unload(self, req: ModelUnloadRequest) -> ModelUnloadResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with self.residency_lock:
            with self.state.work(f"unload:{req.model}", kind="model_unload"):
                current = await self.ollama.loaded(req.model)
                if current is None:
                    return ModelUnloadResult(model=req.model, unloaded=True, was_loaded=False, duration_ms=0)
                await self._unload_and_wait(current.name)
        log.info("model unloaded", extra={"model": req.model})
        return ModelUnloadResult(model=req.model, unloaded=True, was_loaded=True, duration_ms=int((loop.time() - started) * 1000))

    async def gpu_response(self) -> GpuResponse:
        gpu = await self.gpu()
        return GpuResponse(available=gpu.available, gpus=gpu.gpus, error=gpu.error)

    # ------------------------------------------------------------------ selftest
    async def selftest(self, req: SelftestRequest) -> SelftestResult:
        loop = asyncio.get_running_loop()
        checks: list[SelftestCheck] = []

        async def timed(name: str, coro: Any) -> bool:
            t0 = loop.time()
            try:
                ok, detail = await coro
            except OllamaError as exc:
                ok, detail = False, f"{exc.code}: {exc.message}"[:300]
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"[:300]
            checks.append(SelftestCheck(name=name, ok=ok, detail=detail, duration_ms=int((loop.time() - t0) * 1000)))
            return bool(ok)

        async def version() -> tuple[bool, str]:
            return True, await self.ollama.version()

        async def ps() -> tuple[bool, str]:
            loaded = await self.ollama.ps()
            return True, ", ".join(m.name for m in loaded) or "no model resident"

        async def gpu() -> tuple[bool, str]:
            g = await self.gpu()
            if not g.available:
                return False, g.error or "no GPU"
            return True, "; ".join(f"{x.index}: {x.name} {x.memory_used_mb}/{x.memory_total_mb} MiB" for x in g.gpus)

        async def disk() -> tuple[bool, str]:
            free = await asyncio.to_thread(disk_free_mb, self.settings.data_dir)
            return free >= MIN_FREE_DISK_MB, f"{free} MiB free"

        async def probe(model: str) -> tuple[bool, str]:
            async with self.residency_lock:
                with self.state.work(f"selftest:{model}", kind="selftest"):
                    stats = await self.ollama.probe(model, num_ctx=req.context_tokens, timeout_seconds=self.load_timeout_seconds)
            return stats["eval_count"] > 0, ", ".join(f"{k}={v}" for k, v in stats.items())

        reachable = await timed("ollama_version", version())
        if reachable:
            await timed("ollama_ps", ps())
        await timed("gpu", gpu())
        await timed("disk_free", disk())
        if req.model and reachable:
            await timed(f"inference:{req.model}", probe(req.model))
        return SelftestResult(ok=all(c.ok for c in checks), worker_id=self.settings.worker_id, checks=checks)


def _service(request: Request) -> ModelService:
    svc = request.app.state.model
    assert isinstance(svc, ModelService)
    return svc


def create_app(
    settings: WorkerDaemonSettings,
    *,
    ollama_transport: httpx.AsyncBaseTransport | None = None,
    token_file: TokenFile | None = None,
    heartbeat: bool = True,
    heartbeat_transport: httpx.AsyncBaseTransport | None = None,
    extra_routers: Sequence[APIRouter] = (),
    comfyui_transport: httpx.AsyncBaseTransport | None = None,
    load_timeout_seconds: float = 900.0,
    unload_timeout_seconds: float = 120.0,
    poll_seconds: float = 0.5,
) -> FastAPI:
    state = DaemonState(settings.worker_id, settings.kind)
    svc = ModelService(
        settings=settings,
        state=state,
        ollama=OllamaClient(settings.ollama_url, transport=ollama_transport),
        load_timeout_seconds=load_timeout_seconds,
        unload_timeout_seconds=unload_timeout_seconds,
        poll_seconds=poll_seconds,
    )
    app = create_daemon_app(
        settings,
        title="Hermclaw model worker",
        token_file=token_file,
        state=state,
        health_checks=svc.health_checks,
        extras=svc.extras,
        heartbeat=heartbeat,
        heartbeat_transport=heartbeat_transport,
        on_shutdown=(svc.ollama.aclose,),
    )
    app.state.model = svc

    @app.get("/v1/models", response_model=ModelsResponse)
    async def models(request: Request) -> ModelsResponse:
        return await _service(request).models()

    @app.post("/v1/models/load", response_model=ModelLoadResult)
    async def load_model(
        request: Request,
        body: Annotated[ModelLoadRequest, Body()],
        exclusive: bool = False,
        keep: Annotated[list[str] | None, Query()] = None,
    ) -> ModelLoadResult:
        return await _service(request).load(body, exclusive=exclusive, keep=keep or [])

    @app.post("/v1/models/unload", response_model=ModelUnloadResult)
    async def unload_model(request: Request, body: Annotated[ModelUnloadRequest, Body()]) -> ModelUnloadResult:
        return await _service(request).unload(body)

    @app.get("/v1/gpu", response_model=GpuResponse)
    async def gpu(request: Request) -> GpuResponse:
        return await _service(request).gpu_response()

    @app.post("/v1/selftest", response_model=SelftestResult)
    async def selftest(request: Request, body: Annotated[SelftestRequest | None, Body()] = None) -> SelftestResult:
        return await _service(request).selftest(body or SelftestRequest())

    # ------------------------------------------------------------------------------------------------
    # MEDIA EXTENSION POINT (P29, DECISIONS D-007)
    # The media phase adds its image/video endpoints (ComfyUI / ffmpeg adapters, MediaJobRequest ->
    # MediaJobResult) as APIRouters passed via ``extra_routers``. They are mounted here, behind the same
    # signed-request middleware and error handlers; handlers reach the daemon state through
    # ``request.app.state.daemon`` (``worker.common.server.DaemonContext``) and must wrap long jobs in
    # ``state.work(...)`` so heartbeats report ``busy``. Their capability names are announced via
    # ``WORKER_CAPABILITIES`` (e.g. ``image,video``). GPU residency must go through ``svc.residency_lock``.
    # ------------------------------------------------------------------------------------------------
    if settings.media_enabled:  # P29: image/video jobs share this daemon's GPU residency lock
        from worker.media.backends import ComfyUIBackend, FfmpegBackend
        from worker.media.service import MediaService, create_media_router

        app.state.media = MediaService(
            root=settings.data_dir / "media",
            state=state,
            ffmpeg=FfmpegBackend(settings.ffmpeg, settings.ffprobe),
            comfyui=ComfyUIBackend(settings.comfyui_url, workflows_dir=settings.comfyui_workflows_dir, transport=comfyui_transport)
            if settings.comfyui_url
            else None,
            model=svc,
        )
        app.include_router(create_media_router())
    for router in extra_routers:
        app.include_router(router)

    return app
