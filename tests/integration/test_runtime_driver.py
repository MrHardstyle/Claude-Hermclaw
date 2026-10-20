"""Runtime job driver end-to-end against real PostgreSQL, real git (file:// remote), real planner/replanner code
and the real scheduler. Only the models (scripted Gemma/fast router) and the step work itself are fakes."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update

from hermclaw.api import jobs as jobs_api
from hermclaw.api.auth import Principal
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core import job_metadata
from hermclaw.core.config import get_config
from hermclaw.core.errors import ModelError
from hermclaw.core.interfaces import RepoHit, WorkspaceHandle
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.persistence.models import Artifact, Event, Job, Step, VerificationRun
from hermclaw.planner import Planner, Replanner
from hermclaw.runtime.driver import DriverSettings, RuntimeJobDriver, TriageResult, failed_step_keys, replan_reason_code
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

    async def search(self, workspace: WorkspaceHandle, query: str, *, k: int = 20) -> list[RepoHit]:
        return []

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]:
        return []

    async def read(self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000) -> str:
        return Path(workspace.path, path).read_text(encoding="utf-8")[:max_chars]

    async def inventory_summary(self, workspace: WorkspaceHandle) -> dict[str, Any]:
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

    def __init__(
        self, world: World, content: str = "VALUE = 2\n", on_done: Any = None, outcome: str = "completed", blocks: int = 1
    ) -> None:
        self.world, self.content, self.on_done, self.outcome, self.blocks = world, content, on_done, outcome, blocks
        self.calls = 0

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        self.calls += 1
        if self.outcome == "blocked" and self.calls <= self.blocks:
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


async def _job(
    world: World, *, repo: bool = True, title: str = "Set VALUE to two", prompt: str = "Change VALUE in src/module.py to 2."
) -> uuid.UUID:
    async with world.sessionmaker() as s:
        job = Job(title=title, prompt=prompt, repository_id=world.repo.id if repo else None)
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
    world: World,
    driver: RuntimeJobDriver,
    handlers: list[Any],
    job_ids: list[uuid.UUID],
    timeout: float = 60,
    terminal: tuple[str, ...] = TERMINAL,
    **settings: Any,
) -> dict[uuid.UUID, Job]:
    sched = Scheduler(
        world.sessionmaker,
        get_config(),
        handlers,
        driver,
        settings=SchedulerSettings(poll_seconds=0.01, retry_backoff_seconds=(0,), **settings),
    )
    async with asyncio.timeout(timeout):
        while True:
            await sched.tick()
            async with world.sessionmaker() as s:
                jobs = {j.id: j for j in (await s.execute(select(Job).where(Job.id.in_(job_ids)))).scalars()}
            if all(j.status in terminal for j in jobs.values()) and not sched._job_tasks:
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


async def test_failed_regression_fails_job_but_delivers_a_draft(world: World, tmp_path: Path) -> None:
    """The job stays failed, but its branch is pushed instead of being lost (BUG-210)."""
    chat = Chat([_plan()])
    driver = _driver(world, chat, tmp_path, regression=FakeRegression(ok=False))
    jid = await _job(world)
    jobs = await _run(
        world, driver, [ImplementHandler(world, on_done=lambda: world.upstream.commit({"docs/guide.md": "x\n"})), ReviewHandler()], [jid]
    )
    assert jobs[jid].status == "failed" and jobs[jid].error_code == "REGRESSION_FAILED"
    assert git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare).strip() != ""
    assert "failed (REGRESSION_FAILED)" in await _report(world, jid)


async def test_conflicting_base_change_delivers_the_work_on_its_old_base(world: World, tmp_path: Path) -> None:
    """A base change that conflicts with a finished job failed it with MERGE_CONFLICT and nothing was pushed: hours of
    verified work were lost for a merge decision the runtime never makes. The branch is pushed on its old base and the
    report names the conflict (BUG-210)."""
    chat = Chat([_plan()])
    driver = _driver(world, chat, tmp_path)
    jid = await _job(world)
    conflict = lambda: world.upstream.commit({"src/module.py": "VALUE = 99\n"})  # noqa: E731
    jobs = await _run(world, driver, [ImplementHandler(world, on_done=conflict), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert "Base-Konflikt" in (jobs[jid].result_summary or "")
    branch = git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare).strip().lstrip("* ").strip()
    assert branch, "the job branch must be pushed"
    assert git("show", f"{branch}:src/module.py", cwd=world.upstream.bare) != "VALUE = 99"
    report = await _report(world, jid)
    assert "Base-Konflikt" in report and "src/module.py" in report


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


class RecordingLeases:
    """Model leases that record which profile is held while the model is called."""

    def __init__(self) -> None:
        self.held: list[str] = []
        self.calls: list[tuple[str, list[str]]] = []

    @asynccontextmanager
    async def hold_model(self, profile: Any, **_: Any) -> AsyncIterator[None]:
        self.held.append(profile.role)
        try:
            yield
        finally:
            self.held.pop()


class LeaseCheckingChat(Chat):
    def __init__(self, answers: list[Any], leases: RecordingLeases) -> None:
        super().__init__(answers)
        self.leases = leases

    async def chat(self, alias: str, messages: list[ChatMessage], *, ctx: CallContext, **kw: Any) -> ChatResult:  # type: ignore[override]
        self.leases.calls.append((ctx.purpose, list(self.leases.held)))
        return await super().chat(alias, messages, ctx=ctx, **kw)


async def test_planner_and_replanner_hold_the_planner_model_lease(world: World, tmp_path: Path) -> None:
    """R-033: the planner runs on another model of the exclusive large-model group (a router-mode llama-server swaps
    models), so a plan or replan call holds the planner's lease – it never switches the model under a running coder
    step of another job."""
    leases = RecordingLeases()
    chat = LeaseCheckingChat([_plan(), _replan()], leases)
    cfg = get_config()
    driver = RuntimeJobDriver(
        world.sessionmaker,
        cfg,
        planner=Planner(chat, world.sessionmaker, cfg),
        replanner=Replanner(chat, world.sessionmaker, cfg),
        git=world.engine,
        repo_intel=FakeIntel(),
        chat=chat,
        settings=DriverSettings(artifacts_dir=tmp_path / "artifacts"),
        resources=leases,
    )
    jid = await _job(world)
    jobs = await _run(world, driver, [ImplementHandler(world, outcome="blocked"), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert [held for _, held in leases.calls] == [["planner"], ["planner"]], leases.calls
    assert leases.held == []


async def test_replanner_question_makes_the_job_wait_and_the_answer_resumes_it(world: World, tmp_path: Path) -> None:
    """rc16 battery J4: the job demanded exactly a package that does not exist; the replanner planned a workaround
    against the job (remove an import that was never there) and burnt the replan budget. Now it asks the user: the job
    waits without using the budget, and the answer (POST /replan with the reason) resumes it (BUG-187)."""
    question = {
        "goal": "Set VALUE to 2",
        "steps": [],
        "user_question": "The required package does not exist. Which package should be used?",
    }
    chat = Chat([_plan(), question, _replan()])
    driver = _driver(world, chat, tmp_path)
    jid = await _job(world)
    handlers = [ImplementHandler(world, outcome="blocked"), ReviewHandler()]
    jobs = await _run(world, driver, handlers, [jid], terminal=(*TERMINAL, "waiting_for_user"))
    job = jobs[jid]
    assert (job.status, job.replan_count, job.current_plan_version) == ("waiting_for_user", 0, 1)
    assert job.error_code == "USER_INPUT_REQUIRED" and "Which package" in (job.error_message or "")
    asked = [e.payload for e in await _events(world, jid) if e.event_type == "replan.user_question"]
    assert asked and asked[0]["question"].startswith("The required package")
    async with world.sessionmaker() as s:
        await jobs_api._control(s, jid, "replan", Principal("operator", frozenset({"control"})), reason="Use the standard library.")
        # a human-requested replan is never refused for an exhausted budget (BUG-201)
        meta = (await s.execute(select(Job.metadata_).where(Job.id == jid))).scalar_one()
        assert meta[job_metadata.REPLAN_BUDGET_BASE] == 0
    jobs = await _run(world, driver, handlers, [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert jobs[jid].replan_count == 1 and jobs[jid].error_code is None
    assert "Use the standard library." in str(chat.user_payload(2))


async def test_a_confirmation_question_next_to_a_plan_does_not_stop_the_job(world: World, tmp_path: Path) -> None:
    """rc22 battery X1: the replanner returned a complete plan AND asked "please confirm if this approach is
    acceptable"; the job stopped in waiting_for_user although nothing needed the user. A question next to steps is a
    confirmation request: the steps run and the question is kept as a plan note (BUG-208)."""
    replan = _replan()
    replan["user_question"] = "Please confirm if this approach is acceptable."
    chat = Chat([_plan(), replan])
    driver = _driver(world, chat, tmp_path)
    jid = await _job(world)
    jobs = await _run(world, driver, [ImplementHandler(world, outcome="blocked"), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert jobs[jid].replan_count == 1
    events = await _events(world, jid)
    assert not [e for e in events if e.event_type == "replan.user_question"]
    created = [e.payload for e in events if e.event_type == "replan.created"]
    assert created and "Please confirm" in str(created[0])


async def test_retry_after_the_replan_limit_continues_with_a_fresh_budget(world: World, tmp_path: Path) -> None:
    """A job that failed with REPLAN_LIMIT came back from POST /retry only to fail again at once: the blocked step
    stayed blocked and the used-up budget was checked again. A long job could never be continued (BUG-189)."""
    second = _replan()
    for st in second["steps"]:
        st["id"] = {"S003": "S005", "S004": "S006"}[st["id"]]
        st["goal"] += " Use the granted scope once more."
        st["depends_on"] = [{"S003": "S005"}.get(d, d) for d in st.get("depends_on", [])]
    chat = Chat([_plan(), _replan(), second])
    driver = _driver(world, chat, tmp_path)
    jid = await _job(world)
    handlers = [ImplementHandler(world, outcome="blocked", blocks=3), ReviewHandler()]
    jobs = await _run(world, driver, handlers, [jid])
    assert (jobs[jid].status, jobs[jid].error_code, jobs[jid].replan_count) == ("failed", "REPLAN_LIMIT", 2)
    async with world.sessionmaker() as s:
        out = await jobs_api._control(s, jid, "retry", Principal("operator", frozenset({"control"})))
    assert out.status == "queued" and "2 failed/blocked step(s)" in out.detail  # S005 and its review
    jobs = await _run(world, driver, handlers, [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert jobs[jid].replan_count == 2 and len(chat.calls) == 3  # continued without another replan


async def test_triage_failure_is_not_fatal_and_job_without_repository(world: World, tmp_path: Path) -> None:
    research_only = plan(
        "Compare reverse proxy options",
        [step("S001", "research", "research", "Research current TLS defaults of nginx and Caddy with sources.", network=True)],
    )
    chat = Chat([research_only], triage=ModelError("router down", code="MODEL_UNAVAILABLE"))
    driver = _driver(world, chat, tmp_path)
    jid = await _job(world, repo=False, title="Proxy research", prompt="Research the current TLS defaults of nginx and Caddy.")
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


async def test_the_planner_gets_the_active_knowledge_of_this_repository(world: World, tmp_path: Path) -> None:
    """D-019: planning a job looks up the active knowledge entries closest to the job text – global ones and those of
    this repository – and gives them to the planner; a candidate or another repository's entry is not given."""
    from sqlalchemy import delete

    from hermclaw.core.config import MemoryPolicy
    from hermclaw.memory.store import MemoryStore
    from hermclaw.persistence.models import MemoryEntry
    from tests.integration.test_repo_intelligence_support import HashingEmbedder

    async with world.sessionmaker() as s:
        await s.execute(delete(MemoryEntry))
        await s.commit()
    store = MemoryStore(world.sessionmaker, HashingEmbedder(), MemoryPolicy(min_score=0.1))
    await store.add(kind="lesson", title="VALUE constants", content="change VALUE in module.py, keep its tests", status="active")
    await store.add(kind="lesson", title="VALUE unproven", content="change VALUE in module.py", status="candidate")
    await store.add(kind="convention", title="VALUE elsewhere", content="VALUE module.py", status="active", scope="repo:other")
    chat = Chat([_plan()])
    driver = _driver(world, chat, tmp_path)
    driver.memory = store
    jid = await _job(world)
    await _run(world, driver, [ImplementHandler(world), ReviewHandler()], [jid])
    planner_prompt = chat.calls[0].messages[-1].content
    assert "[lesson] VALUE constants" in planner_prompt
    assert "unproven" not in planner_prompt and "elsewhere" not in planner_prompt


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


