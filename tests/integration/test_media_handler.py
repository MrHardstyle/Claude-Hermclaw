"""P29 media step handler: real resource manager (GPU lease, AI drain/preemption), real model/media daemon app,
real ffmpeg, real PostgreSQL artifacts. Only the spec model (fast router) is scripted."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from hermclaw.core.config import get_config
from hermclaw.media import MediaSettings, MediaStepHandler
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.persistence.models import Artifact, Job, ResourceLease, Step, StepAttempt
from hermclaw.resources.manager import ResourceManager
from hermclaw.scheduler import CancelToken, StepRunContext
from tests.integration.test_media_worker import _app as media_app
from tests.integration.test_media_worker import _client as media_client
from tests.integration.test_media_worker import _video
from tests.integration.test_workers_support import FakeOllama, lifespan

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")]
T = TypeVar("T", bound=BaseModel)


class SpecModel:
    def __init__(self, specs: list[dict[str, Any]]) -> None:
        self.specs, self.prompts = list(specs), []

    async def chat(self, *a: Any, **k: Any) -> ChatResult:  # pragma: no cover
        raise NotImplementedError

    async def structured(
        self, alias: str, messages: list[ChatMessage], schema: type[T], *, ctx: CallContext, **_: Any
    ) -> StructuredResult[T]:
        assert alias == "fast-router" and ctx.purpose == "media_spec"
        self.prompts.append(messages[-1].content)
        return StructuredResult(
            value=schema.model_validate(self.specs.pop(0)), result=ChatResult(content="{}", alias=alias, model="qwen3:8b")
        )


@pytest.fixture
async def ollama() -> AsyncIterator[FakeOllama]:
    fake = await FakeOllama().start()
    yield fake
    await fake.stop()


async def _setup(
    sm: Any,
    tmp_path: Path,
    *,
    kind: str = "video",
    acceptance: list[Any] | None = None,
    attempt_kind: str = "initial",
    correction: dict[str, Any] | None = None,
) -> StepRunContext:
    src = _video(tmp_path / "in.mp4")
    async with sm() as s:
        job = Job(title="media job", prompt="render", status="cancelled")  # not picked up by other scheduler tests
        s.add(job)
        await s.flush()
        st = Step(
            job_id=job.id,
            step_key="S001",
            title="make clip",
            kind=kind,
            capability=kind,
            goal="Make a small 160px webm preview of the input clip.",
            acceptance=acceptance or [{"type": "artifact", "kind": kind, "name_glob": "*.webm" if kind == "video" else "*.png"}],
            status="running",
        )
        s.add(st)
        await s.flush()
        s.add(
            Artifact(
                job_id=job.id,
                step_id=None,
                kind="upload",
                name="in.mp4",
                path=str(src),
                media_type="video/mp4",
                size_bytes=src.stat().st_size,
            )
        )
        att = StepAttempt(step_id=st.id, job_id=job.id, attempt_no=1, kind=attempt_kind, status="running")
        s.add(att)
        await s.commit()
    return StepRunContext(
        job_id=job.id,
        step_id=st.id,
        attempt_id=att.id,
        attempt_no=1,
        attempt_kind=attempt_kind,
        step_key="S001",
        kind=kind,
        capability=kind,
        sessionmaker=sm,
        config=get_config(),
        token=CancelToken(),
        correction_input=correction or {},
    )


def _handler(sm: Any, chat: SpecModel, client: Any, tmp_path: Path) -> tuple[MediaStepHandler, ResourceManager]:
    rm = ResourceManager.from_config(sm, get_config(), f"test-{uuid.uuid4().hex[:6]}")

    async def factory() -> Any:
        return client

    h = MediaStepHandler(
        sm,
        get_config(),
        chat=chat,
        resources=rm,
        client_factory=factory,
        settings=MediaSettings(lease_wait_seconds=30, lease_ttl_seconds=60),
        artifacts_dir=tmp_path / "artifacts",
    )
    return h, rm


TRANSCODE = {
    "backend": "ffmpeg",
    "operation": "transcode",
    "inputs": ["artifact:in.mp4"],
    "params": {
        "input": "artifact:in.mp4",
        "output": "preview.webm",
        "video_codec": "libvpx-vp9",
        "audio_codec": "none",
        "scale": "160:-2",
        "crf": 45,
    },
}


async def test_video_step_registers_artifacts_and_releases_gpu(sessionmaker: Any, tmp_path: Path, ollama: FakeOllama) -> None:
    (tmp_path / "w").mkdir()
    app = media_app(tmp_path / "w", ollama)
    async with lifespan(app), media_client(app) as client:
        chat = SpecModel([TRANSCODE])
        h, _rm = _handler(sessionmaker, chat, client, tmp_path)
        ctx = await _setup(sessionmaker, tmp_path)
        out = await h.run(ctx)
    assert out.outcome == "completed", (out.error_code, out.error_message)
    assert "artifact:in.mp4" in chat.prompts[0], "available inputs are offered to the spec model"
    async with sessionmaker() as s:
        arts = (await s.execute(select(Artifact).where(Artifact.step_id == ctx.step_id))).scalars().all()
        active = (
            (
                await s.execute(
                    select(ResourceLease).where(ResourceLease.owner_job_id == ctx.job_id, ResourceLease.state.in_(["active", "preempting"]))
                )
            )
            .scalars()
            .all()
        )
    assert (
        [a.name for a in arts] == ["preview.webm"] and arts[0].kind == "video" and Path(arts[0].path).stat().st_size == arts[0].size_bytes
    )
    assert active == [], "GPU, video and drain leases must be released after the job"


async def test_video_preempts_running_ai_lease(sessionmaker: Any, tmp_path: Path, ollama: FakeOllama) -> None:
    (tmp_path / "w").mkdir()
    app = media_app(tmp_path / "w", ollama)
    async with lifespan(app), media_client(app) as client:
        h, rm = _handler(sessionmaker, SpecModel([TRANSCODE]), client, tmp_path)
        coder = get_config().models.by_role("coder")
        yielded = asyncio.Event()
        order: list[str] = []

        async def ai_work() -> None:
            async with rm.hold_model(
                coder,
                on_preempt=lambda lease, status: yielded.set(),
                job_id=None,
                step_id=None,
                ttl_seconds=60,
                wait_timeout=10,
                keeper_interval=0.05,
            ):
                order.append("ai-start")
                await asyncio.wait_for(yielded.wait(), timeout=20)  # the coder checkpoints when asked to yield
                order.append("ai-yield")

        ai = asyncio.create_task(ai_work())
        while "ai-start" not in order:
            await asyncio.sleep(0.02)
        ctx = await _setup(sessionmaker, tmp_path)
        out = await asyncio.wait_for(h.run(ctx), timeout=60)
        await ai
    assert out.outcome == "completed", (out.error_code, out.error_message)
    assert order == ["ai-start", "ai-yield"], "the AI lease holder must be asked to yield before the video job renders"


async def test_bad_parameters_become_a_correction_attempt_with_the_worker_error(
    sessionmaker: Any, tmp_path: Path, ollama: FakeOllama
) -> None:
    (tmp_path / "w").mkdir()
    app = media_app(tmp_path / "w", ollama)
    bad = {**TRANSCODE, "params": {**TRANSCODE["params"], "video_codec": "h266_magic"}}
    async with lifespan(app), media_client(app) as client:
        h, _ = _handler(sessionmaker, SpecModel([bad]), client, tmp_path)
        ctx = await _setup(sessionmaker, tmp_path)
        out = await h.run(ctx)
    assert out.outcome == "failed" and out.retryable and out.retry_delay_seconds == 0 and out.error_code == "MEDIA_JOB_FAILED"
    async with sessionmaker() as s:
        att = (await s.execute(select(StepAttempt).where(StepAttempt.id == ctx.attempt_id))).scalar_one()
        st = (await s.execute(select(Step).where(Step.id == ctx.step_id))).scalar_one()
    assert att.correction_input["pending"] and "video_codec" in att.correction_input["items"][0]["message"] and st.correction_count == 1
    # the correction attempt shows the error to the spec model and succeeds
    (tmp_path / "w2").mkdir()
    app2 = media_app(tmp_path / "w2", ollama)
    async with lifespan(app2), media_client(app2) as client2:
        chat2 = SpecModel([TRANSCODE])
        h2, _ = _handler(sessionmaker, chat2, client2, tmp_path)
        (tmp_path / "x").mkdir()
        ctx2 = await _setup(sessionmaker, tmp_path / "x", attempt_kind="correction", correction=att.correction_input)
        out2 = await h2.run(ctx2)
    assert out2.outcome == "completed" and "video_codec" in chat2.prompts[0]


async def test_unknown_inputs_and_acceptance_mismatch_are_corrections(sessionmaker: Any, tmp_path: Path, ollama: FakeOllama) -> None:
    (tmp_path / "w").mkdir()
    app = media_app(tmp_path / "w", ollama)
    async with lifespan(app), media_client(app) as client:
        h, _ = _handler(sessionmaker, SpecModel([{**TRANSCODE, "inputs": ["/etc/passwd"]}]), client, tmp_path)
        out = await h.run(await _setup(sessionmaker, tmp_path))
        assert out.outcome == "failed" and out.error_code == "MEDIA_INPUT_UNKNOWN"
        h2, _ = _handler(sessionmaker, SpecModel([TRANSCODE]), client, tmp_path)
        (tmp_path / "y").mkdir()
        ctx = await _setup(sessionmaker, tmp_path / "y", acceptance=[{"type": "artifact", "kind": "video", "name_glob": "*.gif"}])
        out2 = await h2.run(ctx)
    assert out2.outcome == "failed" and out2.error_code == "MEDIA_ACCEPTANCE_FAILED" and "*.gif" in (out2.error_message or "")
