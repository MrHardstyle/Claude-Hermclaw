"""Runtime job driver end-to-end against real PostgreSQL, real git (file:// remote), real planner/replanner code
and the real scheduler. Only the models (scripted Gemma/fast router) and the step work itself are fakes."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update

from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import get_config
from hermclaw.core.errors import ModelError
from hermclaw.core.interfaces import RepoHit, WorkspaceHandle
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.persistence.models import Artifact, Event, Job, Step, VerificationRun
from hermclaw.planner import Planner, Replanner
from hermclaw.runtime.driver import DriverSettings, RuntimeJobDriver, TriageResult, replan_reason_code
from hermclaw.scheduler import Scheduler, SchedulerSettings, StepOutcome, StepRunContext
from tests.integration.test_gitops_support import SEED_FILES, World, git, make_world, write
from tests.unit.test_planner_support import ScriptedChat, plan, step

pytestmark = pytest.mark.asyncio(loop_scope="session")
T = TypeVar("T", bound=BaseModel)
TERMINAL = ("succeeded", "failed", "cancelled")


# ----------------------------------------------------------------------------- fakes (test code only)
class Chat(ScriptedChat):
    """Scripted Gemma answers (chat) plus a scripted fast-router triage (structured)."""

    def __init__(self, answers: list[Any], triage: dict[str, Any] | Exception | None = None) -> None:
        super().__init__(answers)
        self.triage = triage
        self.triage_calls = 0

    async def structured(  # type: ignore[override]
        self, alias: str, messages: list[ChatMessage], schema: type[T], *, ctx: CallContext, **_: Any
    ) -> StructuredResult[T]:
        self.triage_calls += 1
        assert alias == "fast-router" and ctx.purpose == "triage"
        if isinstance(self.triage, Exception):
            raise self.triage
        value = schema.model_validate(self.triage or {"intent": "code_change", "summary": "small code change"})
        return StructuredResult(value=value, result=ChatResult(content="{}", alias=alias, model="qwen3:8b"))


class FakeIntel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def inventory(self, workspace: WorkspaceHandle, *, job_id: uuid.UUID | None = None) -> dict[str, Any]:
        self.calls.append("inventory")
        assert Path(workspace.path, "src/module.py").is_file()
        return {
            "languages": ["python"],
            "files": [{"path": p} for p in SEED_FILES],
            "tests": {"framework": "pytest", "command": "pytest -q"},
        }

    async def context_for(
        self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int, job_id: uuid.UUID | None = None
    ) -> list[RepoHit]:
        self.calls.append("context")
        return [RepoHit(path="src/module.py", start_line=1, end_line=1, score=0.9, snippet="VALUE = 1\n")]


class FakeRegression:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls = 0

    async def rerun(self, job_id: uuid.UUID, workspace: WorkspaceHandle) -> tuple[bool, dict[str, Any]]:
        self.calls += 1
        return self.ok, {"checks": 1}


class ImplementHandler:
    """Writes the change, stages within scope, records a passed verification and lets the runtime commit."""

    kinds = frozenset({"implement"})

    def __init__(self, world: World, content: str = "VALUE = 2\n", on_done: Any = None, outcome: str = "completed") -> None:
        self.world, self.content, self.on_done, self.outcome = world, content, on_done, outcome
        self.calls = 0

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        self.calls += 1
        if self.outcome == "blocked" and self.calls == 1:
            return StepOutcome("blocked", error_code="SCOPE_UNAVAILABLE", replan_reason="scope_unavailable", replan_evidence={"path": "x"})
        eng = self.world.engine
        ws = (await eng.list_workspaces(ctx.job_id))[0]
        write(Path(ws.path), "src/module.py", self.content)
        await eng.stage_allowed(ws, ScopeContract(target_paths=["src/module.py"]), step_id=ctx.step_id)
        async with ctx.sessionmaker() as s:
            vr = VerificationRun(
                job_id=ctx.job_id, step_id=ctx.step_id, passed=True, status="passed", summary="presence ok", changed_files=["src/module.py"]
            )
            s.add(vr)
            await s.commit()
        await eng.commit_verified(ws, f"{ctx.step_key}: set VALUE", vr.id, step_id=ctx.step_id)
        if self.on_done:
            self.on_done()
        return StepOutcome("completed", summary="VALUE updated")


class ResearchHandler:
    kinds = frozenset({"research"})

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        return StepOutcome("completed", summary="sources collected")


class ReviewHandler:
    kinds = frozenset({"review"})

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        return StepOutcome("completed", summary="review pass")


def _plan() -> dict[str, Any]:
    return plan(
        "Set VALUE to 2",
        [
            step(
                "S001",
                "implement",
                "coding",
                "Change VALUE in src/module.py from 1 to 2.",
                repo_hints=["src/module.py"],
                acceptance=[{"type": "presence", "path_glob": "src/module.py", "pattern": "VALUE = 2"}],
            ),
            step("S002", "review", "review", "Review the VALUE change.", depends_on=["S001"]),
        ],
    )


def _replan() -> dict[str, Any]:
    return plan(
        "Set VALUE to 2",
        [
            step(
                "S003",
                "implement",
                "coding",
                "Change VALUE in src/module.py from 1 to 2 using the granted scope.",
                repo_hints=["src/module.py"],
                acceptance=[{"type": "presence", "path_glob": "src/module.py", "pattern": "VALUE = 2"}],
            ),
            step("S004", "review", "review", "Review the VALUE change.", depends_on=["S003"]),
        ],
        summary="replanned after scope problem",
    )


# ----------------------------------------------------------------------------- harness
@pytest.fixture
async def clean_jobs(sessionmaker: Any) -> None:
    async with sessionmaker() as s:
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        await s.commit()


@pytest.fixture
async def world(sessionmaker: Any, tmp_path: Path, clean_jobs: None) -> World:
    return await make_world(sessionmaker, tmp_path)


async def _job(world: World, *, repo: bool = True, title: str = "Set VALUE to two") -> uuid.UUID:
    async with world.sessionmaker() as s:
        job = Job(title=title, prompt="Change VALUE in src/module.py to 2.", repository_id=world.repo.id if repo else None)
        s.add(job)
        await s.commit()
        return job.id


def _driver(world: World, chat: Chat, tmp_path: Path, **kw: Any) -> RuntimeJobDriver:
    cfg = get_config()
    return RuntimeJobDriver(
        world.sessionmaker,
        cfg,
        planner=Planner(chat, world.sessionmaker, cfg),
        replanner=Replanner(chat, world.sessionmaker, cfg),
        git=world.engine,
        repo_intel=kw.pop("repo_intel", FakeIntel()),
        chat=chat,
        regression=kw.pop("regression", None),
        settings=DriverSettings(artifacts_dir=tmp_path / "artifacts", **kw),
    )


async def _run(
    world: World, driver: RuntimeJobDriver, handlers: list[Any], job_ids: list[uuid.UUID], timeout: float = 60
) -> dict[uuid.UUID, Job]:
    sched = Scheduler(
        world.sessionmaker, get_config(), handlers, driver, settings=SchedulerSettings(poll_seconds=0.01, retry_backoff_seconds=(0,))
    )
    async with asyncio.timeout(timeout):
        while True:
            await sched.tick()
            async with world.sessionmaker() as s:
                jobs = {j.id: j for j in (await s.execute(select(Job).where(Job.id.in_(job_ids)))).scalars()}
            if all(j.status in TERMINAL for j in jobs.values()) and not sched._job_tasks:
                await sched.wait_idle()
                return jobs
            await asyncio.sleep(0.02)


async def _events(world: World, job_id: uuid.UUID) -> list[Event]:
    async with world.sessionmaker() as s:
        return list((await s.execute(select(Event).where(Event.job_id == job_id).order_by(Event.sequence))).scalars())


async def _report(world: World, job_id: uuid.UUID) -> str:
    async with world.sessionmaker() as s:
        art = (await s.execute(select(Artifact).where(Artifact.job_id == job_id, Artifact.kind == "report"))).scalar_one()
    return Path(art.path).read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- tests
async def test_end_to_end_prepare_run_finalize_pushes_and_reports(world: World, tmp_path: Path) -> None:
    chat = Chat([_plan()])
    intel = FakeIntel()
    driver = _driver(world, chat, tmp_path, repo_intel=intel)
    jid = await _job(world)
    jobs = await _run(world, driver, [ImplementHandler(world), ReviewHandler()], [jid])
    job = jobs[jid]
    assert job.status == "succeeded", (job.error_code, job.error_message)
    assert intel.calls == ["inventory", "context"] and chat.triage_calls == 1
    assert job.metadata_["triage"]["intent"] == "code_change"
    # the planner saw the inventory paths and the retrieved snippet
    payload = chat.user_payload(0)
    assert "src/module.py" in str(payload)
    # job branch pushed to the bare remote, base untouched
    branches = git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare)
    assert branches.strip(), "job branch missing on remote"
    branch = branches.strip().lstrip("* ").strip()
    assert git("show", f"{branch}:src/module.py", cwd=world.upstream.bare) == "VALUE = 2"
    assert git("show", "main:src/module.py", cwd=world.upstream.bare) == "VALUE = 1"
    # final report artifact + summary
    text = await _report(world, jid)
    assert "Abschlussbericht" in text and "S001" in text and "Commit" in text and "Triage" in text
    assert job.result_summary and "final_report.md" in job.result_summary
    types = [e.event_type for e in await _events(world, jid)]
    for t in ("planner.plan.created", "git.commit.created", "git.pushed", "job.succeeded"):
        assert t in types, t
    states = [e.payload["to"] for e in await _events(world, jid) if e.event_type == "job.transition"]
    assert states[:4] == ["inventory", "discovering", "planning", "running"]
    assert "committing" in states and states[-1] == "succeeded"


async def test_base_moved_during_job_is_rebased_and_regression_rerun(world: World, tmp_path: Path) -> None:
    chat = Chat([_plan()])
    regression = FakeRegression()
    driver = _driver(world, chat, tmp_path, regression=regression)
    jid = await _job(world)
    moved = lambda: world.upstream.commit({"docs/guide.md": "guide v2\n"})  # noqa: E731 - unrelated upstream change
    jobs = await _run(world, driver, [ImplementHandler(world, on_done=moved), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", jobs[jid].error_message
    assert regression.calls == 1
    branch = git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare).strip().lstrip("* ").strip()
    assert git("show", f"{branch}:docs/guide.md", cwd=world.upstream.bare) == "guide v2"
    assert "Base aktualisiert" in await _report(world, jid)


async def test_failed_regression_fails_job_without_push(world: World, tmp_path: Path) -> None:
    chat = Chat([_plan()])
    driver = _driver(world, chat, tmp_path, regression=FakeRegression(ok=False))
    jid = await _job(world)
    jobs = await _run(
        world, driver, [ImplementHandler(world, on_done=lambda: world.upstream.commit({"docs/guide.md": "x\n"})), ReviewHandler()], [jid]
    )
    assert jobs[jid].status == "failed" and jobs[jid].error_code == "REGRESSION_FAILED"
    assert git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare).strip() == ""
    assert "failed (REGRESSION_FAILED)" in await _report(world, jid)


async def test_conflicting_base_change_fails_with_merge_conflict(world: World, tmp_path: Path) -> None:
    chat = Chat([_plan()])
    driver = _driver(world, chat, tmp_path)
    jid = await _job(world)
    conflict = lambda: world.upstream.commit({"src/module.py": "VALUE = 99\n"})  # noqa: E731
    jobs = await _run(world, driver, [ImplementHandler(world, on_done=conflict), ReviewHandler()], [jid])
    assert jobs[jid].status == "failed" and jobs[jid].error_code == "MERGE_CONFLICT"
    assert "MERGE_CONFLICT" in await _report(world, jid)


async def test_blocked_step_triggers_gemma_replan_and_job_completes(world: World, tmp_path: Path) -> None:
    chat = Chat([_plan(), _replan()])
    driver = _driver(world, chat, tmp_path)
    jid = await _job(world)
    jobs = await _run(world, driver, [ImplementHandler(world, outcome="blocked"), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert jobs[jid].replan_count == 1, "replan must be counted exactly once"
    async with world.sessionmaker() as s:
        active = {
            st.step_key: st.status for st in (await s.execute(select(Step).where(Step.job_id == jid, Step.superseded.is_(False)))).scalars()
        }
    assert active == {"S003": "completed", "S004": "completed"}
    created = [e for e in await _events(world, jid) if e.event_type == "replan.created"]
    assert created, "replan.created event missing"
    replan_call = chat.user_payload(1)
    assert "scope_unavailable" in str(replan_call)


async def test_triage_failure_is_not_fatal_and_job_without_repository(world: World, tmp_path: Path) -> None:
    research_only = plan(
        "Compare reverse proxy options",
        [step("S001", "research", "research", "Research current TLS defaults of nginx and Caddy with sources.", network=True)],
    )
    chat = Chat([research_only], triage=ModelError("router down", code="MODEL_UNAVAILABLE"))
    driver = _driver(world, chat, tmp_path)
    jid = await _job(world, repo=False, title="Proxy research")
    jobs = await _run(world, driver, [ResearchHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    texts = [e.payload.get("text", "") for e in await _events(world, jid) if e.event_type == "status"]
    assert any("Triage nicht verfügbar" in t for t in texts)
    states = [e.payload["to"] for e in await _events(world, jid) if e.event_type == "job.transition"]
    assert "inventory" not in states  # no repository -> no workspace phase
    assert "keine Commits" in await _report(world, jid)


async def test_invalid_planner_output_fails_preparation(world: World, tmp_path: Path) -> None:
    chat = Chat(["not json", "still not json", "{}"])
    driver = _driver(world, chat, tmp_path, triage_enabled=False)
    jid = await _job(world)
    jobs = await _run(world, driver, [], [jid])
    assert jobs[jid].status == "failed" and jobs[jid].error_code == "PLANNER_INVALID_OUTPUT"


async def test_prepare_is_reentrant_after_crash(world: World, tmp_path: Path) -> None:
    """A crash after planning (job still 'planning', plan stored) must not plan twice."""
    chat = Chat([_plan()])
    driver = _driver(world, chat, tmp_path)
    jid = await _job(world)
    from hermclaw.scheduler import CancelToken

    first = await driver.prepare(jid, CancelToken())
    assert first.ok
    again = await driver.prepare(jid, CancelToken())  # no scripted answer left: would raise if it planned again
    assert again.ok and again.next_status == "running"
    assert len(await world.engine.list_workspaces(jid)) == 1


async def test_replan_reason_mapping() -> None:
    def ev(*codes: str, rr: str | None = None) -> dict[str, Any]:
        return {
            "failed_steps": [
                {"step_key": f"S{i}", "status": "failed", "error_code": c, "result": {"replan_reason": rr} if rr else {}}
                for i, c in enumerate(codes)
            ]
        }

    assert replan_reason_code("operator wants X", ev("BROKEN")) == "operator_request"
    assert replan_reason_code("blocked_or_failed_steps", ev("X", rr="stagnation")) == "stagnation"
    assert replan_reason_code("blocked_or_failed_steps", ev("DEPENDENCY_FAILED", "SCOPE_UNAVAILABLE")) == "scope_unavailable"
    assert replan_reason_code("blocked_or_failed_steps", ev("VERIFIER_FAILED")) == "repeated_verifier_failure"
    assert replan_reason_code("blocked_or_failed_steps", ev("NO_HANDLER")) == "worker_unavailable"
    assert replan_reason_code("blocked_or_failed_steps", ev("MERGE_CONFLICT")) == "repository_changed"
    assert replan_reason_code("blocked_or_failed_steps", ev("DEPENDENCY_FAILED")) == "missing_dependency"
    assert replan_reason_code("blocked_or_failed_steps", ev("BROKEN")) == "step_failed"
    assert replan_reason_code("blocked_or_failed_steps", ev("X", rr="not-a-reason")) == "step_failed"
    assert TriageResult.model_validate({"intent": "media"}).risk == "medium"
