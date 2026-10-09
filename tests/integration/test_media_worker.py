"""P29 media worker (``.224``): real ffmpeg/ffprobe, ComfyUI API via a test-only fake, mounted in the real model
daemon behind signed requests, models unloaded before GPU work."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from hermclaw.contracts.common import WorkerKind
from hermclaw.contracts.worker import MediaJobRequest, ModelLoadRequest
from hermclaw.workers.client import MediaWorkerClient
from hermclaw.workers.errors import WorkerRemoteError
from tests.integration.test_workers_support import TOKEN, FakeOllama, lifespan, make_settings
from tests.unit.test_workers_daemon_common import write_fake_nvidia_smi
from worker.common.errors import DaemonError
from worker.media.backends import FfmpegBackend, MediaContext
from worker.model.app import create_app

pytestmark = [pytest.mark.integration, pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")]
WORKER = "media-test-224"


def _video(path: Path, seconds: int = 2, color: str = "testsrc") -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"{color}=duration={seconds}:size=320x240:rate=25",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path


class FakeComfy:
    """Minimal ComfyUI API (/prompt, /history, /view) as an httpx MockTransport."""

    PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )

    def __init__(self, *, reject: bool = False) -> None:
        self.reject, self.prompts, self.polls = reject, [], 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            body = json.loads(request.content)
            self.prompts.append(body)
            if self.reject:
                return httpx.Response(400, json={"error": "invalid prompt", "node_errors": {"3": "missing ckpt"}})
            return httpx.Response(200, json={"prompt_id": "p-1", "number": 1, "node_errors": {}})
        if request.url.path == "/history/p-1":
            self.polls += 1
            if self.polls < 2:
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "p-1": {
                        "status": {"status_str": "success"},
                        "outputs": {"9": {"images": [{"filename": "hc_00001_.png", "subfolder": "", "type": "output"}]}},
                    }
                },
            )
        if request.url.path == "/view":
            assert request.url.params["filename"] == "hc_00001_.png"
            return httpx.Response(200, content=self.PNG, headers={"content-type": "image/png"})
        return httpx.Response(404)


@pytest.fixture
async def ollama() -> AsyncIterator[FakeOllama]:
    fake = await FakeOllama().start()
    yield fake
    await fake.stop()


def _app(tmp_path: Path, ollama: FakeOllama, comfy: FakeComfy | None = None) -> FastAPI:
    smi_dir = tmp_path / "bin"
    smi_dir.mkdir(exist_ok=True)
    settings = make_settings(
        tmp_path,
        WorkerKind.model,
        WORKER,
        ollama_url=ollama.url,
        nvidia_smi=str(write_fake_nvidia_smi(smi_dir)),
        capabilities=("chat", "image", "video"),
        comfyui_url="http://comfy" if comfy else None,
    )
    return create_app(settings, heartbeat=False, poll_seconds=0.01, comfyui_transport=httpx.MockTransport(comfy.handler) if comfy else None)


def _client(app: FastAPI) -> MediaWorkerClient:
    return MediaWorkerClient("http://media", worker_id=WORKER, token=TOKEN, transport=httpx.ASGITransport(app=app), get_retries=0)


def _req(kind: str, backend: str, **params: Any) -> MediaJobRequest:
    return MediaJobRequest(
        request_id=f"r-{uuid.uuid4().hex[:12]}",
        job_id="job-1",
        step_id="S001",
        kind=kind,
        backend=backend,
        params=params,
        timeout_seconds=120,
    )  # type: ignore[arg-type]


async def test_video_transcode_thumbnail_probe_concat_unloads_models_first(tmp_path: Path, ollama: FakeOllama) -> None:
    app = _app(tmp_path, ollama)
    src = _video(tmp_path / "in.mp4")
    async with lifespan(app), _client(app) as c:
        await c.load_model(ModelLoadRequest(model="qwen3:8b", context_tokens=4096))
        assert [m.name for m in await c.loaded_models()] == ["qwen3:8b"]
        up = await c.put_media_input("job-1", "in.mp4", src.read_bytes())
        assert up["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
        res = await c.run_media_job(
            _req(
                "video",
                "ffmpeg",
                operation="transcode",
                input="in.mp4",
                output="out.webm",
                video_codec="libvpx-vp9",
                audio_codec="none",
                scale="160:-2",
                crf=40,
            )
        )
        assert res.ok, res.error
        assert await c.loaded_models() == [], "models must be unloaded before GPU media work"
        art = res.artifacts[0]
        assert art["name"] == "out.webm" and art["media_type"] == "video/webm" and art["size_bytes"] > 0
        data = await c.download_media_file("job-1", res.request_id, "out.webm")
        assert hashlib.sha256(data).hexdigest() == art["sha256"]
        thumb = await c.run_media_job(
            _req("image", "ffmpeg", operation="thumbnail", input="in.mp4", at_seconds=1, width=64, unload_models=False)
        )
        assert thumb.ok and thumb.artifacts[0]["media_type"] == "image/png"
        probe = await c.run_media_job(_req("video", "ffmpeg", operation="probe", input="in.mp4"))
        meta = json.loads(await c.download_media_file("job-1", probe.request_id, "probe.json"))
        assert meta["streams"][0]["width"] == 320 and abs(float(meta["format"]["duration"]) - 2.0) < 0.2
        await c.put_media_input("job-1", "b.mp4", _video(tmp_path / "b.mp4", 1, "smptebars").read_bytes())
        cat = await c.run_media_job(_req("video", "ffmpeg", operation="concat", inputs=["in.mp4", "b.mp4"], output="all.mp4"))
        assert cat.ok, cat.error
        cat_path = tmp_path / "all.mp4"
        cat_path.write_bytes(await c.download_media_file("job-1", cat.request_id, "all.mp4"))
        dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(cat_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert abs(float(dur.stdout) - 3.0) < 0.3
        assert (await c.delete_media_job("job-1"))["deleted"] is True


async def test_comfyui_workflow_with_overrides_and_rejection(tmp_path: Path, ollama: FakeOllama) -> None:
    comfy = FakeComfy()
    app = _app(tmp_path, ollama, comfy)
    wf = {
        "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}},
        "9": {"class_type": "SaveImage", "inputs": {}},
    }
    async with lifespan(app), _client(app) as c:
        res = await c.run_media_job(_req("image", "comfyui", workflow=wf, set={"6.inputs.text": "a red bicycle", "3.inputs.seed": 42}))
        assert res.ok, res.error
        sent = comfy.prompts[0]["prompt"]
        assert sent["6"]["inputs"]["text"] == "a red bicycle" and sent["3"]["inputs"]["seed"] == 42
        assert res.artifacts[0]["name"] == "hc_00001_.png" and res.artifacts[0]["media_type"] == "image/png"
        bad = await c.run_media_job(_req("image", "comfyui", workflow=wf, set={"99.inputs.text": "unknown node"}))
        assert not bad.ok and "MEDIA_WORKFLOW_INVALID" in (bad.error or "")
    rejecting = FakeComfy(reject=True)
    (tmp_path / "second").mkdir()
    app2 = _app(tmp_path / "second", ollama, rejecting)
    async with lifespan(app2), _client(app2) as c2:
        res2 = await c2.run_media_job(_req("image", "comfyui", workflow=wf))
        assert not res2.ok and "COMFYUI_REJECTED" in (res2.error or "") and "missing ckpt" in (res2.error or "")


async def test_input_validation_and_path_safety(tmp_path: Path, ollama: FakeOllama) -> None:
    app = _app(tmp_path, ollama)
    async with lifespan(app), _client(app) as c:
        with pytest.raises(ValueError):
            await c.put_media_input("job-1", "../evil", b"x")
        with pytest.raises(ValueError):  # the client refuses it before sending
            await c.put_media_input("job-1", ".hidden", b"x")
        with pytest.raises(DaemonError):  # and the daemon refuses it independently
            await app.state.media.put_input("job-1", ".hidden", b"x")
        with pytest.raises(DaemonError):
            await app.state.media.put_input("../job", "a.mp4", b"x")
        res = await c.run_media_job(_req("video", "ffmpeg", operation="transcode", input="missing.mp4"))
        assert not res.ok and "MEDIA_INPUT_MISSING" in (res.error or "")
        res = await c.run_media_job(_req("video", "ffmpeg", operation="transcode", input="x.mp4", video_codec="rm -rf /"))
        assert not res.ok
        res = await c.run_media_job(_req("video", "ffmpeg", operation="shell", input="x.mp4"))
        assert not res.ok and "MEDIA_OPERATION_UNKNOWN" in (res.error or "")
        with pytest.raises(WorkerRemoteError) as err:  # no ComfyUI configured on this worker -> 503
            await c.run_media_job(_req("image", "comfyui", workflow={"1": {}}))
        assert err.value.remote_code == "COMFYUI_NOT_CONFIGURED"


async def test_ffmpeg_argv_is_declarative_and_nvenc_aware(tmp_path: Path) -> None:
    ind, outd = tmp_path / "in", tmp_path / "out"
    ind.mkdir()
    outd.mkdir()
    (ind / "a.mp4").write_bytes(b"x")
    ctx = MediaContext(
        "r1", {"operation": "transcode", "input": "a.mp4", "output": "b.mp4", "video_codec": "h264_nvenc", "crf": 23}, ind, outd, 10
    )
    (argv,), _ = FfmpegBackend().plan(ctx)
    assert "-c:v" in argv and argv[argv.index("-c:v") + 1] == "h264_nvenc" and "-cq" in argv and "-crf" not in argv
    assert argv[-1] == str(outd / "b.mp4") and all(";" not in a and "|" not in a for a in argv)
    with pytest.raises(DaemonError):
        FfmpegBackend().plan(MediaContext("r2", {"operation": "transcode", "input": "a.mp4", "scale": "1;rm"}, ind, outd, 10))
    with pytest.raises(DaemonError):
        FfmpegBackend().plan(MediaContext("r3", {"operation": "transcode", "input": "a.mp4", "output": "../x.mp4"}, ind, outd, 10))
    slow = MediaContext("r4", {"operation": "probe", "input": "a.mp4"}, ind, outd, 10)
    with pytest.raises(DaemonError):  # not a real video -> ffprobe fails with a clear error
        await FfmpegBackend().run(slow)
    await asyncio.sleep(0)
