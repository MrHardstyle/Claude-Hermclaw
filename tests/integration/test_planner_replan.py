"""Replanning against real PostgreSQL (P24 24.1-24.6)."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import StepStatus
from hermclaw.contracts.events import EventType
from hermclaw.contracts.plan import PlanContract
from hermclaw.core.config import get_config
from hermclaw.events.store import list_events
from hermclaw.persistence.models import (
    Job,
    Plan,
    PlanVersion,
    ResearchRun,
    ScopeContractRow,
    Step,
    StepAttempt,
    StepDependency,
    TestRun,
    VerificationCheckRow,
    VerificationRun,
)
from hermclaw.planner import PlanConflict, Planner, PlannerError, ReplanLimitReached, Replanner, ReplanTrigger
from hermclaw.planner.replanner import replan_json_schema
from hermclaw.runtime.transitions import transition_step
from tests.unit.test_planner_support import FASTAPI, LINUX_ADMIN, Answer, ScriptedChat, create_job, plan, step

pytestmark = pytest.mark.integration
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _sm(sessionmaker: object) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], sessionmaker)


PATH_TO = {
    StepStatus.completed: [StepStatus.ready, StepStatus.leased, StepStatus.running, StepStatus.completed],
    StepStatus.failed: [StepStatus.ready, StepStatus.leased, StepStatus.running, StepStatus.failed],
    StepStatus.running: [StepStatus.ready, StepStatus.leased, StepStatus.running],
}


async def _move(sm: async_sessionmaker[AsyncSession], step_id: uuid.UUID, to: StepStatus) -> None:
    async with sm() as s:
        row = await s.get(Step, step_id)
        assert row is not None
        for target in PATH_TO[to]:
            await transition_step(s, row, target, reason="test")
        if to == StepStatus.failed:
            row.error_code = "VERIFIER_FAILED"
            row.error_message = "diff check failed twice"
            row.attempt_count = 3
        await s.commit()


async def _planned_fastapi(sm: async_sessionmaker[AsyncSession], *, s003_running: bool = False) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """FastAPI plan v1 with S001 completed, S002 failed (with verifier/test/scope evidence), S003 open."""
    job_id = await create_job(sm, FASTAPI)
    result = await Planner(ScriptedChat([FASTAPI.answer()]), sm, get_config()).create_plan(job_id, FASTAPI.inputs)
    ids = dict(result.step_ids)
    await _move(sm, ids["S001"], StepStatus.completed)
    await _move(sm, ids["S002"], StepStatus.failed)
    if s003_running:
        await _move(sm, ids["S003"], StepStatus.running)
    async with sm() as s:
        s.add(
            StepAttempt(step_id=ids["S001"], job_id=job_id, attempt_no=1, status="completed", summary="Added limit/offset to list_users.")
        )
        s.add(
            StepAttempt(step_id=ids["S002"], job_id=job_id, attempt_no=1, status="failed", outcome="verifier_failed", summary="README edit")
        )
        run = VerificationRun(job_id=job_id, step_id=ids["S002"], passed=False, status="failed", summary="1 blocking failure")
        s.add(run)
        await s.flush()
        s.add(
            VerificationCheckRow(
                verification_run_id=run.id, check_type="diff", name="must_change README.md", status="fail", message="README.md unchanged"
            )
        )
        s.add(VerificationCheckRow(verification_run_id=run.id, check_type="scope", name="scope", status="pass", message="ok"))
        lines = [f"tests/test_users.py::test_{i} PASSED" for i in range(400)] + [
            f"token={SECRET}",
            "FAILED tests/test_users.py::test_page - 1 failed",
        ]
        s.add(
            TestRun(
                job_id=job_id,
                step_id=ids["S002"],
                command="pytest -q",
                framework="pytest",
                status="failed",
                passed=400,
                failed=1,
                output_excerpt="\n".join(lines),
            )
        )
        s.add(ScopeContractRow(job_id=job_id, step_id=ids["S002"], version=1, status="active", contract={"target_paths": ["README.md"]}))
        s.add(
            ScopeContractRow(
                job_id=job_id, step_id=ids["S001"], version=1, status="active", contract={"target_paths": ["app/routers/users.py"]}
            )
        )
        s.add(ResearchRun(job_id=job_id, question="pagination conventions", status="completed", synthesis="Use limit/offset with max 100."))
        await s.commit()
    return job_id, ids


def _replan_answer(**overrides: Any) -> dict[str, Any]:
    steps = [
        step(
            "S002",
            "documentation",
            "documentation",
            "Document the pagination parameters in a dedicated README section with an example request.",
            depends_on=["S001"],
            repo_hints=["README.md"],
            acceptance=[{"type": "presence", "path_glob": "README.md", "pattern": "limit"}],
        ),
        step("S004", "review", "review", "Review pagination code and the new README section together.", depends_on=["S001", "S002"]),
    ]
    data = plan("Paginate GET /users", steps, summary="replanned documentation")
    data.update(overrides)
    return data


async def _rows(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> list[Step]:
    async with sm() as s:
        return list((await s.execute(select(Step).where(Step.job_id == job_id).order_by(Step.step_key, Step.created_at))).scalars())


async def _events(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> list[tuple[str, dict[str, Any]]]:
    async with sm() as s:
        return [(e.event_type, e.payload) for e in await list_events(s, job_id=job_id)]


def _trigger(ids: dict[str, uuid.UUID], reason: str = "repeated_verifier_failure") -> ReplanTrigger:
    return ReplanTrigger.model_validate(
        {"reason_code": reason, "failed_step_id": ids["S002"], "evidence": {"verifier": "README.md unchanged"}, "detail": "S002 failed 3x"}
    )


# --------------------------------------------------------------------------------------------- main flow
async def test_replan_preserves_completed_steps_and_supersedes_the_rest(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm, s003_running=True)
    chat = ScriptedChat([_replan_answer()])
    result = await Replanner(chat, sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)

    # 24.2 Gemma replan through the planner alias with the replan schema
    call = chat.calls[0]
    assert call.alias == "planner-gemma" and call.ctx.purpose == "replan" and call.ctx.step_id == ids["S002"]
    assert call.json_schema == replan_json_schema()
    assert "REPLANNER" in call.messages[0].content

    # 24.3 new plan version
    assert result.version == 2 and result.source == "replanner"
    async with sm() as s:
        job = await s.get(Job, job_id)
        plan_row = (await s.execute(select(Plan).where(Plan.job_id == job_id))).scalar_one()
        versions = list((await s.execute(select(PlanVersion).where(PlanVersion.job_id == job_id).order_by(PlanVersion.version))).scalars())
    assert job is not None and job.replan_count == 1 and job.current_plan_version == 2
    assert job.metadata_["last_replan"] == {"version": 2, "reason_code": "repeated_verifier_failure"}
    assert plan_row.current_version == 2
    assert [v.version for v in versions] == [1, 2] and versions[1].source == "replanner"
    assert versions[1].reason is not None and versions[1].reason.startswith("repeated_verifier_failure: S002 failed 3x")
    merged = PlanContract.model_validate(versions[1].plan_json)
    assert [s.id for s in merged.steps] == ["S001", "S002", "S004"]

    # 24.4 completed step preserved, others superseded (in-flight cancelled, failed kept as history)
    rows = await _rows(sm, job_id)
    by_key: dict[str, list[Step]] = {}
    for r in rows:
        by_key.setdefault(r.step_key, []).append(r)
    s001 = by_key["S001"]
    assert len(s001) == 1 and s001[0].id == ids["S001"] and s001[0].status == "completed" and not s001[0].superseded
    assert s001[0].plan_version_id == versions[0].id
    old_s002 = next(r for r in by_key["S002"] if r.id == ids["S002"])
    new_s002 = next(r for r in by_key["S002"] if r.id != ids["S002"])
    assert old_s002.superseded and old_s002.status == "failed" and old_s002.error_code == "VERIFIER_FAILED"
    assert not new_s002.superseded and new_s002.status == "pending" and new_s002.plan_version_id == versions[1].id
    old_s003 = by_key["S003"][0]
    assert old_s003.superseded and old_s003.status == "cancelled"
    assert by_key["S004"][0].status == "pending"
    assert result.preserved_step_keys == ["S001"] and result.superseded_step_keys == ["S002", "S003"]
    assert set(result.step_ids) == {"S001", "S002", "S004"} and result.step_ids["S001"] == ids["S001"]
    assert result.created_step_keys == ["S002", "S004"]

    # 24.5 new dependencies reference the kept completed row
    async with sm() as s:
        deps = list((await s.execute(select(StepDependency).where(StepDependency.step_id == new_s002.id))).scalars())
        s004_deps = {
            d.depends_on_step_id
            for d in (await s.execute(select(StepDependency).where(StepDependency.step_id == by_key["S004"][0].id))).scalars()
        }
    assert [d.depends_on_step_id for d in deps] == [ids["S001"]]
    assert s004_deps == {ids["S001"], new_s002.id}

    # 24.6 scope refresh: superseded step's scope superseded, kept step's scope untouched, new steps without scope
    async with sm() as s:
        scopes = {r.step_id: r for r in (await s.execute(select(ScopeContractRow).where(ScopeContractRow.job_id == job_id))).scalars()}
    assert scopes[ids["S002"]].status == "superseded" and "plan version 2" in (scopes[ids["S002"]].reason or "")
    assert scopes[ids["S001"]].status == "active"
    assert new_s002.current_scope_version is None and new_s002.id not in scopes

    # events
    events = await _events(sm, job_id)
    types = [e[0] for e in events]
    started = next(p for t, p in events if t == EventType.REPLAN_STARTED)
    assert started["reason_code"] == "repeated_verifier_failure" and started["failed_step"] == "S002" and started["from_version"] == 1
    created = next(p for t, p in events if t == EventType.REPLAN_CREATED)
    assert created["to_version"] == 2 and created["preserved_steps"] == ["S001"] and created["superseded_steps"] == ["S002", "S003"]
    assert created["new_steps"] == ["S002", "S004"] and created["scopes_superseded"] == 1
    cancelled = [p for t, p in events if t == EventType.STEP_TRANSITION and p["to"] == "cancelled"]
    assert len(cancelled) == 1 and cancelled[0]["step_key"] == "S003" and "superseded by plan version 2" in cancelled[0]["reason"]
    assert types.count(EventType.STEP_CREATED) == 3 + 2


async def test_failure_package_contents(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)
    chat = ScriptedChat([_replan_answer()])
    await Replanner(chat, sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)
    payload = chat.user_payload()
    assert list(payload)[:8] == [
        "original_goal",
        "job",
        "trigger",
        "current_plan",
        "completed_steps",
        "failed_step",
        "deterministic_evidence",
        "open_steps",
    ]
    assert payload["original_goal"] == FASTAPI.goal and "goal" not in payload["job"]
    assert payload["trigger"] == {"reason_code": "repeated_verifier_failure", "detail": "S002 failed 3x", "failed_step": "S002"}
    assert payload["current_plan"]["version"] == 1 and [s["id"] for s in payload["current_plan"]["plan"]["steps"]] == [
        "S001",
        "S002",
        "S003",
    ]
    assert payload["completed_steps"][0]["id"] == "S001" and payload["completed_steps"][0]["summary"] == "Added limit/offset to list_users."
    failed = payload["failed_step"]
    assert failed["id"] == "S002" and failed["status"] == "failed" and failed["error_code"] == "VERIFIER_FAILED"
    assert failed["attempts"][0]["outcome"] == "verifier_failed"
    ev = payload["deterministic_evidence"]
    assert ev["trigger"] == {"verifier": "README.md unchanged"}
    assert ev["verifier"]["failed_checks"] == [
        {
            "check_type": "diff",
            "name": "must_change README.md",
            "status": "fail",
            "blocking": True,
            "message": "README.md unchanged",
            "evidence": {},
        }
    ]
    tail = ev["tests"][0]["output_tail"]
    assert tail.endswith("FAILED tests/test_users.py::test_page - 1 failed") and "line(s) truncated]" in tail
    assert SECRET not in str(payload) and "***REDACTED***" in tail
    assert ev["scope_decision"]["status"] == "active" and ev["scope_decision"]["target_paths"] == ["README.md"]
    assert [s["id"] for s in payload["open_steps"]] == ["S003"]
    assert payload["research_evidence"][0]["synthesis"] == "Use limit/offset with max 100."
    assert payload["repository_inventory"]["test_command"] == "pytest -q"


async def test_completed_step_needs_rerun_reason(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)
    again = step("S001", "implement", "coding", "Re-apply pagination on top of the changed router.", repo_hints=["app/routers/users.py"])
    first = _replan_answer()
    first["steps"].insert(0, dict(again))
    second = _replan_answer()
    second["steps"].insert(0, {**again, "rerun_reason": "app/routers/users.py was rewritten upstream after S001 completed"})
    chat = ScriptedChat([first, second])
    result = await Replanner(chat, sm, get_config()).replan(job_id, _trigger(ids, "repository_changed"), FASTAPI.inputs)
    assert result.repair_attempts == 1
    assert any("S001: is already completed and is never re-run without a reason" in e for e in result.validation_errors[0]["errors"])
    assert result.rerun_reasons == {"S001": "app/routers/users.py was rewritten upstream after S001 completed"}
    assert result.preserved_step_keys == []
    rows = [r for r in await _rows(sm, job_id) if r.step_key == "S001"]
    old = next(r for r in rows if r.id == ids["S001"])
    new = next(r for r in rows if r.id != ids["S001"])
    assert old.superseded and old.status == "completed"  # history stays completed
    assert not new.superseded and new.status == "pending" and new.current_scope_version is None
    async with sm() as s:
        scope = (await s.execute(select(ScopeContractRow).where(ScopeContractRow.step_id == ids["S001"]))).scalar_one()
        new_s002 = (
            await s.execute(select(Step).where(Step.job_id == job_id, Step.step_key == "S002", Step.superseded.is_(False)))
        ).scalar_one()
        s002_deps = [
            d.depends_on_step_id for d in (await s.execute(select(StepDependency).where(StepDependency.step_id == new_s002.id))).scalars()
        ]
    assert scope.status == "superseded"  # 24.6: the re-run step needs a fresh scope
    assert s002_deps == [new.id]  # 24.5: dependants of a re-run step wait for the new row, not the old completed one
    async with sm() as s:
        version = (await s.execute(select(PlanVersion).where(PlanVersion.job_id == job_id, PlanVersion.version == 2))).scalar_one()
    assert "rerun S001: app/routers/users.py was rewritten upstream" in (version.reason or "")


async def test_unchanged_repeat_of_failed_step_is_rejected(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)
    orig = FASTAPI.answer()["steps"][1]  # identical to the failed S002
    repeat = plan(
        "Paginate GET /users", [dict(orig), step("S004", "review", "review", "Review the documentation step.", depends_on=["S002"])]
    )
    chat = ScriptedChat([repeat, _replan_answer()])
    result = await Replanner(chat, sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)
    assert result.repair_attempts == 1
    assert result.validation_errors[0]["errors"] == [
        "step S002: repeats the failed step S002 unchanged although it failed with repeated_verifier_failure; "
        "change the approach (goal, repo_hints, dependencies or acceptance) or remove it"
    ]


async def test_unchanged_repeat_is_allowed_when_worker_was_unavailable(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)
    orig = FASTAPI.answer()["steps"][1]
    repeat = plan(
        "Paginate GET /users", [dict(orig), step("S004", "review", "review", "Review the documentation step.", depends_on=["S002"])]
    )
    result = await Replanner(ScriptedChat([repeat]), sm, get_config()).replan(job_id, _trigger(ids, "worker_unavailable"), FASTAPI.inputs)
    assert result.repair_attempts == 0 and result.created_step_keys == ["S002", "S004"]


async def test_replan_can_add_research_and_depend_on_completed_steps(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)
    answer = _replan_answer(research_needed=[{"question": "How do other FastAPI services document pagination parameters?"}])
    result = await Replanner(ScriptedChat([answer]), sm, get_config()).replan(
        job_id, _trigger(ids, "research_changed_assumptions"), FASTAPI.inputs
    )
    research = [s for s in result.plan.steps if s.kind.value == "research"]
    assert len(research) == 1 and research[0].id == "S005"  # S001-S004 are in use
    s002 = next(s for s in result.plan.steps if s.id == "S002")
    assert set(s002.depends_on) == {"S001", "S005"}


# --------------------------------------------------------------------------------------------- limits + conflicts
async def test_replan_limit_is_enforced(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)
    max_replans = get_config().policies.correction.max_replans_per_job
    for i in range(max_replans):
        answer = _replan_answer()
        answer["steps"][0]["goal"] += f" (variant {i})"
        answer["steps"][1]["goal"] += f" (variant {i})"
        res = await Replanner(ScriptedChat([answer]), sm, get_config()).replan(
            job_id, ReplanTrigger(reason_code="stagnation", failed_step_id=None), FASTAPI.inputs
        )
        assert res.version == i + 2
    chat = ScriptedChat([_replan_answer()])
    with pytest.raises(ReplanLimitReached) as info:
        await Replanner(chat, sm, get_config()).replan(job_id, ReplanTrigger(reason_code="stagnation"), FASTAPI.inputs)
    assert info.value.code == "REPLAN_LIMIT_REACHED" and chat.calls == []
    failed = [p for t, p in await _events(sm, job_id) if t == EventType.PLANNER_FAILED]
    assert failed[-1]["error_code"] == "REPLAN_LIMIT_REACHED" and failed[-1]["mode"] == "replan"
    async with sm() as s:
        job = await s.get(Job, job_id)
    assert job is not None and job.replan_count == max_replans


async def test_replan_without_plan_is_refused(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, LINUX_ADMIN)
    with pytest.raises(PlanConflict) as info:
        await Replanner(ScriptedChat([]), sm, get_config()).replan(job_id, ReplanTrigger(reason_code="missing_dependency"))
    assert info.value.code == "NO_PLAN"


async def test_stale_failed_step_is_refused(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)
    await Replanner(ScriptedChat([_replan_answer()]), sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)
    with pytest.raises(PlanConflict) as info:  # old S002 row is superseded now
        await Replanner(ScriptedChat([_replan_answer()]), sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)
    assert info.value.code == "PLAN_CHANGED"


async def test_concurrent_completion_during_replan_aborts(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)

    async def complete_s003() -> None:
        await _move(sm, ids["S003"], StepStatus.completed)

    chat = ScriptedChat([Answer(content=_replan_answer(), before=complete_s003)])
    with pytest.raises(PlanConflict) as info:
        await Replanner(chat, sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)
    assert info.value.code == "PLAN_CHANGED"
    async with sm() as s:
        job = await s.get(Job, job_id)
        versions = list((await s.execute(select(PlanVersion).where(PlanVersion.job_id == job_id))).scalars())
    assert job is not None and job.replan_count == 0 and job.current_plan_version == 1 and len(versions) == 1
    assert [r.step_key for r in await _rows(sm, job_id) if r.superseded] == []


async def test_replan_fallback_and_invalid_output(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)
    with pytest.raises(PlannerError) as info:
        await Replanner(ScriptedChat(["{}", "[]", "{"]), sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)
    assert info.value.code == "PLANNER_INVALID_OUTPUT" and info.value.details["repair_attempts"] == 2
    result = await Replanner(ScriptedChat([Answer(content=_replan_answer(), fallback_used=True)]), sm, get_config()).replan(
        job_id, _trigger(ids), FASTAPI.inputs
    )
    assert result.source == "fallback"
    async with sm() as s:
        job = await s.get(Job, job_id)
    assert job is not None and job.metadata_["planner_model_fallback"] is True and job.replan_count == 1
    types = [t for t, _ in await _events(sm, job_id)]
    assert EventType.PLANNER_FALLBACK_USED in types and EventType.PLANNER_FAILED in types


async def test_concurrent_replans_apply_only_once(sessionmaker: object) -> None:
    """Two replans computed against the same version: the second must not stack a version on stale state."""
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm)

    async def other_replan_wins() -> None:
        await Replanner(ScriptedChat([_replan_answer()]), sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)

    chat = ScriptedChat([Answer(content=_replan_answer(), before=other_replan_wins)])
    with pytest.raises(PlanConflict) as info:
        await Replanner(chat, sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)
    assert info.value.code == "PLAN_CHANGED"
    async with sm() as s:
        job = await s.get(Job, job_id)
        versions = list((await s.execute(select(PlanVersion).where(PlanVersion.job_id == job_id))).scalars())
    assert job is not None and job.replan_count == 1 and job.current_plan_version == 2 and len(versions) == 2
    active = [r for r in await _rows(sm, job_id) if not r.superseded]
    assert sorted(r.step_key for r in active) == ["S001", "S002", "S004"]
    created = [p for t, p in await _events(sm, job_id) if t == EventType.REPLAN_CREATED]
    assert len(created) == 1
