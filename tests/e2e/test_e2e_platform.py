"""P37 end-to-end matrix – platform scenarios through the full runtime (harness: tests/e2e/harness.py).

37.10 research-only job (real ResearchEngine against the local fixture web), 37.11 image and 37.12 video jobs (real
model/media daemon app with ffmpeg, GPU leases, verified artifacts), 37.13 Wake-on-LAN before dispatch to the remote
execution worker (real execution daemon app, signed requests, workspace sync), 37.14 execution worker failure and
recovery (daemon restarted mid-job with an empty workspace copy, transient connection loss)."""

# ruff: noqa: F811  (imported fixtures)
from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from hermclaw.contracts.worker import WorkerHeartbeat
from hermclaw.core.config import HostConfig, HostsConfig, get_config
from hermclaw.persistence.models import Artifact, ResearchRun, ResourceLease
from hermclaw.workers.auth import StaticTokenStore
from hermclaw.workers.registry import WorkerRegistry
from tests.e2e.harness import Scenario, run_scenario
from tests.e2e.test_e2e_code_tasks import fastapi_scenario
from tests.integration.test_media_worker import _app as media_app
from tests.integration.test_media_worker import _video
from tests.integration.test_research_engine import QUESTION, engine_for, happy_chat, server, static_search  # noqa: F401
from tests.integration.test_research_support import FixtureServer
from tests.integration.test_workers_execution_daemon import WORKER
from tests.integration.test_workers_execution_daemon import _app as exec_app
from tests.integration.test_workers_support import TOKEN, FakeOllama, lifespan
from tests.unit.test_planner_support import plan, step

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.slow]
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")


# ------------------------------------------------------------------------------------------- 37.10 research-only
async def test_37_10_research_only_task(sessionmaker: Any, tmp_path: Path, server: FixtureServer) -> None:
    engine = engine_for(sessionmaker, happy_chat(), static_search(server))
    sc = Scenario(
        name="research",
        title="Toolkit 4.2 Python requirement",
        prompt=QUESTION,
        seed=None,
        triage={"intent": "research", "needs_repository": False, "needs_research": True},
        plans=[
            plan(
                "answer with sources", [step("S001", "research", "research", f"Research and answer with sources: {QUESTION}", network=True)]
            )
        ],
        overrides={"research": engine, "research_enabled": True},
    )
    try:
        out = await run_scenario(sessionmaker, sc, tmp_path)
    finally:
        await engine.aclose()
    s1 = out.steps["S001"]
    assert s1.result["sources"] >= 2 and "docs.toolkit.test" in s1.result["worker_summary"]
    async with sessionmaker() as s:
        runs = (await s.execute(select(ResearchRun).where(ResearchRun.job_id == out.job.id))).scalars().all()
    assert len(runs) == 1 and runs[0].status in ("completed", "partial")
    assert "docs.toolkit.test" in out.report, "the final report lists the sources (no invisible sources)"
    assert "research.finished" in out.event_types()


# ------------------------------------------------------------------------------------------- 37.11/37.12 media
@pytest.fixture
async def ollama() -> AsyncIterator[FakeOllama]:
    fake = await FakeOllama().start()
    yield fake
    await fake.stop()


async def _media_job(
    sessionmaker: Any, tmp_path: Path, ollama: FakeOllama, *, kind: str, spec: dict[str, Any], acceptance: dict[str, Any]
) -> Any:
    (tmp_path / "worker").mkdir()
    app = media_app(tmp_path / "worker", ollama)
    src = _video(tmp_path / "in.mp4")

    async def upload(job_id: uuid.UUID) -> None:
        async with sessionmaker() as s:
            s.add(
                Artifact(job_id=job_id, kind="upload", name="in.mp4", path=str(src), media_type="video/mp4", size_bytes=src.stat().st_size)
            )
            await s.commit()

    from tests.integration.test_media_worker import _client as media_client

    async with lifespan(app), media_client(app) as client:

        async def factory() -> Any:
            return client

        sc = Scenario(
            name=kind,
            title=f"{kind} job",
            prompt=f"Make a {kind} from the uploaded clip.",
            seed=None,
            triage={"intent": "media", "needs_repository": False},
            plans=[plan(f"{kind} job", [step("S001", kind, kind, f"Produce the {kind} from artifact in.mp4.", acceptance=[acceptance])])],
            media_specs=[spec],
            overrides={"media_client_factory": factory},
        )
        return await run_scenario(sessionmaker, sc, tmp_path, before_start=upload)


