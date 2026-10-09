"""Media step handler (Bauplan §32, Phase 29): image/video steps on the model/media worker ``.224``.

1. media spec: the fast router turns the step goal + the available input files into a validated ``MediaSpec``
   (backend, operation, inputs, parameters) – a translation, no task-specific rules in the runtime
2. GPU lease via the resource manager (video priority 100, image 90): lower-priority AI holders are asked to yield
   (they checkpoint), the AI model resource groups are drained and held for the duration
3. inputs uploaded, job run on the worker (the worker unloads every Ollama model first and renders under its
   residency lock), artifacts downloaded, sha256-checked and registered (``artifacts`` rows, kind image/video)
4. lease released → drained AI steps resume from their checkpoints
5. artifact acceptance evidence checked deterministically; worker-side parameter errors become a bounded
   correction attempt carrying the error as evidence
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.acceptance import ArtifactEvidence
from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import MediaJobRequest
from hermclaw.core.config import HermclawConfig
from hermclaw.core.errors import HermclawError, ModelError
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.core.settings import get_settings
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext, ChatMessage, ChatModel
from hermclaw.persistence.models import Artifact, Step, StepAttempt
from hermclaw.runtime.transitions import emit_status
from hermclaw.scheduler.handlers import StepOutcome, StepRunContext

if TYPE_CHECKING:
    from hermclaw.gitops.engine import GitEngine
    from hermclaw.resources.manager import ResourceManager
    from hermclaw.workers.client import MediaWorkerClient

log = get_logger(__name__)
MEDIA_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".wav", ".mp3", ".json")
FFMPEG_OPERATIONS = ("transcode", "thumbnail", "concat", "slideshow", "probe")
SPEC_GUIDE = (
    "ffmpeg operations and parameters:\n"
    "- transcode: input, output (*.mp4|*.webm|*.mkv|*.mov), video_codec (h264_nvenc|hevc_nvenc|libx264|libx265|libvpx-vp9|copy), "
    "audio_codec (aac|libopus|copy|none), crf 0-51, preset, scale 'W:H' (-2 keeps aspect), fps, start_seconds, duration_seconds\n"
    "- thumbnail: input, output (*.png|*.jpg|*.webp), at_seconds, width\n"
    "- concat: inputs [..], output\n- slideshow: images [..], seconds_per_image, fps, output, video_codec\n- probe: input\n"
    "comfyui: workflow_name (a configured workflow file) and set {'<node>.inputs.<key>': value} overrides.\n"
    "Use only file names from the available inputs. Prefer h264_nvenc for H.264 on the GTX 1080."
)


class MediaSpec(BaseModel):
    backend: Literal["ffmpeg", "comfyui"]
    operation: str | None = Field(default=None, description="ffmpeg operation")
    inputs: list[str] = Field(default_factory=list, max_length=500, description="paths/names from the available inputs")
    params: dict[str, Any] = Field(default_factory=dict)


ClientFactory = Callable[[], Awaitable["MediaWorkerClient"]]


@dataclass
class MediaSettings:
    spec_alias: str = "fast-router"
    lease_wait_seconds: float = 6 * 3600.0
    lease_ttl_seconds: float = 900.0
    job_timeout_seconds: int = 3 * 3600
    max_inputs: int = 200
    max_input_bytes: int = 4 * 1024**3


class MediaStepHandler:
    kinds = frozenset({"image", "video"})

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig,
        *,
        chat: ChatModel,
        resources: ResourceManager,
        client_factory: ClientFactory,
        git: GitEngine | None = None,
        settings: MediaSettings | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        self.sm = sessionmaker
        self.config = config
        self.chat = chat
        self.resources = resources
        self.client_factory = client_factory
        self.git = git
        self.settings = settings or MediaSettings()
        self.artifacts_dir = artifacts_dir

    # ------------------------------------------------------------------ inputs
    async def _available_inputs(self, job_id: uuid.UUID) -> dict[str, Path]:
        """name/path -> local file: media files of the job workspace and artifacts of earlier steps."""
        out: dict[str, Path] = {}
        if self.git is not None:
            for ws in await self.git.list_workspaces(job_id):
                if ws.status in ("archived", "cleaned", "removed", "failed"):
                    continue
                root = Path(ws.path)

                def scan(r: Path = root) -> list[Path]:
                    return [
                        p
                        for p in r.rglob("*")
                        if p.is_file()
                        and not p.is_symlink()
                        and ".git" not in p.relative_to(r).parts
                        and p.suffix.lower() in MEDIA_SUFFIXES
                    ]

                for p in (await asyncio.to_thread(scan))[: self.settings.max_inputs]:
                    out[p.relative_to(root).as_posix()] = p
        async with self.sm() as s:
            for art in (await s.execute(select(Artifact).where(Artifact.job_id == job_id).order_by(Artifact.created_at))).scalars():
                p = Path(art.path)
                if p.suffix.lower() in MEDIA_SUFFIXES and await asyncio.to_thread(p.is_file):
                    out.setdefault(f"artifact:{art.name}", p)
        return out

    async def _spec(self, ctx: StepRunContext, row: Step, available: dict[str, Path], feedback: str) -> MediaSpec:
        listing = "\n".join(f"- {name}" for name in list(available)[: self.settings.max_inputs]) or "- (none)"
        acceptance = [a for a in (row.acceptance or []) if isinstance(a, dict) and a.get("type") == "artifact"]
        prompt = (
            f"Media step ({row.kind}). Goal:\n{DEFAULT_REDACTOR.text(row.goal)}\n\nConstraints: {DEFAULT_REDACTOR.obj(row.constraints)}\n"
            f"Required artifacts: {acceptance}\n\nAvailable inputs:\n{listing}\n\n{SPEC_GUIDE}\n"
            + (f"\nThe previous attempt failed: {feedback[:1500]}\nFix the parameters.\n" if feedback else "")
            + "\nAnswer only with the MediaSpec JSON object."
        )
        res = await self.chat.structured(
            self.settings.spec_alias,
            [ChatMessage("system", "You translate media tasks into exact tool parameters. No explanations."), ChatMessage("user", prompt)],
            MediaSpec,
            ctx=CallContext(purpose="media_spec", job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id),
            max_tokens=800,
            temperature=0.0,
        )
        return res.value

    # ------------------------------------------------------------------ helpers
    def _target_dir(self, job_id: uuid.UUID, step_id: uuid.UUID) -> Path:
        base = self.artifacts_dir or get_settings().artifacts_dir
        return base / str(job_id) / str(step_id)

    async def _correction_or_fail(self, ctx: StepRunContext, row: Step, code: str, message: str) -> StepOutcome:
        limit = self.config.policies.correction.max_corrections_per_step
        if row.correction_count >= limit or row.attempt_count >= row.max_attempts:
            return StepOutcome(
                "blocked",
                error_code=code,
                error_message=message[:2000],
                replan_reason="step_failed",
                replan_evidence={"media_error": message[:2000]},
            )
        async with self.sm() as s:
            await s.execute(
                update(StepAttempt)
                .where(StepAttempt.id == ctx.attempt_id)
                .values(
                    correction_input={
                        "pending": True,
                        "source": "media",
                        "items": [{"source": "media", "label": code, "message": message[:2000]}],
                    }
                )
            )
            await s.execute(update(Step).where(Step.id == row.id).values(correction_count=Step.correction_count + 1))
            await s.commit()
        return StepOutcome("failed", error_code=code, error_message=message[:2000], retryable=True, retry_delay_seconds=0)

    @staticmethod
    def check_acceptance(acceptance: list[Any], artifacts: list[dict[str, Any]], kind: str) -> list[str]:
        problems: list[str] = []
        criteria = [ArtifactEvidence.model_validate(a) for a in acceptance if isinstance(a, dict) and a.get("type") == "artifact"]
        if not criteria:
            criteria = [ArtifactEvidence(kind=kind)]
        for c in criteria:
            hits = [
                a
                for a in artifacts
                if a["kind"] == c.kind and fnmatch.fnmatch(a["name"], c.name_glob) and a["size_bytes"] >= c.min_size_bytes
            ]
            if len(hits) < c.min_count:
                problems.append(
                    f"expected >= {c.min_count} {c.kind} artifact(s) matching {c.name_glob!r} "
                    f"(>= {c.min_size_bytes} bytes), got {len(hits)}"
                )
        return problems

    # ------------------------------------------------------------------ run
    async def run(self, ctx: StepRunContext) -> StepOutcome:
        async with self.sm() as s:
            row = (await s.execute(select(Step).where(Step.id == ctx.step_id))).scalar_one()
        available = await self._available_inputs(ctx.job_id)
        feedback = (
            "; ".join(str(i.get("message", "")) for i in (ctx.correction_input.get("items") or []))
            if ctx.attempt_kind == "correction"
            else ""
        )
        try:
            spec = await self._spec(ctx, row, available, feedback)
        except (ModelError, HermclawError) as exc:
            return StepOutcome("failed", error_code=exc.code, error_message=exc.message, retryable=True)
        unknown = [i for i in spec.inputs if i not in available]
        if unknown:
            return await self._correction_or_fail(
                ctx, row, "MEDIA_INPUT_UNKNOWN", f"unknown inputs {unknown[:10]}; available: {list(available)[:30]}"
            )
        names = {i: Path(i.removeprefix("artifact:")).name for i in spec.inputs}
        if len(set(names.values())) != len(names):
            return await self._correction_or_fail(ctx, row, "MEDIA_INPUT_NAME_CLASH", "two inputs share a file name")
        params = dict(spec.params)
        if spec.backend == "ffmpeg":
            if spec.operation not in FFMPEG_OPERATIONS:
                return await self._correction_or_fail(ctx, row, "MEDIA_OPERATION_UNKNOWN", f"operation must be one of {FFMPEG_OPERATIONS}")
            params["operation"] = spec.operation
        for key in ("input",):
            if isinstance(params.get(key), str) and params[key] in names:
                params[key] = names[params[key]]
        for key in ("inputs", "images"):
            if isinstance(params.get(key), list):
                params[key] = [names.get(v, v) for v in params[key]]
        request = MediaJobRequest(
            request_id=f"m-{uuid.uuid4().hex[:24]}",
            job_id=ctx.job_id.hex,
            step_id=ctx.step_id.hex,
            kind=row.kind,  # type: ignore[arg-type]
            backend=spec.backend,
            params=params,
            timeout_seconds=self.settings.job_timeout_seconds,
        )
        async with self.sm() as s:
            await append_event(
                s,
                EventType.MEDIA_STARTED,
                source_type="media",
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                attempt_id=ctx.attempt_id,
                payload={"kind": row.kind, "backend": spec.backend, "operation": spec.operation, "inputs": len(spec.inputs)},
            )
            await emit_status(s, ctx.job_id, f"{row.kind.capitalize()}-Job wartet auf GPU-Lease (.224)", step_id=ctx.step_id)
            await s.commit()
        async with self.resources.hold_media(
            row.kind,
            job_id=ctx.job_id,
            step_id=ctx.step_id,
            ttl_seconds=self.settings.lease_ttl_seconds,
            wait_timeout=self.settings.lease_wait_seconds,
        ) as (_media, _keeper):
            client = await self.client_factory()
            for original, name in names.items():
                data = await asyncio.to_thread(available[original].read_bytes)
                if len(data) > self.settings.max_input_bytes:
                    return StepOutcome("failed", error_code="MEDIA_INPUT_TOO_LARGE", error_message=f"{original} exceeds the input limit")
                await client.put_media_input(request.job_id, name, data)
            async with self.sm() as s:
                await emit_status(s, ctx.job_id, f"{row.kind.capitalize()} wird auf .224 gerendert ({spec.backend})", step_id=ctx.step_id)
                await s.commit()
            result = await client.run_media_job(request)
            if not result.ok:
                async with self.sm() as s:
                    await append_event(
                        s,
                        EventType.MEDIA_FINISHED,
                        source_type="media",
                        job_id=ctx.job_id,
                        step_id=ctx.step_id,
                        severity="warning",
                        payload={"ok": False, "error": (result.error or "")[:500]},
                    )
                    await s.commit()
                return await self._correction_or_fail(ctx, row, "MEDIA_JOB_FAILED", result.error or "media job failed")
            target = self._target_dir(ctx.job_id, ctx.step_id)
            stored: list[dict[str, Any]] = []
            for art in result.artifacts:
                data = await client.download_media_file(request.job_id, request.request_id, art["name"])
                if hashlib.sha256(data).hexdigest() != art["sha256"]:
                    return StepOutcome(
                        "failed", error_code="MEDIA_ARTIFACT_CORRUPT", error_message=f"{art['name']} hash mismatch", retryable=True
                    )
                path = target / art["name"]

                def _write(p: Path = path, d: bytes = data) -> None:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(d)

                await asyncio.to_thread(_write)
                stored.append({**art, "kind": row.kind, "local_path": str(path)})
        # lease released here -> drained AI work resumes
        async with self.sm() as s:
            for art in stored:
                s.add(
                    Artifact(
                        job_id=ctx.job_id,
                        step_id=ctx.step_id,
                        kind=row.kind,
                        name=art["name"],
                        path=art["local_path"],
                        media_type=art["media_type"],
                        size_bytes=art["size_bytes"],
                        sha256=art["sha256"],
                        metadata_={"backend": spec.backend, "operation": spec.operation, "request_id": request.request_id},
                    )
                )
            await append_event(
                s,
                EventType.MEDIA_FINISHED,
                source_type="media",
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                payload={"ok": True, "artifacts": [a["name"] for a in stored], "duration_ms": result.duration_ms},
            )
            await s.commit()
        problems = self.check_acceptance(list(row.acceptance or []), stored, row.kind)
        if problems:
            return await self._correction_or_fail(ctx, row, "MEDIA_ACCEPTANCE_FAILED", "; ".join(problems))
        return StepOutcome(
            "completed",
            summary=f"{len(stored)} {row.kind} artifact(s)",
            result={"artifacts": [a["name"] for a in stored], "backend": spec.backend},
        )
