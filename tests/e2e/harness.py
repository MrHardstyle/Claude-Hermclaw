"""P37 end-to-end harness: one *scenario* = seed repository + Gemma plan (+ replans) + coder actions per step.

A scenario runs through the assembled runtime exactly like ``hermclaw scheduler`` (``SchedulerProcess``): GitEngine
(file:// upstream), RepoIntelligence (lexical), ScopeEngine/Auditor, ToolEngine, ContextBuilder, CoderLoop,
VerifierAdapter (acceptance commands really run), WorkspaceReviewer, ResourceManager, Planner/Replanner,
RuntimeJobDriver (triage, plan, finalize, push, report), Scheduler. Only the model *answers* are scripted
(:class:`ScenarioModels`), and commands run in the unisolated test executor because the container images of this
build host carry no project toolchains (the rootless sandbox itself is tested in P18).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import select, update

from hermclaw.contracts.step import StepContract
from hermclaw.core.config import PoliciesConfig, get_config
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.core.settings import Settings
from hermclaw.gitops.engine import GitEngine
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.persistence.models import Artifact, Event, Job, Step, StepAttempt
from hermclaw.runtime.main import SchedulerProcess
from hermclaw.runtime.services import ServiceOverrides
from hermclaw.scheduler.scheduler import SchedulerSettings
from hermclaw.tools.executors import SubprocessExecutor
from tests.integration.test_gitops_support import git, write
from tests.unit.test_planner_support import ScriptedChat

T = TypeVar("T", bound=BaseModel)
TERMINAL = ("succeeded", "failed", "cancelled")


def act(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args, "status": tool, "decision": tool}


def done(summary: str, files: list[str], tests: list[str] | None = None) -> dict[str, Any]:
    return act("complete_step", summary=summary, changed_files=files, tests_run=tests or [])


def write_file(path: str, content: str) -> dict[str, Any]:
    return act("write_file", path=path, content=content)


def replace(path: str, old: str, new: str) -> dict[str, Any]:
    return act("replace_text", path=path, old=old, new=new)


@dataclass
class Scenario:
    name: str
    title: str
    prompt: str
    seed: dict[str, str] | None  # None = job without repository
    plans: list[Any]  # Gemma answers in order: plan, then replans
    coder: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # step_key -> actions (then complete)
    triage: dict[str, Any] = field(default_factory=lambda: {"intent": "code_change"})
    review: dict[str, Any] = field(default_factory=lambda: {"verdict": "pass", "summary": "ok", "findings": []})
    media_specs: list[dict[str, Any]] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)  # extra ServiceOverrides fields
    settings: dict[str, Any] = field(default_factory=dict)  # extra Settings fields
    expect: str = "succeeded"
    timeout: float = 180.0


class ScenarioModels(ScriptedChat):
    """Gemma = chat (plans/replans in order); fast router, Qwen3-Coder, heavy reviewer = structured by alias."""

    def __init__(self, sc: Scenario, sessionmaker: Any) -> None:
        super().__init__(list(sc.plans))
        self.sc, self.sm = sc, sessionmaker
        self.coder_queue = {k: list(v) for k, v in sc.coder.items()}
        self.media_specs = list(sc.media_specs)
        self.purposes: list[str] = []
        self.coder_turns: dict[str, int] = {}

    async def _step_key(self, step_id: uuid.UUID | None) -> str:
        if step_id is None:
            return "?"
        async with self.sm() as s:
            return str((await s.execute(select(Step.step_key).where(Step.id == step_id))).scalar_one_or_none() or "?")

    async def structured(  # type: ignore[override]
        self, alias: str, messages: list[ChatMessage], schema: type[T], *, ctx: CallContext, **_: Any
    ) -> StructuredResult[T]:
        self.purposes.append(f"{alias}:{ctx.purpose}")
        if alias == "fast-router" and ctx.purpose == "media_spec":
            data = self.media_specs.pop(0)
        elif alias == "fast-router":
            data = self.sc.triage
        elif alias == "heavy-review":
            data = self.sc.review
        else:
            assert alias == "coder-main", alias
            key = await self._step_key(ctx.step_id)
            self.coder_turns[key] = self.coder_turns.get(key, 0) + 1
            queue = self.coder_queue.get(key) or []
            data = queue.pop(0) if queue else done(f"{key} complete", [])
        return StructuredResult(
            value=schema.model_validate(data), result=ChatResult(content=json.dumps(data), alias=alias, model=f"scripted:{alias}")
        )


@dataclass
class Outcome:
    job: Job
    steps: dict[str, Step]
    attempts: list[StepAttempt]
    events: list[Event]
    artifacts: list[Artifact]
    report: str
    models: ScenarioModels
    upstream: Path | None
    root: Path

    def remote_file(self, path: str) -> str:
        assert self.upstream is not None
        branch = git("branch", "--list", "hermclaw/*", cwd=self.upstream).strip().lstrip("* ").strip()
        return git("show", f"{branch}:{path}", cwd=self.upstream)

    def commits(self) -> list[str]:
        assert self.upstream is not None
        branch = git("branch", "--list", "hermclaw/*", cwd=self.upstream).strip().lstrip("* ").strip()
        return git("log", "--format=%s", f"main..{branch}", cwd=self.upstream).splitlines() if branch else []

    def event_types(self) -> list[str]:
        return [e.event_type for e in self.events]


def make_upstream(root: Path, files: dict[str, str]) -> Path:
    seed = root / "seed"
    seed.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(seed), cwd=root)
    for rel, content in files.items():
        write(seed, rel, content)
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "initial", cwd=seed)
    bare = root / "remote.git"
    git("clone", "-q", "--bare", str(seed), str(bare), cwd=root)
    return bare


async def subprocess_executor(handle: WorkspaceHandle, step: StepContract | None) -> SubprocessExecutor:
    cfg = get_config()
    return SubprocessExecutor(cfg.policies.sandbox, settings=Settings(env="test"))


async def run_scenario(
    sessionmaker: Any,
    sc: Scenario,
    root: Path,
    *,
    before_start: Callable[[uuid.UUID], Awaitable[None]] | None = None,
    during: Callable[[uuid.UUID], Awaitable[None]] | None = None,
) -> Outcome:
    async with sessionmaker() as s:  # the DB is shared per test session: neutralise other tests' open jobs
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        await s.commit()
    settings = Settings(
        **{
            "env": "test",
            "data_dir": root / "data",
            "instance_id": f"e2e-{uuid.uuid4().hex[:8]}",
            "execution_mode": "local",
            "wake_on_lan": False,
            **sc.settings,
        }
    )
    upstream = None
    repo_id = None
    if sc.seed is not None:
        upstream = make_upstream(root / "upstream", sc.seed)
        engine = GitEngine(sessionmaker, settings=settings, policies=PoliciesConfig())
        repo = await engine.registry.register(f"e2e/{sc.name}-{uuid.uuid4().hex[:6]}", f"file://{upstream}", provider="generic")
        repo_id = repo.id
    models = ScenarioModels(sc, sessionmaker)
    ov: dict[str, Any] = {
        "chat": models,
        "research_enabled": False,
        "gitlab": None,
        "executor_factory": subprocess_executor,
        "lease_keeper_interval": 0.2,
        **sc.overrides,
    }
    proc = SchedulerProcess(
        get_config(),
        settings,
        overrides=ServiceOverrides(**ov),
        scheduler_settings=SchedulerSettings(poll_seconds=0.05, retry_backoff_seconds=(0,)),
    )
    async with sessionmaker() as s:
        job = Job(title=sc.title, prompt=sc.prompt, repository_id=repo_id)
        s.add(job)
        await s.commit()
        job_id = job.id
    if before_start is not None:
        await before_start(job_id)
    task = asyncio.create_task(proc.run())
    try:
        async with asyncio.timeout(sc.timeout):
            while True:
                async with sessionmaker() as s:
                    status = (await s.execute(select(Job.status).where(Job.id == job_id))).scalar_one()
                if status in TERMINAL:
                    break
                if during is not None:
                    await during(job_id)
                if task.done():
                    task.result()  # surface a crash of the scheduler process
                    raise AssertionError("scheduler process exited before the job finished")
                await asyncio.sleep(0.1)
    finally:
        proc.stop()
        await asyncio.wait_for(task, timeout=90)
    async with sessionmaker() as s:
        job_row = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        steps = {r.step_key: r for r in (await s.execute(select(Step).where(Step.job_id == job_id, Step.superseded.is_(False)))).scalars()}
        attempts = list(
            (await s.execute(select(StepAttempt).where(StepAttempt.job_id == job_id).order_by(StepAttempt.started_at))).scalars()
        )
        events = list((await s.execute(select(Event).where(Event.job_id == job_id).order_by(Event.sequence))).scalars())
        artifacts = list((await s.execute(select(Artifact).where(Artifact.job_id == job_id))).scalars())
    report = ""
    for a in artifacts:
        if a.kind == "report" and Path(a.path).is_file():
            report = Path(a.path).read_text(encoding="utf-8")
    out = Outcome(job_row, steps, attempts, events, artifacts, report, models, upstream, root)
    assert job_row.status == sc.expect, (
        f"{sc.name}: job {job_row.status} ({job_row.error_code}: {job_row.error_message}); "
        f"steps {[(k, v.status, v.error_code, (v.error_message or '')[:300]) for k, v in steps.items()]}"
    )
    return out


__all__ = [
    "Outcome",
    "Scenario",
    "ScenarioModels",
    "act",
    "done",
    "make_upstream",
    "replace",
    "run_scenario",
    "subprocess_executor",
    "write_file",
]