async def test_failed_step_keys_put_the_cause_before_its_dependents() -> None:
    """BUG-156 (live job eadf1b6f): S002 blocked with CODER_REQUESTED_REPLAN, S003/S004 blocked with
    DEPENDENCY_FAILED; the replan trigger named S004 as the failed step."""
    ev = {
        "failed_steps": [
            {"step_key": "S004", "status": "blocked", "error_code": "DEPENDENCY_FAILED"},
            {"step_key": "S003", "status": "blocked", "error_code": "DEPENDENCY_FAILED"},
            {"step_key": "S002", "status": "blocked", "error_code": "CODER_REQUESTED_REPLAN"},
            {"status": "blocked", "error_code": "X"},
        ]
    }
    assert failed_step_keys(ev) == ["S002", "S004", "S003"]
    ev["failed_steps"].append({"step_key": "S009", "status": "failed", "error_code": "VERIFIER_FAILED"})
    assert failed_step_keys(ev)[0] == "S009"
    assert failed_step_keys({}) == []


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


async def _tournament_job(world: World, **settings: Any) -> uuid.UUID:
    meta = {job_metadata.TOURNAMENT: {"plan": _plan(), "coder_alias": "coder-main", "task": "T1", "run": "coder-main/T1/1", **settings}}
    async with world.sessionmaker() as s:
        job = Job(title="Set VALUE to two", prompt="Change VALUE in src/module.py to 2.", repository_id=world.repo.id, metadata_=meta)
        s.add(job)
        await s.commit()
        return job.id


