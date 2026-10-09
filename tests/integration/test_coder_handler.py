"""Implement step handler + correction pipeline (P19/P23) end-to-end through the scheduler and runtime driver.

Real: PostgreSQL, git (file:// remote), GitEngine, ScopeEngine/ScopeAuditor/ScopeExpansionHandler, ToolEngine with a
subprocess executor, ContextBuilder, Planner/Replanner, Scheduler, RuntimeJobDriver. Scripted: the models, the
deterministic verifier and the heavy reviewer (their own components are tested separately)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, ClassVar, TypeVar

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update

from hermclaw.coder.handler import ImplementDeps, ImplementStepHandler, VerificationOutcome
from hermclaw.contracts.review import ReviewContract, ReviewFinding
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.verification import VerificationCheck, VerificationReport
from hermclaw.core.config import get_config
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.persistence.models import Job, Step, StepAttempt, VerificationRun
from hermclaw.planner import Planner, Replanner
from hermclaw.runtime.driver import DriverSettings, RuntimeJobDriver
from hermclaw.scope.audit import ScopeAuditor
from hermclaw.scope.engine import ScopeEngine
from hermclaw.scope.expansion import ScopeExpansionHandler
from hermclaw.tools.executors import SubprocessExecutor
from tests.integration.test_gitops_support import World, git, make_world
from tests.integration.test_runtime_driver import FakeIntel, ReviewHandler, _job, _plan, _replan, _run
from tests.integration.test_tools_support import dev_settings
from tests.unit.test_planner_support import ScriptedChat

pytestmark = pytest.mark.asyncio(loop_scope="session")
T = TypeVar("T", bound=BaseModel)


@pytest.fixture
async def world(sessionmaker: Any, tmp_path: Path) -> World:
    async with sessionmaker() as s:  # the DB is shared per test session: neutralise other tests' open jobs
        await s.execute(update(Job).where(Job.status.not_in(("succeeded", "failed", "cancelled"))).values(status="cancelled"))
        await s.commit()
    return await make_world(sessionmaker, tmp_path)


def act(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args, "status": tool, "decision": tool}


EDIT = act("replace_text", path="src/module.py", old="VALUE = 1", new="VALUE = 2")
DONE = act("complete_step", summary="VALUE set to 2", changed_files=["src/module.py"], tests_run=[])


class Models(ScriptedChat):
    """Gemma (chat), fast router + Qwen3-Coder (structured) – scripted per alias."""

    def __init__(self, plans: list[Any], coder: list[dict[str, Any]]) -> None:
        super().__init__(plans)
        self.coder = list(coder)
        self.coder_prompts: list[list[ChatMessage]] = []

    async def structured(  # type: ignore[override]
        self, alias: str, messages: list[ChatMessage], schema: type[T], *, ctx: CallContext, **_: Any
    ) -> StructuredResult[T]:
        if alias == "fast-router":
            return StructuredResult(
                value=schema.model_validate({"intent": "code_change"}), result=ChatResult(content="{}", alias=alias, model="qwen3:8b")
            )
        assert alias == "coder-main", alias
        self.coder_prompts.append(list(messages))
        if not self.coder:
            raise AssertionError("no scripted coder action left")
        return StructuredResult(
            value=schema.model_validate(self.coder.pop(0)), result=ChatResult(content="{}", alias=alias, model="qwen3-coder:30b")
        )


class ScriptedVerifier:
    """Records a real verification_runs row (needed by commit_verified) with scripted pass/fail."""

    def __init__(self, world: World, results: list[bool]) -> None:
        self.world, self.results, self.calls = world, list(results), 0

    async def verify(
        self, step: StepContract, workspace: WorkspaceHandle, *, job_id: uuid.UUID, step_id: uuid.UUID, attempt_id: uuid.UUID
    ) -> VerificationOutcome:
        self.calls += 1
        passed = self.results.pop(0) if self.results else True
        changed = await self.world.engine.changed_files(workspace.id)
        checks = [
            VerificationCheck(
                check_type="presence",
                name="VALUE = 2",
                status="pass" if passed else "fail",
                message="" if passed else "pattern 'VALUE = 3' not found in src/module.py",
                evidence={"path": "src/module.py"},
            )
        ]
        report = VerificationReport(passed=passed, checks=checks, changed_files=changed, summary="ok" if passed else "1 blocking failure")
        async with self.world.sessionmaker() as s:
            vr = VerificationRun(
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt_id,
                passed=passed,
                status="passed" if passed else "failed",
                summary=report.summary,
                changed_files=changed,
            )
            s.add(vr)
            await s.commit()
        return VerificationOutcome(report=report, run_id=vr.id)


class ScriptedReviewer:
    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts, self.calls = list(verdicts), 0

    def should_review(self, step_kind: str, verifier_passed: bool) -> bool:
        return step_kind == "implement" and verifier_passed

    async def review(self, step: StepContract, workspace: WorkspaceHandle, verification: VerificationReport, **_: Any) -> ReviewContract:
        self.calls += 1
        v = self.verdicts.pop(0) if self.verdicts else "pass"
        if v == "major_pass":  # model claims pass but reports a major finding -> invariant must override
            return ReviewContract(
                verdict="pass",
                findings=[
                    ReviewFinding(
                        severity="major",
                        path="src/module.py",
                        summary="VALUE must stay configurable via env",
                        suggested_fix="read VALUE from HERMCLAW_VALUE",
                    )
                ],
            )
        return ReviewContract(verdict="pass", summary="looks good")


def _handler(world: World, models: Models, verifier: ScriptedVerifier, reviewer: ScriptedReviewer | None) -> ImplementStepHandler:
    cfg = get_config()
    intel = FakeIntel()

    async def executor_factory(handle: WorkspaceHandle, step: StepContract) -> SubprocessExecutor:
        return SubprocessExecutor(cfg.policies.sandbox, settings=dev_settings())

    deps = ImplementDeps(
        git=world.engine,
        scope_engine=ScopeEngine(world.sessionmaker, cfg, intel),
        scope_auditor=ScopeAuditor(world.sessionmaker, cfg),
        expansion=ScopeExpansionHandler(world.sessionmaker, cfg, intel),
        chat=models,
        repo=intel,
        executor_factory=executor_factory,
        verifier=verifier,
        reviewer=reviewer,
    )
    return ImplementStepHandler(world.sessionmaker, cfg, deps)


def _driver(world: World, models: Models, tmp_path: Path) -> RuntimeJobDriver:
    cfg = get_config()
    return RuntimeJobDriver(
        world.sessionmaker,
        cfg,
        planner=Planner(models, world.sessionmaker, cfg),
        replanner=Replanner(models, world.sessionmaker, cfg),
        git=world.engine,
        repo_intel=FakeIntel(),
        chat=models,
        settings=DriverSettings(artifacts_dir=tmp_path / "artifacts"),
    )


async def _attempts(world: World, job_id: uuid.UUID) -> list[StepAttempt]:
    async with world.sessionmaker() as s:
        return list(
            (
                await s.execute(
                    select(StepAttempt).where(StepAttempt.job_id == job_id).order_by(StepAttempt.started_at, StepAttempt.attempt_no)
                )
            ).scalars()
        )


def _remote_value(world: World) -> str:
    branch = git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare).strip().lstrip("* ").strip()
    return git("show", f"{branch}:src/module.py", cwd=world.upstream.bare)


async def test_implement_step_happy_path_commits_and_pushes(world: World, tmp_path: Path) -> None:
    models = Models([_plan()], [EDIT, DONE])
    verifier, reviewer = ScriptedVerifier(world, [True]), ScriptedReviewer(["pass"])
    jid = await _job(world)
    jobs = await _run(world, _driver(world, models, tmp_path), [_handler(world, models, verifier, reviewer), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert verifier.calls == 1 and reviewer.calls == 1
    assert _remote_value(world) == "VALUE = 2"
    async with world.sessionmaker() as s:
        s1 = (await s.execute(select(Step).where(Step.job_id == jid, Step.step_key == "S001"))).scalar_one()
    assert s1.result["commit"] and s1.result["files"] == ["src/module.py"]
    # the scope engine created an explicit scope from the planner hint before the coder ran
    assert any("src/module.py" in m.content for m in models.coder_prompts[0])


async def test_verifier_failure_triggers_correction_with_evidence(world: World, tmp_path: Path) -> None:
    models = Models([_plan()], [EDIT, DONE, DONE])  # correction attempt: change already present, coder completes again
    verifier = ScriptedVerifier(world, [False, True])
    jid = await _job(world)
    jobs = await _run(world, _driver(world, models, tmp_path), [_handler(world, models, verifier, None), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    s1 = [a for a in await _attempts(world, jid)]
    kinds = [(a.attempt_no, a.kind, a.outcome) for a in s1 if a.attempt_no <= 2 and a.kind in ("initial", "correction")]
    assert (1, "initial", "failed") in kinds and (2, "correction", "completed") in kinds
    correction_prompt = models.coder_prompts[2]
    assert any("pattern 'VALUE = 3' not found" in m.content for m in correction_prompt), (
        "verifier evidence must reach the correction attempt"
    )
    async with world.sessionmaker() as s:
        st = (await s.execute(select(Step).where(Step.job_id == jid, Step.step_key == "S001"))).scalar_one()
    assert st.correction_count == 1 and st.attempt_count == 2


async def test_review_invariant_major_finding_forces_correction(world: World, tmp_path: Path) -> None:
    models = Models([_plan()], [EDIT, DONE, DONE])
    verifier, reviewer = ScriptedVerifier(world, [True, True]), ScriptedReviewer(["major_pass", "pass"])
    jid = await _job(world)
    jobs = await _run(world, _driver(world, models, tmp_path), [_handler(world, models, verifier, reviewer), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert reviewer.calls == 2
    assert any("configurable via env" in m.content for m in models.coder_prompts[2]), "review finding must reach the correction attempt"


async def test_exhausted_corrections_escalate_to_replan(world: World, tmp_path: Path) -> None:
    # S001 fails verification 3x (initial + 2 corrections) -> blocked repeated_verifier_failure -> Gemma replan -> S003 passes
    models = Models([_plan(), _replan()], [EDIT, DONE, DONE, DONE, EDIT, DONE])
    verifier = ScriptedVerifier(world, [False, False, False, True])
    jid = await _job(world)
    jobs = await _run(world, _driver(world, models, tmp_path), [_handler(world, models, verifier, None), ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert jobs[jid].replan_count == 1
    replan_payload = json.dumps(models.user_payload(1))
    assert "repeated_verifier_failure" in replan_payload
    async with world.sessionmaker() as s:
        old = (await s.execute(select(Step).where(Step.job_id == jid, Step.step_key == "S001"))).scalar_one()
        new = (await s.execute(select(Step).where(Step.job_id == jid, Step.step_key == "S003"))).scalar_one()
        job = (await s.execute(select(Job).where(Job.id == jid))).scalar_one()
    assert old.superseded and old.correction_count == 2 and old.result["replan_reason"] == "repeated_verifier_failure"
    assert new.status == "completed" and job.status == "succeeded"
    assert _remote_value(world) == "VALUE = 2"


async def test_stagnation_research_recommendation_feeds_research_into_correction(world: World, tmp_path: Path) -> None:
    from hermclaw.coder import StagnationDirective

    class StopOnce:
        calls = 0

        async def observe(self, obs: Any) -> StagnationDirective:
            StopOnce.calls += 1
            if StopOnce.calls == 1:
                return StagnationDirective("stop", recommendation="research", reasons=("same error signature 4x",))
            return StagnationDirective()

    class Research:
        questions: ClassVar[list[str]] = []

        async def ask(self, question: str, *, job_id: uuid.UUID, step_id: uuid.UUID) -> str:
            Research.questions.append(question)
            return "Official docs: set VALUE via module constant; tests read it directly."

    async def stagnation_factory(ctx: Any) -> StopOnce:
        return StopOnce()

    models = Models([_plan()], [act("read_file", path="src/module.py"), EDIT, DONE])
    handler = _handler(world, models, ScriptedVerifier(world, [True]), None)
    handler.deps.research = Research()
    handler.deps.stagnation_factory = stagnation_factory
    jid = await _job(world)
    jobs = await _run(world, _driver(world, models, tmp_path), [handler, ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert Research.questions and "same error signature" in Research.questions[0]
    assert any("Official docs" in m.content for m in models.coder_prompts[1]), "research result must reach the correction attempt"


async def test_two_implement_steps_with_real_verifier_commit_separately(world: World, tmp_path: Path) -> None:
    """Step baseline: S002 is verified against the HEAD after S001's commit, not against the job base."""
    from hermclaw.runtime.adapters import VerifierAdapter
    from tests.unit.test_planner_support import plan, step

    two = plan(
        "Set VALUE and helper",
        [
            step(
                "S001",
                "implement",
                "coding",
                "Change VALUE in src/module.py from 1 to 2.",
                repo_hints=["src/module.py"],
                acceptance=[{"type": "presence", "path_glob": "src/module.py", "pattern": "VALUE = 2"}],
            ),
            step(
                "S002",
                "implement",
                "coding",
                "Make helper() in src/util.py return 'HELP'.",
                depends_on=["S001"],
                repo_hints=["src/util.py"],
                acceptance=[{"type": "presence", "path_glob": "src/util.py", "pattern": "HELP"}],
            ),
            step("S003", "review", "review", "Review both changes.", depends_on=["S002"]),
        ],
    )
    util_edit = act("replace_text", path="src/util.py", old="return 'help'", new="return 'HELP'")
    util_done = act("complete_step", summary="helper upper-cased", changed_files=["src/util.py"], tests_run=[])
    models = Models([two], [EDIT, DONE, util_edit, util_done])
    cfg = get_config()

    async def executor_for(ws: WorkspaceHandle, step_c: StepContract | None) -> SubprocessExecutor:
        return SubprocessExecutor(cfg.policies.sandbox, settings=dev_settings())

    handler = _handler(world, models, ScriptedVerifier(world, []), None)
    handler.deps.verifier = VerifierAdapter(world.sessionmaker, cfg, world.engine.reader(), executor_for)
    jid = await _job(world)
    jobs = await _run(world, _driver(world, models, tmp_path), [handler, ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    branch = git("branch", "--list", "hermclaw/*", cwd=world.upstream.bare).strip().lstrip("* ").strip()
    log = git("log", "--format=%s", f"main..{branch}", cwd=world.upstream.bare).splitlines()
    assert log == ["S002: Make helper() in src/util.py return 'HELP'.", "S001: Change VALUE in src/module.py from 1 to 2."]
    assert git("show", f"{branch}:src/util.py", cwd=world.upstream.bare).endswith("return 'HELP'")
    async with world.sessionmaker() as s:
        rows = {r.step_key: r for r in (await s.execute(select(Step).where(Step.job_id == jid))).scalars()}
    assert rows["S001"].result["files"] == ["src/module.py"] and rows["S002"].result["files"] == ["src/util.py"]


async def test_video_preemption_checkpoints_the_coder_and_the_step_resumes(world: World, tmp_path: Path) -> None:
    """25.4/29.4/29.7: the coder holds the coder-model lease; a video job preempts it, the coder checkpoints,
    the GPU goes to the video, and after its release the scheduler resumes the step from the checkpoint."""
    from hermclaw.persistence.models import ResourceLease
    from hermclaw.resources.manager import ResourceManager

    rm = ResourceManager.from_config(world.sessionmaker, get_config(), "test-preempt")
    video_done = asyncio.Event()
    media_task: list[asyncio.Task[Any]] = []

    async def video_job() -> None:
        media = await rm.acquire_gpu_for_media("video", ttl_seconds=60, wait_timeout=30)
        await asyncio.sleep(0.2)  # "render"
        await rm.release_media(media)
        video_done.set()

    class PreemptingModels(Models):
        async def structured(
            self, alias: str, messages: list[ChatMessage], schema: type[T], *, ctx: CallContext, **kw: Any
        ) -> StructuredResult[T]:  # type: ignore[override]
            if alias == "coder-main" and not media_task:
                media_task.append(asyncio.create_task(video_job()))  # a video job arrives during the first coder turn
                await asyncio.sleep(0.5)  # the keeper sees the preemption request while the turn runs
            return await super().structured(alias, messages, schema, ctx=ctx, **kw)

    models = PreemptingModels([_plan()], [act("read_file", path="src/module.py"), EDIT, DONE])
    handler = _handler(world, models, ScriptedVerifier(world, [True]), None)
    handler.deps.resources = rm
    handler.deps.lease_wait_seconds = 30
    handler.deps.lease_keeper_interval = 0.05
    jid = await _job(world)
    jobs = await _run(world, _driver(world, models, tmp_path), [handler, ReviewHandler()], [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert video_done.is_set()
    attempts = await _attempts(world, jid)
    s1 = [(a.attempt_no, a.kind, a.outcome) for a in attempts if a.kind in ("initial", "resume")]
    assert (1, "initial", "checkpointed") in s1 and any(k == "resume" and o == "completed" for _, k, o in s1), s1
    async with world.sessionmaker() as s:
        leases = (await s.execute(select(ResourceLease).where(ResourceLease.owner_job_id == jid))).scalars().all()
    reasons = sorted({lease.release_reason for lease in leases if lease.release_reason})
    assert "preempted" in reasons and all(lease.state not in ("active", "preempting") for lease in leases), reasons


def _real_verifier(world: World) -> Any:
    from hermclaw.runtime.adapters import VerifierAdapter

    cfg = get_config()

    async def executor_for(ws: WorkspaceHandle, step_c: StepContract | None) -> SubprocessExecutor:
        return SubprocessExecutor(cfg.policies.sandbox, settings=dev_settings())

    return VerifierAdapter(world.sessionmaker, cfg, world.engine.reader(), executor_for)


def _plan_with_checks() -> dict[str, Any]:
    from tests.unit.test_planner_support import plan, step

    return plan(
        "Set VALUE to 2 and check it",
        [
            step(
                "S001",
                "implement",
                "coding",
                "Change VALUE in src/module.py from 1 to 2.",
                repo_hints=["src/module.py"],
                acceptance=[{"type": "presence", "path_glob": "src/module.py", "pattern": "VALUE = 2"}],
            ),
            step(
                "S002",
                "test",
                "testing",
                "Check that VALUE is 2 and the old value is gone.",
                depends_on=["S001"],
                acceptance=[
                    {"type": "presence", "path_glob": "src/module.py", "pattern": "VALUE = 2"},
                    {"type": "absence", "path_glob": "src/*.py", "pattern": "VALUE = 1", "expected_matches": 0},
                ],
            ),
            step("S003", "review", "review", "Review the whole change.", depends_on=["S002"]),
        ],
    )


async def test_verify_and_review_step_handlers_in_the_pipeline(world: World, tmp_path: Path) -> None:
    from hermclaw.runtime.handlers import ReviewStepHandler, VerifyStepHandler

    models = Models([_plan_with_checks()], [EDIT, DONE])
    verifier = _real_verifier(world)
    reviewer = ScriptedReviewer(["pass"])
    impl = _handler(world, models, ScriptedVerifier(world, []), None)
    impl.deps.verifier = verifier
    cfg = get_config()
    handlers = [
        impl,
        VerifyStepHandler(world.sessionmaker, cfg, git=world.engine, verifier=verifier),
        ReviewStepHandler(world.sessionmaker, cfg, git=world.engine, verifier=verifier, reviewer=reviewer),
    ]
    jid = await _job(world)
    jobs = await _run(world, _driver(world, models, tmp_path), handlers, [jid])
    assert jobs[jid].status == "succeeded", (jobs[jid].error_code, jobs[jid].error_message)
    assert reviewer.calls == 1
    async with world.sessionmaker() as s:
        rows = {r.step_key: r for r in (await s.execute(select(Step).where(Step.job_id == jid))).scalars()}
    assert rows["S002"].status == "completed" and rows["S002"].result["verification_run_id"]
    assert rows["S003"].result["verdict"] == "pass"


async def test_failing_verification_step_blocks_with_evidence_for_the_replanner(world: World, tmp_path: Path) -> None:
    from hermclaw.runtime.handlers import VerifyStepHandler

    # the implement step "forgets" the change -> the test step's evidence fails -> replan with the failing checks
    noop_done = act("complete_step", summary="nothing changed", changed_files=[], tests_run=[])
    models = Models([_plan_with_checks()], [noop_done])
    verifier = _real_verifier(world)
    impl = _handler(world, models, ScriptedVerifier(world, [True]), None)
    cfg = get_config()
    handlers = [impl, VerifyStepHandler(world.sessionmaker, cfg, git=world.engine, verifier=verifier), ReviewHandler()]
    jid = await _job(world)
    jobs = await _run(world, _driver(world, models, tmp_path), handlers, [jid])
    assert jobs[jid].status == "failed"  # no scripted replan answer -> replan cannot succeed
    async with world.sessionmaker() as s:
        s2 = (await s.execute(select(Step).where(Step.job_id == jid, Step.step_key == "S002"))).scalar_one()
    assert s2.status == "blocked" and s2.error_code == "VERIFICATION_STEP_FAILED"
    checks = [f["check"] for f in s2.result["replan_evidence"]["failures"]]
    assert any(c.startswith("presence") for c in checks) and any(c.startswith("absence") for c in checks)
