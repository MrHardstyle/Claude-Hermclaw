"""Media service + router mounted into the model worker daemon on ``.224`` (Bauplan §32, Phase 29).

Endpoints (behind the daemon's signed-request middleware):
  PUT    /v1/media/{job}/inputs/{name}      upload an input file (raw bytes)
  POST   /v1/media/jobs                     run a MediaJobRequest → MediaJobResult (artifacts with sha256)
  GET    /v1/media/{job}/{request}/{name}   download an output artifact
  DELETE /v1/media/{job}                    remove all media files of a job

GPU discipline: a media job unloads every Ollama model first (``unload_models``, default true – the orchestrator
already drained AI work through the GPU lease) and then runs under the daemon's ``residency_lock`` so no model can
be loaded while the GPU renders.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import FileResponse

from hermclaw.contracts.worker import MediaJobRequest, MediaJobResult, ModelUnloadRequest
from hermclaw.core.logging import get_logger
from worker.common.errors import DaemonError, bad_request, not_found
from worker.common.state import DaemonState
from worker.media.backends import ComfyUIBackend, FfmpegBackend, MediaBackend, MediaContext, media_type, safe_name

if TYPE_CHECKING:
    from worker.model.app import ModelService

log = get_logger(__name__)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_INPUT_BYTES = 4 * 1024**3


def _id(value: str, what: str) -> str:
    if not _ID.fullmatch(value or ""):
        raise bad_request(f"invalid {what}", "MEDIA_ID_INVALID", field=what)
    return value


@dataclass
class MediaService:
    root: Path
    state: DaemonState
    ffmpeg: FfmpegBackend = field(default_factory=FfmpegBackend)
    comfyui: ComfyUIBackend | None = None
    model: ModelService | None = None
    max_input_bytes: int = MAX_INPUT_BYTES
    _job_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def _job_dir(self, job: str) -> Path:
        return self.root / _id(job, "job_id")

    async def put_input(self, job: str, name: str, data: bytes) -> dict[str, Any]:
        safe_name(name, field="input")
        if len(data) > self.max_input_bytes:
            raise bad_request("input too large", "MEDIA_INPUT_TOO_LARGE", size=len(data))
        target = self._job_dir(job) / "inputs" / name
        tmp = target.with_name(f".{name}.part")

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            tmp.replace(target)

        await asyncio.to_thread(_write)
        return {"name": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def _backend(self, req: MediaJobRequest) -> MediaBackend:
        if req.backend == "ffmpeg":
            return self.ffmpeg
        if self.comfyui is None:
            raise DaemonError("ComfyUI backend is not configured on this worker", code="COMFYUI_NOT_CONFIGURED", status=503)
        return self.comfyui

    async def _unload_all(self) -> list[str]:
        if self.model is None:
            return []
        names = [m.name for m in await self.model.ollama.ps()]
        for name in names:
            await self.model.unload(ModelUnloadRequest(model=name))
        return names

    async def run(self, req: MediaJobRequest) -> MediaJobResult:
        job, rid = _id(req.job_id, "job_id"), _id(req.request_id, "request_id")
        backend = self._backend(req)
        out_dir = self._job_dir(job) / rid
        in_dir = self._job_dir(job) / "inputs"
        lock = self._job_locks.setdefault(job, asyncio.Lock())
        started = time.monotonic()
        async with lock:
            if out_dir.exists():
                raise DaemonError(f"request {rid} already exists for job {job}", code="MEDIA_REQUEST_EXISTS", status=409)
            await asyncio.to_thread(out_dir.mkdir, parents=True)
            await asyncio.to_thread(in_dir.mkdir, parents=True, exist_ok=True)
            unloaded = await self._unload_all() if req.params.get("unload_models", True) else []
            ctx = MediaContext(
                request_id=rid, params=dict(req.params), in_dir=in_dir, out_dir=out_dir, timeout_seconds=float(req.timeout_seconds)
            )
            residency = self.model.residency_lock if self.model is not None else asyncio.Lock()
            try:
                async with residency:
                    with self.state.work(f"media:{rid}", kind=req.kind, job_id=req.job_id, step_id=req.step_id):
                        outputs = await backend.run(ctx)
            except DaemonError as exc:
                log.warning("media job failed", extra={"request_id": rid, "code": exc.code})
                return MediaJobResult(
                    request_id=rid,
                    ok=False,
                    error=f"{exc.code}: {exc.message}"[:2000],
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
        artifacts = []
        for path in outputs:
            data = await asyncio.to_thread(path.read_bytes)
            artifacts.append(
                {
                    "name": path.name,
                    "path": f"{job}/{rid}/{path.name}",
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "media_type": media_type(path),
                    "kind": req.kind,
                    "backend": req.backend,
                }
            )
        log.info("media job finished", extra={"request_id": rid, "artifacts": len(artifacts), "unloaded_models": unloaded})
        return MediaJobResult(request_id=rid, ok=True, artifacts=artifacts, duration_ms=int((time.monotonic() - started) * 1000))

    def file(self, job: str, rid: str, name: str) -> Path:
        path = self._job_dir(job) / _id(rid, "request_id") / safe_name(name)
        if not path.is_file() or path.is_symlink():
            raise not_found("media file not found", "MEDIA_FILE_NOT_FOUND")
        return path

    async def delete_job(self, job: str) -> dict[str, Any]:
        path = self._job_dir(job)
        existed = path.is_dir()
        if existed:
            await asyncio.to_thread(shutil.rmtree, path)
        self._job_locks.pop(job, None)
        return {"job_id": job, "deleted": existed}


def _svc(request: Request) -> MediaService:
    svc = request.app.state.media
    assert isinstance(svc, MediaService)
    return svc


def create_media_router() -> APIRouter:
    router = APIRouter(prefix="/v1/media", tags=["media"])

    @router.put("/{job}/inputs/{name}")
    async def put_input(job: str, name: str, request: Request) -> dict[str, Any]:
        return await _svc(request).put_input(job, name, await request.body())

    @router.post("/jobs", response_model=MediaJobResult)
    async def run_job(request: Request, body: Annotated[MediaJobRequest, Body()]) -> MediaJobResult:
        return await _svc(request).run(body)

    @router.get("/{job}/{rid}/{name}")
    async def get_file(job: str, rid: str, name: str, request: Request) -> Response:
        path = _svc(request).file(job, rid, name)
        return FileResponse(path, media_type=media_type(path), filename=path.name)

    @router.delete("/{job}")
    async def delete_job(job: str, request: Request) -> dict[str, Any]:
        return await _svc(request).delete_job(job)

    return router
