"""Media backends of the model/media worker ``.224`` (Bauplan §32, DECISIONS D-007).

``FfmpegBackend``  – declarative video operations (transcode, thumbnail, concat, slideshow, probe). Arguments are
                     built from validated parameters only (no shell, no free-form ffmpeg flags); Pascal NVENC
                     (``h264_nvenc``/``hevc_nvenc``) or CPU codecs.
``ComfyUIBackend`` – image workflows through the ComfyUI HTTP API (``/prompt`` → ``/history`` → ``/view``).

Inputs live in ``<media_root>/<job>/inputs``, outputs in ``<media_root>/<job>/<request>/``. Every name is a plain
file name (no paths); every path is resolved inside its directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from worker.common.errors import DaemonError, bad_request, unavailable

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VIDEO_CODECS = frozenset({"h264_nvenc", "hevc_nvenc", "libx264", "libx265", "libvpx-vp9", "copy"})
AUDIO_CODECS = frozenset({"aac", "libopus", "copy", "none"})
PRESETS = frozenset({"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "p1", "p2", "p3", "p4", "p5", "p6", "p7"})
VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MEDIA_TYPES = {**VIDEO_TYPES, **IMAGE_TYPES, ".json": "application/json", ".txt": "text/plain"}
_SCALE = re.compile(r"^(\d{2,5}|-1|-2):(\d{2,5}|-1|-2)$")
STDERR_TAIL = 4000


def safe_name(name: str, *, field: str = "name", suffixes: Mapping[str, str] | None = None) -> str:
    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name) or ".." in name:
        raise bad_request(f"invalid {field} {name!r}: plain file name expected", "MEDIA_NAME_INVALID", field=field)
    if suffixes is not None and Path(name).suffix.lower() not in suffixes:
        raise bad_request(f"{field} {name!r} must end with one of {sorted(suffixes)}", "MEDIA_TYPE_UNSUPPORTED", field=field)
    return name


def media_type(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


@dataclass(frozen=True)
class MediaContext:
    request_id: str
    params: dict[str, Any]
    in_dir: Path
    out_dir: Path
    timeout_seconds: float


class MediaBackend(Protocol):
    async def run(self, ctx: MediaContext) -> list[Path]: ...


def _num(params: Mapping[str, Any], key: str, lo: float, hi: float, default: float | None = None) -> float | None:
    value = params.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not lo <= float(value) <= hi:
        raise bad_request(f"parameter {key} must be a number in [{lo}, {hi}]", "MEDIA_PARAM_INVALID", param=key)
    return float(value)


def _choice(params: Mapping[str, Any], key: str, allowed: frozenset[str], default: str) -> str:
    value = params.get(key, default)
    if value not in allowed:
        raise bad_request(f"parameter {key} must be one of {sorted(allowed)}", "MEDIA_PARAM_INVALID", param=key)
    return str(value)


def _input(ctx: MediaContext, name: Any, field: str = "input") -> Path:
    path = ctx.in_dir / safe_name(str(name), field=field)
    if not path.is_file() or path.is_symlink():
        raise bad_request(f"{field} {name!r} was not uploaded for this job", "MEDIA_INPUT_MISSING", field=field)
    return path


def _quote(path: Path) -> str:
    """concat-demuxer quoting; the paths are runtime-built (validated names inside the job directory)."""
    return "'" + str(path).replace("'", "'\\''") + "'"


def _output(ctx: MediaContext, name: Any, suffixes: Mapping[str, str]) -> Path:
    return ctx.out_dir / safe_name(str(name), field="output", suffixes=suffixes)


class FfmpegBackend:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    # ------------------------------------------------------------------ argv builders (pure, unit-testable)
    def plan(self, ctx: MediaContext) -> tuple[list[list[str]], list[Path]]:
        p = ctx.params
        op = p.get("operation")
        base = [self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
        if op == "transcode":
            src, out = _input(ctx, p.get("input")), _output(ctx, p.get("output", "output.mp4"), VIDEO_TYPES)
            argv = [*base]
            start = _num(p, "start_seconds", 0, 86_400)
            if start is not None:
                argv += ["-ss", f"{start:.3f}"]
            argv += ["-i", str(src)]
            duration = _num(p, "duration_seconds", 0.04, 86_400)
            if duration is not None:
                argv += ["-t", f"{duration:.3f}"]
            vcodec = _choice(p, "video_codec", VIDEO_CODECS, "libx264")
            argv += ["-c:v", vcodec]
            if vcodec != "copy":
                filters = []
                scale = p.get("scale")
                if scale is not None:
                    if not isinstance(scale, str) or not _SCALE.fullmatch(scale):
                        raise bad_request("parameter scale must look like 1280:-2", "MEDIA_PARAM_INVALID", param="scale")
                    filters.append(f"scale={scale}")
                fps = _num(p, "fps", 1, 120)
                if fps is not None:
                    filters.append(f"fps={fps:g}")
                if filters:
                    argv += ["-vf", ",".join(filters)]
                crf = _num(p, "crf", 0, 51)
                if crf is not None:
                    argv += ["-cq", f"{int(crf)}"] if vcodec.endswith("_nvenc") else ["-crf", f"{int(crf)}"]
                argv += ["-preset", _choice(p, "preset", PRESETS, "p4" if vcodec.endswith("_nvenc") else "medium")]
                if vcodec in ("libx264", "h264_nvenc"):
                    argv += ["-pix_fmt", "yuv420p"]
            acodec = _choice(p, "audio_codec", AUDIO_CODECS, "aac")
            argv += ["-an"] if acodec == "none" else ["-c:a", acodec]
            return [[*argv, str(out)]], [out]
        if op == "thumbnail":
            src, out = _input(ctx, p.get("input")), _output(ctx, p.get("output", "thumbnail.png"), IMAGE_TYPES)
            at = _num(p, "at_seconds", 0, 86_400, 0.0) or 0.0
            width = _num(p, "width", 16, 7680, 640)
            return [[*base, "-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1", "-vf", f"scale={int(width or 640)}:-2", str(out)]], [out]
        if op == "concat":
            inputs = p.get("inputs")
            if not isinstance(inputs, list) or not 2 <= len(inputs) <= 200:
                raise bad_request("concat needs 2..200 inputs", "MEDIA_PARAM_INVALID", param="inputs")
            paths = [_input(ctx, n, "inputs") for n in inputs]
            out = _output(ctx, p.get("output", "concat.mp4"), VIDEO_TYPES)
            listing = ctx.out_dir / f".concat-{uuid.uuid4().hex}.txt"
            listing.write_text("".join(f"file {_quote(x)}\n" for x in paths), encoding="utf-8")
            return [[*base, "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)]], [out]
        if op == "slideshow":
            images = p.get("images")
            if not isinstance(images, list) or not 1 <= len(images) <= 500:
                raise bad_request("slideshow needs 1..500 images", "MEDIA_PARAM_INVALID", param="images")
            paths = [_input(ctx, n, "images") for n in images]
            per = _num(p, "seconds_per_image", 0.1, 600, 2.0) or 2.0
            fps = _num(p, "fps", 1, 60, 25.0) or 25.0
            out = _output(ctx, p.get("output", "slideshow.mp4"), VIDEO_TYPES)
            listing = ctx.out_dir / f".slides-{uuid.uuid4().hex}.txt"
            lines = "".join(f"file {_quote(x)}\nduration {per:.3f}\n" for x in paths) + f"file {_quote(paths[-1])}\n"
            listing.write_text(lines, encoding="utf-8")
            vcodec = _choice(p, "video_codec", VIDEO_CODECS - {"copy"}, "libx264")
            argv = [
                *base,
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-vf",
                f"fps={fps:g},scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v",
                vcodec,
            ]
            if vcodec in ("libx264", "h264_nvenc"):
                argv += ["-pix_fmt", "yuv420p"]
            return [[*argv, str(out)]], [out]
        if op == "probe":
            src = _input(ctx, p.get("input"))
            out = ctx.out_dir / "probe.json"
            return [[self.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(src)]], [out]
        raise bad_request(f"unknown ffmpeg operation {op!r}", "MEDIA_OPERATION_UNKNOWN", operation=str(op)[:50])

    async def run(self, ctx: MediaContext) -> list[Path]:
        commands, outputs = self.plan(ctx)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ctx.timeout_seconds
        for argv in commands:
            stdout = await _run_process(argv, max(1.0, deadline - loop.time()))
            if argv[0] == self.ffprobe:
                outputs[0].write_bytes(stdout)
        for listing in ctx.out_dir.glob(".concat-*.txt"):
            listing.unlink(missing_ok=True)
        for listing in ctx.out_dir.glob(".slides-*.txt"):
            listing.unlink(missing_ok=True)
        missing = [o.name for o in outputs if not o.is_file()]
        if missing:
            raise DaemonError(f"ffmpeg produced no output {missing}", code="MEDIA_NO_OUTPUT", status=500)
        return outputs


async def _run_process(argv: list[str], limit_seconds: float) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True
        )
    except FileNotFoundError as exc:
        raise unavailable(f"media tool not installed: {argv[0]}", "MEDIA_TOOL_MISSING", tool=argv[0]) from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=limit_seconds)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise DaemonError(f"{Path(argv[0]).name} exceeded {limit_seconds:.0f}s", code="MEDIA_TIMEOUT", status=504) from None
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace")[-STDERR_TAIL:]
        raise DaemonError(f"{Path(argv[0]).name} failed (exit {proc.returncode}): {tail}", code="MEDIA_TOOL_FAILED", status=422)
    return out


class ComfyUIBackend:
    """ComfyUI API client. ``params``: ``workflow`` (API-format dict) or ``workflow_name`` (file in ``workflows_dir``),
    optional ``set`` overrides ``{"<node>.inputs.<key>": value}`` (scalars, existing nodes only)."""

    def __init__(
        self,
        base_url: str,
        *,
        workflows_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workflows_dir = workflows_dir
        self.transport = transport
        self.poll_seconds = poll_seconds

    def workflow(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(params.get("workflow"), dict):
            wf = json.loads(json.dumps(params["workflow"]))
        elif params.get("workflow_name"):
            if self.workflows_dir is None:
                raise bad_request("no workflows directory configured", "MEDIA_WORKFLOW_MISSING")
            path = self.workflows_dir / safe_name(str(params["workflow_name"]), field="workflow_name", suffixes={".json": ""})
            if not path.is_file():
                raise bad_request(f"unknown workflow {params['workflow_name']!r}", "MEDIA_WORKFLOW_MISSING")
            wf = json.loads(path.read_text(encoding="utf-8"))
        else:
            raise bad_request("comfyui job needs workflow or workflow_name", "MEDIA_WORKFLOW_MISSING")
        if not isinstance(wf, dict) or not wf:
            raise bad_request("workflow must be a non-empty API-format object", "MEDIA_WORKFLOW_INVALID")
        for key, value in (params.get("set") or {}).items():
            parts = str(key).split(".")
            if len(parts) != 3 or parts[1] != "inputs" or parts[0] not in wf or not isinstance(value, str | int | float | bool):
                raise bad_request(f"invalid override {key!r}", "MEDIA_WORKFLOW_INVALID", key=str(key)[:100])
            wf[parts[0]].setdefault("inputs", {})[parts[2]] = value
        return wf

    async def run(self, ctx: MediaContext) -> list[Path]:
        wf = self.workflow(ctx.params)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ctx.timeout_seconds
        async with httpx.AsyncClient(base_url=self.base_url, transport=self.transport, timeout=30.0) as client:
            try:
                resp = await client.post("/prompt", json={"prompt": wf, "client_id": ctx.request_id})
            except httpx.HTTPError as exc:
                raise unavailable(f"ComfyUI unreachable: {type(exc).__name__}", "COMFYUI_UNAVAILABLE") from exc
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code != 200 or body.get("node_errors") or "prompt_id" not in body:
                detail = json.dumps(body.get("node_errors") or body.get("error") or resp.text[:500])[:1500]
                raise DaemonError(f"ComfyUI rejected the workflow: {detail}", code="COMFYUI_REJECTED", status=422)
            prompt_id = str(body["prompt_id"])
            entry: dict[str, Any] | None = None
            while entry is None:
                if loop.time() > deadline:
                    with contextlib.suppress(httpx.HTTPError):
                        await client.post("/interrupt")
                    raise DaemonError("ComfyUI workflow timed out", code="MEDIA_TIMEOUT", status=504)
                hist = (await client.get(f"/history/{prompt_id}")).json()
                entry = hist.get(prompt_id) if isinstance(hist, dict) else None
                if entry is None:
                    await asyncio.sleep(self.poll_seconds)
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                raise DaemonError(
                    f"ComfyUI workflow failed: {json.dumps(status.get('messages', []))[:1500]}", code="COMFYUI_FAILED", status=422
                )
            outputs: list[Path] = []
            for node_out in (entry.get("outputs") or {}).values():
                for img in node_out.get("images", []) or []:
                    fname = Path(str(img.get("filename", ""))).name
                    target = ctx.out_dir / safe_name(fname, field="comfy output", suffixes=IMAGE_TYPES)
                    view = await client.get(
                        "/view",
                        params={"filename": img.get("filename"), "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")},
                    )
                    if view.status_code != 200:
                        raise DaemonError(f"ComfyUI /view failed for {fname}", code="COMFYUI_FAILED", status=502)
                    target.write_bytes(view.content)
                    outputs.append(target)
        if not outputs:
            raise DaemonError("ComfyUI workflow produced no images", code="MEDIA_NO_OUTPUT", status=500)
        return outputs
