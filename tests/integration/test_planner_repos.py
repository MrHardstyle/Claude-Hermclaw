"""Planner across unrelated repositories against real PostgreSQL (P14 14.1-14.10).

A scripted fake ChatModel stands in for Gemma; everything else (validation, enrichment, persistence, events) is
the production code path.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.contracts.plan import PlanContract
from hermclaw.core.config import get_config
from hermclaw.events.store import list_events
from hermclaw.persistence.models import Job, Plan, PlanVersion, Step, StepDependency
from hermclaw.planner import PlanConflict, Planner, PlannerError
from hermclaw.planner.prompt import PLANNER_INPUT_KEYS
from tests.unit.test_planner_support import (
    FASTAPI,
    LINUX_ADMIN,
    PHP,
    REACT,
    REPOS,
    YAML_CONFIG,
    Answer,
    RepoFixture,
    ScriptedChat,
    create_job,
    step,
)

pytestmark = pytest.mark.integration


def _sm(sessionmaker: object) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], sessionmaker)


async def _steps(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> dict[str, Step]:
    async with sm() as s:
        rows = (await s.execute(select(Step).where(Step.job_id == job_id, Step.superseded.is_(False)))).scalars()
        return {r.step_key: r for r in rows}


async def _deps(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> dict[str, set[str]]:
    async with sm() as s:
        steps = {r.id: r.step_key for r in (await s.execute(select(Step).where(Step.job_id == job_id))).scalars()}
        out: dict[str, set[str]] = {}
        for dep in (await s.execute(select(StepDependency).where(StepDependency.step_id.in_(list(steps))))).scalars():
            out.setdefault(steps[dep.step_id], set()).add(steps[dep.depends_on_step_id])
        return out


async def _events(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> list[tuple[str, dict[str, Any], str]]:
    async with sm() as s:
        return [(e.event_type, e.payload, e.severity) for e in await list_events(s, job_id=job_id)]


async def _plan_and_versions(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> tuple[Plan | None, list[PlanVersion], Job]:
    async with sm() as s:
        plan = (await s.execute(select(Plan).where(Plan.job_id == job_id))).scalar_one_or_none()
        versions = list((await s.execute(select(PlanVersion).where(PlanVersion.job_id == job_id).order_by(PlanVersion.version))).scalars())
        job = await s.get(Job, job_id)
        assert job is not None
        return plan, versions, job


def _acc(row: Step) -> list[str]:
    return [a["type"] for a in row.acceptance]


# --------------------------------------------------------------------------------------------- happy path per repo
@pytest.mark.parametrize("fixture", REPOS, ids=[f.name for f in REPOS])
async def test_plan_created_for_unrelated_repositories(sessionmaker: object, fixture: RepoFixture) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, fixture)
    chat = ScriptedChat([fixture.answer()])
    result = await Planner(chat, sm, get_config()).create_plan(job_id, fixture.inputs)

    # structured call against the planner alias with the PlanContract schema (14.2)
    assert len(chat.calls) == 1
    call = chat.calls[0]
    assert call.alias == "planner-gemma"
    assert call.json_schema == PlanContract.model_json_schema()
    assert call.ctx.purpose == "planner" and call.ctx.job_id == job_id
    assert [m.role for m in call.messages] == ["system", "user"]
    assert tuple(chat.user_payload()) == PLANNER_INPUT_KEYS
    assert "never write code" in call.messages[0].content

    # persisted plan + version + steps (14.3)
    plan_row, versions, job = await _plan_and_versions(sm, job_id)
    assert plan_row is not None and plan_row.current_version == 1 and plan_row.status == "active"
    assert [v.version for v in versions] == [1]
    assert versions[0].source == "planner" and versions[0].model_alias == "planner-gemma" and versions[0].repair_attempts == 0
    assert PlanContract.model_validate(versions[0].plan_json) == result.plan
    assert job.current_plan_version == 1 and job.metadata_["planner_model_alias"] == "planner-gemma"
    assert job.status == "planning"  # job transitions are the orchestrator's job

    rows = await _steps(sm, job_id)
    assert set(rows) == {s.id for s in result.plan.steps} == set(result.step_ids)
    deps = await _deps(sm, job_id)
    for s in result.plan.steps:
        row = rows[s.id]
        assert row.status == "pending" and not row.superseded and row.plan_version_id == versions[0].id
        assert row.kind == s.kind.value and row.capability == s.capability and row.risk == s.risk.value
        assert row.max_attempts == get_config().policies.correction.max_attempts_per_step
        assert row.priority == 60 and row.current_scope_version is None
        assert deps.get(s.id, set()) == set(s.depends_on)

    types = [e[0] for e in await _events(sm, job_id)]
    assert types[0] == EventType.PLANNER_INVOKED
    assert EventType.PLANNER_PLAN_CREATED in types
    assert types.count(EventType.STEP_CREATED) == len(result.plan.steps)
    assert EventType.PLANNER_FAILED not in types and EventType.PLANNER_FALLBACK_USED not in types


async def test_fastapi_acceptance_generation_and_turn_budget(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, FASTAPI)
    await Planner(ScriptedChat([FASTAPI.answer()]), sm, get_config()).create_plan(job_id, FASTAPI.inputs)
    rows = await _steps(sm, job_id)
    # model gave substantive acceptance -> only scope + security added
    assert _acc(rows["S001"]) == ["presence", "test", "scope", "security"]
    # documentation step had none -> diff over its path hints + detected test command + scope + security (14.8)
    assert _acc(rows["S002"]) == ["diff", "test", "scope", "security"]
    diff = rows["S002"].acceptance[0]
    assert diff["must_change"] == ["README.md"] and diff["allow_empty"] is False
    assert rows["S002"].acceptance[1]["command"] == "pytest -q" and rows["S002"].acceptance[1]["framework"] == "pytest"
    assert _acc(rows["S003"]) == []  # review is not mutating
    coder_turns = get_config().policies.coder.max_turns
    assert rows["S001"].turn_budget == coder_turns and rows["S002"].turn_budget == coder_turns
    assert rows["S003"].turn_budget == 0


async def test_php_research_request_becomes_research_step(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, PHP)
    result = await Planner(ScriptedChat([PHP.answer()]), sm, get_config()).create_plan(job_id, PHP.inputs)
    rows = await _steps(sm, job_id)
    research = rows["S003"]
    assert research.kind == "research" and research.capability == "research" and research.network is True
    assert "OWASP" in research.goal
    deps = await _deps(sm, job_id)
    assert deps["S001"] == {"S003"}  # the first work step waits for the research (14.9)
    assert deps["S002"] == {"S001"}
    assert result.plan.topological_order().index("S003") < result.plan.topological_order().index("S001")
    # symbol hints are not diff targets; the PHPUnit command is detected from the inventory
    assert rows["S001"].acceptance[0] == {
        "description": "generated: the step must change its target paths",
        "type": "diff",
        "must_change": ["src/Service/AuthService.php"],
        "must_not_change": [],
        "max_changed_files": None,
        "allow_empty": False,
    }
    assert rows["S001"].acceptance[1]["command"] == "vendor/bin/phpunit" and rows["S001"].acceptance[1]["framework"] == "phpunit"
    created = next(e for e in await _events(sm, job_id) if e[0] == EventType.PLANNER_PLAN_CREATED)
    assert created[1]["research_steps"] == ["S003"]


async def test_react_directory_hint_and_new_paths(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, REACT)
    chat = ScriptedChat([REACT.answer()])
    await Planner(chat, sm, get_config()).create_plan(job_id, REACT.inputs)
    rows = await _steps(sm, job_id)
    assert rows["S002"].allowed_new_paths == ["src/components/FilterToggle.tsx"]
    assert _acc(rows["S002"]) == ["presence", "scope", "security"]
    assert chat.user_payload()["repository_inventory"]["test_command"] == "npm test -- --run"


async def test_yaml_repo_without_test_command_gets_no_test_evidence(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, YAML_CONFIG)
    answer = YAML_CONFIG.answer()
    answer["steps"][0]["acceptance"] = []  # force generation
    await Planner(ScriptedChat([answer]), sm, get_config()).create_plan(job_id, YAML_CONFIG.inputs)
    rows = await _steps(sm, job_id)
    assert _acc(rows["S001"]) == ["diff", "scope", "security"]
    assert rows["S001"].acceptance[0]["must_change"] == ["config/app.yaml", "config/logging.yaml"]


async def test_linux_admin_task_risk_floor_capabilities_and_constraints(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, LINUX_ADMIN)
    chat = ScriptedChat([LINUX_ADMIN.answer()])
    result = await Planner(chat, sm, get_config()).create_plan(job_id, LINUX_ADMIN.inputs)
    rows = await _steps(sm, job_id)
    assert rows["S001"].risk == "medium"  # ssh is at least medium (14.7)
    assert _acc(rows["S001"]) == ["command"]  # remote steps keep the model's machine-checkable criteria only
    assert rows["S003"].kind == "research"
    assert (await _deps(sm, job_id))["S001"] == {"S003"}
    payload = chat.user_payload()
    assert payload["constraints"] == ["do not restart nginx during business hours"]
    assert sorted(c["name"] for c in payload["capabilities"]) == ["research", "review", "ssh"]
    system = chat.calls[0].messages[0].content
    assert "ssh->ssh" in system and "implement->coding" not in system
    assert any("risk raised low -> medium" in n for n in result.notes)


# --------------------------------------------------------------------------------------------- repair loop (14.4)
async def test_semantic_repair_loop_repairs_invalid_first_answer(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, FASTAPI)
    bad = FASTAPI.answer()
    bad["steps"][0]["repo_hints"] = ["app/routers/orders.py"]  # invented path
    bad["steps"][1]["capability"] = "python"  # unknown capability
    bad["steps"][2]["depends_on"] = []  # review without dependency
    chat = ScriptedChat([Answer(content="Here is the plan:\n```json\n" + __import__("json").dumps(bad) + "\n```"), FASTAPI.answer()])
    result = await Planner(chat, sm, get_config()).create_plan(job_id, FASTAPI.inputs)

    assert result.repair_attempts == 1 and len(chat.calls) == 2
    errors = result.validation_errors[0]["errors"]
    assert result.validation_errors[0]["phase"] == "semantic"
    assert any("repo_hint 'app/routers/orders.py' does not exist" in e for e in errors)
    assert any("unknown capability 'python'" in e for e in errors)
    assert any("step S003: review step must depend on" in e for e in errors)

    repair_turn = chat.calls[1].messages
    assert [m.role for m in repair_turn] == ["system", "user", "assistant", "user"]
    assert repair_turn[2].content.startswith("{") and "Here is the plan" not in repair_turn[2].content  # JSON only, no prose
    for err in errors:
        assert err in repair_turn[3].content
    assert "Repair attempts remaining after this one: 1" in repair_turn[3].content

    _, versions, _ = await _plan_and_versions(sm, job_id)
    assert versions[0].repair_attempts == 1 and versions[0].validation_errors[0]["errors"] == errors
    repairs = [e for e in await _events(sm, job_id) if e[0] == EventType.PLANNER_REPAIR]
    assert len(repairs) == 1 and repairs[0][1]["errors"] == errors and repairs[0][2] == "warning"


async def test_schema_invalid_answer_is_repaired(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, YAML_CONFIG)
    bad = YAML_CONFIG.answer()
    bad["steps"][0]["id"] = "step-1"
    bad["steps"][0]["kind"] = "refactor"
    bad["extra"] = True
    chat = ScriptedChat([bad, YAML_CONFIG.answer()])
    result = await Planner(chat, sm, get_config()).create_plan(job_id, YAML_CONFIG.inputs)
    hist = result.validation_errors[0]
    assert hist["phase"] == "schema"
    joined = "\n".join(hist["errors"])
    assert "steps[0](step-1).id" in joined and "steps[0](step-1).kind" in joined and "extra: unknown field" in joined


async def test_failure_after_two_repairs_raises_planner_error(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, REACT)
    bad = REACT.answer()
    bad["steps"][1]["repo_hints"] = ["/home/user/project/src/App.tsx"]
    chat = ScriptedChat(["not json at all", bad, Answer(content="", reasoning_chars=900)])
    with pytest.raises(PlannerError) as info:
        await Planner(chat, sm, get_config()).create_plan(job_id, REACT.inputs)
    exc = info.value
    assert exc.code == "PLANNER_INVALID_OUTPUT"
    assert len(chat.calls) == 3 and exc.details["repair_attempts"] == 2
    assert [h["phase"] for h in exc.details["history"]] == ["parse", "semantic", "parse"]
    assert "reasoning is never accepted" in exc.details["last_errors"][0]

    plan_row, versions, job = await _plan_and_versions(sm, job_id)
    assert plan_row is None and versions == [] and job.current_plan_version is None
    assert await _steps(sm, job_id) == {}
    events = await _events(sm, job_id)
    assert [e[0] for e in events].count(EventType.PLANNER_REPAIR) == 2
    failed = [e for e in events if e[0] == EventType.PLANNER_FAILED]
    assert len(failed) == 1 and failed[0][2] == "error"
    assert failed[0][1]["error_code"] == "PLANNER_INVALID_OUTPUT" and len(failed[0][1]["history"]) == 3


# --------------------------------------------------------------------------------------------- fallback + conflicts
async def test_gateway_fallback_is_recorded(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, FASTAPI)
    chat = ScriptedChat([Answer(content=FASTAPI.answer(), fallback_used=True)])
    result = await Planner(chat, sm, get_config()).create_plan(job_id, FASTAPI.inputs)
    assert result.source == "fallback" and result.fallback_used and result.model_alias == "planner-gemma-fallback"
    _, versions, job = await _plan_and_versions(sm, job_id)
    assert versions[0].source == "fallback" and versions[0].model_alias == "planner-gemma-fallback"
    assert job.metadata_["planner_model_fallback"] is True
    fb = [e for e in await _events(sm, job_id) if e[0] == EventType.PLANNER_FALLBACK_USED]
    assert len(fb) == 1 and fb[0][2] == "warning"
    assert fb[0][1]["requested_alias"] == "planner-gemma" and fb[0][1]["served_aliases"] == ["planner-gemma-fallback"]


async def test_second_initial_plan_is_refused(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, YAML_CONFIG)
    await Planner(ScriptedChat([YAML_CONFIG.answer()]), sm, get_config()).create_plan(job_id, YAML_CONFIG.inputs)
    chat = ScriptedChat([YAML_CONFIG.answer()])
    with pytest.raises(PlanConflict) as info:
        await Planner(chat, sm, get_config()).create_plan(job_id, YAML_CONFIG.inputs)
    assert info.value.code == "PLAN_EXISTS" and chat.calls == []


async def test_unknown_job_is_not_found(sessionmaker: object) -> None:
    from hermclaw.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await Planner(ScriptedChat([]), _sm(sessionmaker), get_config()).create_plan(uuid.uuid4(), FASTAPI.inputs)


async def test_research_step_already_present_is_not_duplicated(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, PHP)
    answer = PHP.answer()
    question = answer["research_needed"][0]["question"]
    answer["steps"].insert(0, step("S009", "research", "research", question, network=True))
    answer["steps"][1]["depends_on"] = ["S009"]
    result = await Planner(ScriptedChat([answer]), sm, get_config()).create_plan(job_id, PHP.inputs)
    assert [s.id for s in result.plan.steps if s.kind.value == "research"] == ["S009"]
    assert set(await _steps(sm, job_id)) == {"S001", "S002", "S009"}


async def test_concurrent_initial_planning_keeps_exactly_one_plan(sessionmaker: object) -> None:
    """Two planners race for the same job: the one that persists second must not create a second plan."""
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, FASTAPI)
    winner: dict[str, Any] = {}

    async def other_planner_finishes_first() -> None:
        result = await Planner(ScriptedChat([FASTAPI.answer()]), sm, get_config()).create_plan(job_id, FASTAPI.inputs)
        winner["result"] = result

    loser = ScriptedChat([Answer(content=FASTAPI.answer(), before=other_planner_finishes_first)])
    with pytest.raises(PlanConflict) as info:
        await Planner(loser, sm, get_config()).create_plan(job_id, FASTAPI.inputs)
    assert info.value.code == "PLAN_EXISTS"

    plan_row, versions, job = await _plan_and_versions(sm, job_id)
    assert plan_row is not None and plan_row.id == winner["result"].plan_id
    assert [v.version for v in versions] == [1] and job.current_plan_version == 1
    rows = await _steps(sm, job_id)
    assert {k: r.id for k, r in rows.items()} == winner["result"].step_ids
    async with sm() as s:
        all_rows = list((await s.execute(select(Step).where(Step.job_id == job_id))).scalars())
    assert len(all_rows) == 3
    events = await _events(sm, job_id)
    assert [e[0] for e in events].count(EventType.PLANNER_PLAN_CREATED) == 1
    failed = [e for e in events if e[0] == EventType.PLANNER_FAILED]
    assert len(failed) == 1 and failed[0][1]["error_code"] == "PLAN_EXISTS"


async def test_inventory_paths_outside_files_ground_hints_end_to_end(sessionmaker: object) -> None:
    """Hints to paths listed in other inventory sections are accepted without a repair turn."""
    sm = _sm(sessionmaker)
    job_id = await create_job(sm, YAML_CONFIG)
    inputs = YAML_CONFIG.inputs.model_copy(
        update={"repository_inventory": {**YAML_CONFIG.inputs.repository_inventory, "helm": {"charts": ["deploy/chart/values.yaml"]}}}
    )
    answer = YAML_CONFIG.answer()
    answer["steps"][0]["repo_hints"] = ["config/app.yaml", "deploy/chart/values.yaml"]
    chat = ScriptedChat([answer])
    result = await Planner(chat, sm, get_config()).create_plan(job_id, inputs)
    assert result.repair_attempts == 0 and len(chat.calls) == 1
