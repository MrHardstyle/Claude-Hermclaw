"""P38 load and performance baseline (build host, scripted models – the model latency of .224 is not part of it).

Every test records its measurements; with ``HERMCLAW_PERF_OUT=<file.json>`` the session writes them as JSON
(source of docs/operations/performance-baseline.md). Assertions use generous ceilings: they catch regressions by
orders of magnitude, the recorded numbers are the baseline.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from hermclaw.context_builder import ContextBuilder, ContextBuilderConfig
from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.worker import ModelLoadRequest
from hermclaw.core.config import get_config, load_config
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.core.settings import Settings
from hermclaw.events.store import append_event, list_events
from hermclaw.gitops.reader import WorkspaceGitReader
from hermclaw.gitops.runner import GitRunner
from hermclaw.persistence.models import Job
from hermclaw.repo_intelligence import RepoIntelConfig, RepoIntelligence
from hermclaw.resources.manager import ResourceManager
from hermclaw.runtime.main import SchedulerProcess
from hermclaw.runtime.services import ServiceOverrides
from hermclaw.scheduler.scheduler import Scheduler, SchedulerSettings
from tests.failure.test_recovery_process_kill import Child
from tests.integration.runtime_stack import StackModels, checked_plan
from tests.integration.test_gitops_support import make_world
from tests.integration.test_media_worker import _app as media_app
from tests.integration.test_media_worker import _client as media_client
from tests.integration.test_scheduler import FakeDriver, FakeHandler, StepSpec
from tests.integration.test_wol_readiness import controller, fake_host, register
from tests.integration.test_workers_support import FakeOllama, lifespan
from tests.unit.test_context_builder_support import TOOLS, make_input, make_step

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.slow]
TERMINAL = ("succeeded", "failed", "cancelled")
RESULTS: dict[str, Any] = {}


@pytest.fixture(scope="module", autouse=True)
def _write_results() -> Iterator[None]:
    yield
    out = os.environ.get("HERMCLAW_PERF_OUT")
    if out:
        Path(out).write_text(json.dumps(RESULTS, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))], 4)


async def _cancel_open_jobs(sm: Any) -> None:
    async with sm() as s:
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        await s.commit()


# ------------------------------------------------------------------------------------------- 38.1 concurrent jobs
async def test_38_1_concurrent_jobs(sessionmaker: Any, tmp_path: Path) -> None:
    n = 6
    await _cancel_open_jobs(sessionmaker)
    worlds = [await make_world(sessionmaker, tmp_path / f"w{i}") for i in range(n)]
    models = StackModels([checked_plan() for _ in range(n)])
    settings = Settings(
        env="test", data_dir=tmp_path / "data", instance_id=f"load-{uuid.uuid4().hex[:6]}", execution_mode="local", wake_on_lan=False
    )
    proc = SchedulerProcess(
        get_config(),
        settings,
        overrides=ServiceOverrides(chat=models, research_enabled=False, gitlab=None, lease_keeper_interval=0.2),
        scheduler_settings=SchedulerSettings(concurrency=4, poll_seconds=0.05, retry_backoff_seconds=(0,)),
    )
    ids = []
    async with sessionmaker() as s:
        for w in worlds:
            job = Job(title="Set VALUE to two", prompt="Change VALUE in src/module.py to 2.", repository_id=w.repo.id)
            s.add(job)
            await s.flush()
            ids.append(job.id)
        await s.commit()
    t0 = time.monotonic()
    task = asyncio.create_task(proc.run())
    try:
        async with asyncio.timeout(300):
            while True:
                async with sessionmaker() as s:
                    jobs = (await s.execute(select(Job).where(Job.id.in_(ids)))).scalars().all()
                if all(j.status in TERMINAL for j in jobs):
                    break
                await asyncio.sleep(0.2)
    finally:
        proc.stop()
        await asyncio.wait_for(task, timeout=90)
    wall = time.monotonic() - t0
    durations = [(j.updated_at - j.created_at).total_seconds() for j in jobs]
    RESULTS["38.1_concurrent_jobs"] = {
        "jobs": n,
        "scheduler_concurrency": 4,
        "wall_s": round(wall, 2),
        "job_s_p50": _pct(durations, 0.5),
        "job_s_max": round(max(durations), 2),
    }
    assert [j.status for j in jobs] == ["succeeded"] * n, [(j.status, j.error_code) for j in jobs]
    assert wall < 240


# ------------------------------------------------------------------------------------------- 38.2 event volume
async def test_38_2_event_volume(sessionmaker: Any) -> None:
    async with sessionmaker() as s:
        job = Job(title="events", prompt="p", status="cancelled")
        s.add(job)
        await s.commit()
    n, batch = 20_000, 1_000
    t0 = time.monotonic()
    for start in range(0, n, batch):
        async with sessionmaker() as s:
            for i in range(start, start + batch):
                await append_event(s, "status", source_type="load", job_id=job.id, payload={"text": f"line {i}", "i": i})
            await s.commit()
    write_s = time.monotonic() - t0
    t1 = time.monotonic()
    seen, cursor = 0, 0
    async with sessionmaker() as s:
        while True:
            page = await list_events(s, job_id=job.id, after_sequence=cursor, limit=5000)
            if not page:
                break
            seen += len(page)
            cursor = page[-1].sequence
    replay_s = time.monotonic() - t1
    RESULTS["38.2_event_volume"] = {
        "events": n,
        "append_per_s": round(n / write_s),
        "replay_per_s": round(n / replay_s),
        "write_s": round(write_s, 2),
        "replay_s": round(replay_s, 2),
    }
    assert seen == n
    assert n / write_s > 200 and n / replay_s > 2_000


# ------------------------------------------------------------------------------------------- 38.3 DB load
async def test_38_3_db_load_many_steps_skip_locked(sessionmaker: Any) -> None:
    await _cancel_open_jobs(sessionmaker)
    jobs_n, steps_n = 5, 25
    async with sessionmaker() as s:
        jobs = [Job(title=f"db-load-{i}", prompt="p") for i in range(jobs_n)]
        s.add_all(jobs)
        await s.commit()
    driver = FakeDriver(sessionmaker, plans={j.id: [StepSpec(f"S{k:03d}") for k in range(1, steps_n + 1)] for j in jobs})
    handler = FakeHandler({"test"}, delay=0.01)
    sched = Scheduler(sessionmaker, get_config(), [handler], driver, settings=SchedulerSettings(concurrency=10, poll_seconds=0.01))
    ticks: list[float] = []
    t0 = time.monotonic()
    async with asyncio.timeout(240):
        while True:
            a = time.monotonic()
            await sched.tick()
            ticks.append(time.monotonic() - a)
            async with sessionmaker() as s:
                states = (await s.execute(select(Job.status).where(Job.id.in_([j.id for j in jobs])))).scalars().all()
            if all(st in TERMINAL for st in states) and not sched._job_tasks:
                break
            await asyncio.sleep(0.005)
    await sched.wait_idle()
    wall = time.monotonic() - t0
    RESULTS["38.3_db_load"] = {
        "jobs": jobs_n,
        "steps": jobs_n * steps_n,
        "wall_s": round(wall, 2),
        "ticks": len(ticks),
        "tick_ms_p50": round(_pct(ticks, 0.5) * 1000, 1),
        "tick_ms_p95": round(_pct(ticks, 0.95) * 1000, 1),
    }
    assert set(states) == {"succeeded"}
    assert len(handler.calls) == jobs_n * steps_n and all(c[1] == 1 for c in handler.calls), "every step ran exactly once"


# ------------------------------------------------------------------------------------------- 38.4 context build
def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


async def test_38_4_context_build_timings(sessionmaker: Any, tmp_path: Path) -> None:
    root = tmp_path / "big"
    (root / "pkg").mkdir(parents=True)
    files = 1500
    for i in range(files):
        (root / "pkg" / f"mod_{i:04d}.py").write_text(
            f'"""Module {i}."""\n\n\ndef handler_{i}(value: int) -> int:\n    return value + {i}\n\n\nclass Service{i}:\n    def run(self) -> int:\n        return handler_{i}({i})\n',
            encoding="utf-8",
        )
    (root / "app.py").write_text(
        "from pkg.mod_0042 import handler_42\n\n\ndef main() -> int:\n    return handler_42(1)\n", encoding="utf-8"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@x", "-c", "user.name=t", "commit", "-q", "-m", "seed")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", "hermclaw/job-load")
    ws = WorkspaceHandle(
        id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        path=root,
        branch="hermclaw/job-load",
        base_branch="main",
        base_sha=base,
        repository_key=f"load-{uuid.uuid4().hex[:6]}",
    )
    cfg = load_config()
    repo = RepoIntelligence(sessionmaker, embedder=None, config=RepoIntelConfig.from_hermclaw(cfg))
    reader = WorkspaceGitReader(GitRunner(author_name="t", author_email="t@x"), forbidden_globs=list(cfg.policies.scope.always_forbidden))
    builder = ContextBuilder(repo, reader, ContextBuilderConfig.from_config(cfg))
    step = make_step(
        goal="Make handler_42 in pkg/mod_0042.py return value * 2 and keep app.py working.",
        scope=ScopeContract(target_paths=["pkg/mod_0042.py", "app.py"]),
        repo_hints=["pkg/mod_0042.py"],
    )
    t0 = time.monotonic()
    inv = await repo.inventory_summary(ws)
    inventory_s = time.monotonic() - t0
    t1 = time.monotonic()
    first = await builder.build(make_input(step=step, workspace=ws, tools=TOOLS))
    cold_s = time.monotonic() - t1
    warm: list[float] = []
    for _ in range(8):
        a = time.monotonic()
        await builder.build(make_input(step=step, workspace=ws, tools=TOOLS))
        warm.append(time.monotonic() - a)
    await repo.aclose()
    RESULTS["38.4_context_build"] = {
        "files": files + 1,
        "inventory_s": round(inventory_s, 3),
        "build_cold_s": round(cold_s, 3),
        "build_warm_s_p50": _pct(warm, 0.5),
        "build_warm_s_max": round(max(warm), 3),
        "prompt_chars": sum(len(m.content) for m in first.messages),
    }
    assert inv["file_count"] == files + 1
    assert cold_s < 60 and statistics.median(warm) < 15


# ------------------------------------------------------------------------------------------- 38.5 model load/unload
@pytest.fixture
async def ollama() -> Any:
    fake = await FakeOllama().start()
    yield fake
    await fake.stop()


async def test_38_5_model_load_unload_and_lease_latency(sessionmaker: Any, tmp_path: Path, ollama: FakeOllama) -> None:
    (tmp_path / "w").mkdir()
    app = media_app(tmp_path / "w", ollama)
    switches: list[float] = []
    ollama.installed |= {"qwen3-coder:30b"}
    async with lifespan(app), media_client(app) as c:
        for i in range(10):
            a = time.monotonic()
            await c.load_model(ModelLoadRequest(model="qwen3-coder:30b" if i % 2 else "gemma4:26b", context_tokens=8192))
            switches.append(time.monotonic() - a)
        await c.unload_model("qwen3-coder:30b")
    rm = ResourceManager.from_config(sessionmaker, get_config(), f"load-{uuid.uuid4().hex[:6]}")
    coder = get_config().models.by_role("coder")
    lease: list[float] = []
    for _ in range(30):
        a = time.monotonic()
        async with rm.hold_model(coder, job_id=None, step_id=None, ttl_seconds=60, wait_timeout=30):
            lease.append(time.monotonic() - a)

    async def contender() -> float:
        a = time.monotonic()
        async with rm.hold_model(coder, job_id=None, step_id=None, ttl_seconds=60, wait_timeout=60):
            await asyncio.sleep(0.05)
        return time.monotonic() - a

    t0 = time.monotonic()
    waits = await asyncio.gather(*(contender() for _ in range(8)))
    contended = time.monotonic() - t0
    RESULTS["38.5_model_load_unload"] = {
        "worker_switch_ms_p50": round(_pct(switches, 0.5) * 1000, 1),
        "lease_acquire_release_ms_p50": round(_pct(lease, 0.5) * 1000, 1),
        "lease_acquire_release_ms_p95": round(_pct(lease, 0.95) * 1000, 1),
        "contended_8x50ms_total_s": round(contended, 2),
        "contended_max_wait_s": round(max(waits), 2),
        "note": "FakeOllama: excludes real model load time of the GTX 1080 (live: BLOCKER-001)",
    }
    assert contended >= 8 * 0.05, "one large-model lease at a time (budget)"
    assert _pct(lease, 0.5) < 1.0


# ------------------------------------------------------------------------------------------- 38.6 worker wake
async def test_38_6_worker_wake_latency(sessionmaker: Any) -> None:
    async with fake_host(sessionmaker, boot_delay=0.3) as fh:
        cfg = fh.host_config()
        await register(sessionmaker, cfg)
        wc = controller(sessionmaker, cfg)
        try:
            a = time.monotonic()
            woke = await wc.ensure_worker_ready(fh.worker_id, uuid.uuid4(), required_capabilities=["model_inference"])
            cold = time.monotonic() - a
            b = time.monotonic()
            again = await wc.ensure_worker_ready(fh.worker_id, uuid.uuid4(), required_capabilities=["model_inference"])
            warm = time.monotonic() - b
        finally:
            await wc.aclose()
    RESULTS["38.6_worker_wake"] = {
        "wake_from_offline_s": round(cold, 3),
        "simulated_boot_delay_s": 0.3,
        "already_ready_probe_s": round(warm, 3),
        "stages": [s.stage.value if hasattr(s.stage, "value") else str(s.stage) for s in woke.stages],
    }
    assert woke.ready and again.ready and not again.woke
    assert warm < cold


# ------------------------------------------------------------------------------------------- 38.7 video preemption
async def test_38_7_video_preemption_latency(sessionmaker: Any) -> None:
    rm = ResourceManager.from_config(sessionmaker, get_config(), f"load-{uuid.uuid4().hex[:6]}")
    coder = get_config().models.by_role("coder")
    asked = asyncio.Event()
    started = asyncio.Event()
    marks: dict[str, float] = {}

    async def ai() -> None:
        async with rm.hold_model(
            coder, on_preempt=lambda *_: asked.set(), job_id=None, step_id=None, ttl_seconds=60, wait_timeout=10, keeper_interval=0.05
        ):
            started.set()
            await asyncio.wait_for(asked.wait(), timeout=30)
            marks["asked"] = time.monotonic()
            await asyncio.sleep(0.05)  # the coder finishes its turn and checkpoints
        marks["released"] = time.monotonic()

    task = asyncio.create_task(ai())
    await started.wait()
    t0 = time.monotonic()
    async with rm.hold_media("video", job_id=None, step_id=None, ttl_seconds=60, wait_timeout=60):
        granted = time.monotonic()
    await task
    RESULTS["38.7_video_preemption"] = {
        "request_to_yield_request_s": round(marks["asked"] - t0, 3),
        "request_to_gpu_grant_s": round(granted - t0, 3),
        "ai_release_to_grant_s": round(granted - marks["released"], 3),
    }
    assert granted >= marks["released"], "the video job never shares the GPU with the AI lease"
    assert granted - t0 < 15


# ------------------------------------------------------------------------------------------- 38.8 memory usage
def _vm_hwm_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    return 0


async def test_38_8_scheduler_process_memory(sessionmaker: Any, tmp_path: Path) -> None:
    await _cancel_open_jobs(sessionmaker)
    world = await make_world(sessionmaker, tmp_path)
    async with sessionmaker() as s:
        job = Job(title="Set VALUE to two", prompt="Change VALUE in src/module.py to 2.", repository_id=world.repo.id)
        s.add(job)
        await s.commit()
    child = await Child(world, f"mem-{uuid.uuid4().hex[:6]}", tmp_path / "child.log", "--plan").start()
    try:
        async with asyncio.timeout(150):
            while True:
                async with sessionmaker() as s:
                    status = (await s.execute(select(Job.status).where(Job.id == job.id))).scalar_one()
                if status in TERMINAL:
                    break
                await asyncio.sleep(0.2)
        assert child.proc is not None
        hwm = _vm_hwm_kb(child.proc.pid)
    finally:
        rc = await child.terminate()
    RESULTS["38.8_memory"] = {"scheduler_peak_rss_mb": round(hwm / 1024, 1), "job_status": status, "exit_code": rc}
    assert status == "succeeded" and rc == 0
    assert hwm / 1024 < 1024, "scheduler process peak RSS stays below 1 GiB for a job"