async def _active_leases(sessionmaker: Any, job_id: uuid.UUID) -> list[Any]:
    async with sessionmaker() as s:
        return list(
            (
                await s.execute(
                    select(ResourceLease).where(ResourceLease.owner_job_id == job_id, ResourceLease.state.in_(["active", "preempting"]))
                )
            ).scalars()
        )


@needs_ffmpeg
async def test_37_11_image_job(sessionmaker: Any, tmp_path: Path, ollama: FakeOllama) -> None:
    spec = {
        "backend": "ffmpeg",
        "operation": "thumbnail",
        "inputs": ["artifact:in.mp4"],
        "params": {"input": "artifact:in.mp4", "output": "poster.png", "at_seconds": 1, "width": 96},
    }
    out = await _media_job(
        sessionmaker, tmp_path, ollama, kind="image", spec=spec, acceptance={"type": "artifact", "kind": "image", "name_glob": "*.png"}
    )
    images = [a for a in out.artifacts if a.kind == "image"]
    assert [a.name for a in images] == ["poster.png"] and Path(images[0].path).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert await _active_leases(sessionmaker, out.job.id) == [], "GPU/media leases released"


@needs_ffmpeg
async def test_37_12_video_job(sessionmaker: Any, tmp_path: Path, ollama: FakeOllama) -> None:
    spec = {
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
    out = await _media_job(
        sessionmaker, tmp_path, ollama, kind="video", spec=spec, acceptance={"type": "artifact", "kind": "video", "name_glob": "*.webm"}
    )
    videos = [a for a in out.artifacts if a.kind == "video"]
    assert [a.name for a in videos] == ["preview.webm"] and videos[0].size_bytes == Path(videos[0].path).stat().st_size > 0
    assert "media.finished" in out.event_types()
    assert await _active_leases(sessionmaker, out.job.id) == []


# ------------------------------------------------------------------------------------------- remote execution worker
async def _heartbeat(sessionmaker: Any, registry: WorkerRegistry, state: str) -> None:
    hb = WorkerHeartbeat(
        worker_id=WORKER,
        hostname="exec",
        kind="execution",
        state=state,
        worker_version="0.1.0rc1",
        capabilities=["sandbox"],
        sent_at=datetime.now(UTC),
    )
    async with sessionmaker() as s, s.begin():
        await registry.ingest_heartbeat(s, hb)


async def _register_asleep(sessionmaker: Any, registry: WorkerRegistry) -> None:
    hosts = HostsConfig(
        hosts=[HostConfig(id=WORKER, address="127.0.0.1", role="execution_worker", worker_kind="execution", worker_api="http://exec")]
    )
    async with sessionmaker() as s, s.begin():
        await registry.register_from_config(s, hosts, get_config().capabilities)
    await _heartbeat(sessionmaker, registry, "ready")
    async with sessionmaker() as s, s.begin():
        await registry.set_state(s, WORKER, "sleeping", reason="e2e: host suspended")


class Wake:
    """Stands in for the WakeController: 'sends the magic packet' by letting the daemon report in again."""

    def __init__(self, sessionmaker: Any, registry: WorkerRegistry) -> None:
        self.sm, self.registry, self.calls = sessionmaker, registry, []

    async def ensure_worker_ready(self, worker_id: str, job_id: uuid.UUID | None = None, **kw: Any) -> Any:
        self.calls.append(worker_id)
        if worker_id != WORKER:
            return type("R", (), {"ready": False})()
        await _heartbeat(self.sm, self.registry, "ready")
        return type("R", (), {"ready": True})()


class SwitchingTransport(httpx.AsyncBaseTransport):
    """Routes to daemon A until ``switch_after`` command runs, then to a *fresh* daemon B (restart with empty state);
    the first manifest GET fails once with a connection error (transient network loss)."""

    def __init__(self, first: Any, second: Any | None = None, *, switch_after: int = 10**9, drop_first_manifest: bool = False) -> None:
        self.a, self.b = httpx.ASGITransport(app=first), httpx.ASGITransport(app=second) if second is not None else None
        self.switch_after, self.commands, self.drop = switch_after, 0, drop_first_manifest
        self.dropped = 0
        self.served_by_b = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.drop and request.method == "GET" and request.url.path.endswith("/manifest") and self.dropped == 0:
            self.dropped += 1
            raise httpx.ConnectError("connection reset by peer (simulated)", request=request)
        if request.method == "POST" and request.url.path == "/v1/commands":
            self.commands += 1
        if self.b is not None and self.commands > self.switch_after:
            self.served_by_b += 1
            return await self.b.handle_async_request(request)
        return await self.a.handle_async_request(request)


def _remote(sc: Scenario, sessionmaker: Any, registry: WorkerRegistry, transport: httpx.AsyncBaseTransport) -> Scenario:
    sc.settings = {"execution_mode": "remote"}
    sc.overrides = {
        "executor_factory": None,
        "wake": Wake(sessionmaker, registry),
        "token_store": StaticTokenStore({WORKER: TOKEN}),
        "worker_client_kwargs": {"transport": transport, "get_retries": 2, "retry_backoff_seconds": 0.05},
    }
    return sc


async def test_37_13_wake_on_lan_job_runs_on_the_woken_remote_worker(sessionmaker: Any, tmp_path: Path) -> None:
    registry = WorkerRegistry()
    await _register_asleep(sessionmaker, registry)
    (tmp_path / "worker").mkdir()
    app = exec_app(tmp_path / "worker")
    async with lifespan(app):
        sc = _remote(fastapi_scenario(), sessionmaker, registry, httpx.ASGITransport(app=app))
        sc.name = "wol"
        out = await run_scenario(sessionmaker, sc, tmp_path)
        wake = sc.overrides["wake"]
        assert WORKER in wake.calls, "the sleeping execution worker was woken before dispatch"
        remote_dirs = [p for p in Path(app.state.execution.root).iterdir() if p.is_dir()]
    assert remote_dirs and (remote_dirs[0] / "tests" / "test_health.py").is_file(), "the commands ran on the worker copy"
    assert not any((d / ".git").exists() for d in remote_dirs), ".git never reaches the worker"
    assert '@app.get("/health")' in out.remote_file("app/main.py") and len(out.commits()) == 1


async def test_37_14_worker_restart_and_connection_loss_are_recovered(sessionmaker: Any, tmp_path: Path) -> None:
    registry = WorkerRegistry()
    await _register_asleep(sessionmaker, registry)
    await _heartbeat(sessionmaker, registry, "ready")
    (tmp_path / "w1").mkdir()
    (tmp_path / "w2").mkdir()
    first, second = exec_app(tmp_path / "w1"), exec_app(tmp_path / "w2")
    async with lifespan(first), lifespan(second):
        transport = SwitchingTransport(first, second, switch_after=1, drop_first_manifest=True)
        sc = _remote(fastapi_scenario(), sessionmaker, registry, transport)
        sc.name = "worker-restart"
        out = await run_scenario(sessionmaker, sc, tmp_path)
    assert transport.dropped == 1, "a transient connection loss happened and was retried"
    assert transport.served_by_b > 0, "the restarted daemon (empty workspace copy) served the rest of the job"
    w2 = [p for p in (tmp_path / "w2").rglob("test_health.py")]
    assert w2, "the full workspace was re-synced to the restarted worker"
    assert len(out.commits()) == 1 and out.steps["S001"].status == "completed"