async def test_a_tournament_job_adopts_its_plan_without_a_model_and_is_not_pushed(world: World, tmp_path: Path) -> None:
    """Coder tournament (2026-10-20): every candidate coder gets the identical prescribed plan. The driver adopts it
    through the planner's deterministic check – no triage or planner model is loaded – and the result stays in the work
    tree for the hidden checks instead of going to GitLab."""
    chat = Chat([])  # any planner or triage call would fail the test
    driver = _driver(world, chat, tmp_path)
    jid = await _tournament_job(world)
    jobs = await _run(world, driver, [ImplementHandler(world), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert chat.triage_calls == 0 and chat.calls == []
    created = [e for e in await _events(world, jid) if e.event_type == "planner.plan.created"]
    assert created[0].payload["source"] == "prescribed" and created[0].payload["steps"] == ["S001", "S002"]
    assert "git.pushed" not in [e.event_type for e in await _events(world, jid)]
    assert not git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare).strip()
    assert "Coder-Turnier" in await _report(world, jid)


async def test_a_tournament_job_gets_no_replan(world: World, tmp_path: Path) -> None:
    """A replan would bring in the planner model; a tournament run measures the coder, so a blocked step ends the job."""
    chat = Chat([])
    driver = _driver(world, chat, tmp_path)
    jid = await _tournament_job(world)
    jobs = await _run(world, driver, [ImplementHandler(world, outcome="blocked"), ReviewHandler()], [jid])
    assert (jobs[jid].status, jobs[jid].error_code, jobs[jid].replan_count) == ("failed", "REPLAN_LIMIT", 0)
    assert chat.calls == []
