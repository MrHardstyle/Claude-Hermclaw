"""Bounded structured-output loop (P14 14.2 structured output, 14.4 plan repair).

Runs ``run_structured_loop`` against the scripted fake ChatModel only (no database): the repair budget, what is
echoed back to the model, redaction of error lists and the truncation hint.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hermclaw.contracts.plan import PlanContract
from hermclaw.core.redaction import REDACTED
from hermclaw.models.protocols import CallContext, ChatMessage
from hermclaw.planner.errors import PlanInvalid, PlannerError
from hermclaw.planner.inputs import PlannerSettings
from hermclaw.planner.loop import TRUNCATED_HINT, ModelCall, run_structured_loop
from hermclaw.planner.parsing import validate_schema
from hermclaw.planner.planner import plan_json_schema
from tests.unit.test_planner_support import FASTAPI, Answer, ScriptedChat

CALL = ModelCall(
    alias="planner-gemma",
    json_schema=plan_json_schema(),
    max_tokens=4096,
    temperature=0.2,
    timeout_seconds=60.0,
    purpose="planner",
)
BASE = [ChatMessage(role="system", content="rules"), ChatMessage(role="user", content='{"job":{}}')]
SECRET = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"


def _plan_validator(data: dict[str, Any]) -> PlanContract:
    return validate_schema(PlanContract, data)


async def _run(chat: ScriptedChat, validate: Any = _plan_validator, *, repairs: int = 2) -> Any:
    return await run_structured_loop(
        chat,
        CALL,
        list(BASE),
        validate,
        ctx=CallContext(purpose="planner"),
        settings=PlannerSettings(max_repair_attempts=repairs),
    )


async def test_valid_first_answer_uses_schema_constrained_call() -> None:
    chat = ScriptedChat([FASTAPI.answer()])
    outcome = await _run(chat)
    assert outcome.repair_attempts == 0 and outcome.validation_history == []
    call = chat.calls[0]
    assert call.alias == "planner-gemma" and call.json_schema == plan_json_schema()
    assert call.max_tokens == 4096 and call.temperature == 0.2 and call.timeout_seconds == 60.0
    assert [s.id for s in outcome.value.steps] == ["S001", "S002", "S003"]


async def test_prose_or_reasoning_inside_a_broken_answer_is_never_echoed() -> None:
    leaked = "I considered deleting the tests because they are slow"
    broken = '{"goal": "Paginate", "steps": [] } ' + leaked + ' {"note": 1}'
    chat = ScriptedChat([broken, FASTAPI.answer()])
    outcome = await _run(chat)
    assert outcome.repair_attempts == 1 and outcome.attempts[0].phase == "parse"
    repair = chat.calls[1].messages
    assert [m.role for m in repair] == ["system", "user", "user"]  # no assistant echo of unparseable text
    assert all(leaked not in m.content for m in repair)
    assert "invalid JSON" in repair[-1].content


async def test_only_the_parsed_json_object_is_echoed_canonically() -> None:
    bad = FASTAPI.answer()
    bad["steps"][0]["id"] = "one"
    chat = ScriptedChat(["Sure, here it is:\n```json\n" + json.dumps(bad, indent=2) + "\n```\nHope this helps!", FASTAPI.answer()])
    outcome = await _run(chat)
    echo = chat.calls[1].messages[2]
    assert echo.role == "assistant" and json.loads(echo.content) == bad
    assert "Sure" not in echo.content and "Hope" not in echo.content and "```" not in echo.content and "\n" not in echo.content
    assert outcome.attempts[0].phase == "schema"


async def test_oversized_answer_is_not_echoed() -> None:
    bad = FASTAPI.answer()
    bad["summary"] = "x" * 3000  # schema limit is 4000 -> valid; break the id instead
    bad["steps"][0]["id"] = "one"
    chat = ScriptedChat([bad, FASTAPI.answer()])
    await run_structured_loop(
        chat,
        CALL,
        list(BASE),
        _plan_validator,
        ctx=CallContext(purpose="planner"),
        settings=PlannerSettings(max_echo_chars=1000),
    )
    assert [m.role for m in chat.calls[1].messages] == ["system", "user", "user"]


async def test_truncated_answer_gets_a_length_hint() -> None:
    chat = ScriptedChat([Answer(content='{"goal": "Paginate", "steps": [{"id": "S0', finish_reason="length"), FASTAPI.answer()])
    outcome = await _run(chat)
    errors = outcome.attempts[0].errors
    assert errors[-1] == TRUNCATED_HINT and TRUNCATED_HINT in chat.calls[1].messages[-1].content


async def test_error_lists_are_redacted_everywhere() -> None:
    def validate(data: dict[str, Any]) -> PlanContract:
        hint = data["steps"][0]["repo_hints"][0]
        if "token=" in hint:
            raise PlanInvalid("semantic", [f"step S001: repo_hint '{hint}' is invalid"])
        return _plan_validator(data)

    bad = FASTAPI.answer()
    bad["steps"][0]["repo_hints"] = [f"token={SECRET}"]
    chat = ScriptedChat([bad, FASTAPI.answer()])
    outcome = await _run(chat, validate)
    stored = outcome.validation_history[0]["errors"][0]
    assert SECRET not in stored and REDACTED in stored
    assert all(SECRET not in m.content for m in chat.calls[1].messages)  # error list and echoed answer


async def test_repair_budget_is_bounded_and_history_complete() -> None:
    chat = ScriptedChat(["nope", "[1]", "{}"])
    with pytest.raises(PlannerError) as info:
        await _run(chat)
    exc = info.value
    assert len(chat.calls) == 3 and exc.code == "PLANNER_INVALID_OUTPUT"
    assert exc.details["attempts"] == 3 and exc.details["repair_attempts"] == 2
    assert [h["phase"] for h in exc.details["history"]] == ["parse", "parse", "schema"]
    assert "Repair attempts remaining after this one: 1" in chat.calls[1].messages[-1].content
    assert "Repair attempts remaining after this one: 0" in chat.calls[2].messages[-1].content


async def test_zero_repair_budget_means_a_single_call() -> None:
    chat = ScriptedChat(["nope", FASTAPI.answer()])
    with pytest.raises(PlannerError):
        await _run(chat, repairs=0)
    assert len(chat.calls) == 1


async def test_on_repair_hook_reports_remaining_budget() -> None:
    seen: list[tuple[int, str | None, int]] = []

    async def hook(record: Any, remaining: int) -> None:
        seen.append((record.attempt, record.phase, remaining))

    chat = ScriptedChat(["nope", "{}", FASTAPI.answer()])
    await run_structured_loop(
        chat, CALL, list(BASE), _plan_validator, ctx=CallContext(purpose="planner"), settings=PlannerSettings(), on_repair=hook
    )
    assert seen == [(1, "parse", 1), (2, "schema", 0)]


async def test_unscoped_implement_step_is_repaired_before_it_can_block() -> None:
    """BUG-143: the live plan for a README-only repository had implement steps without repo_hints and
    allowed_new_paths. Semantic validation now rejects it, and the repair turn tells the model where files go."""
    from hermclaw.core.config import get_config
    from hermclaw.planner.validation import ValidationContext, semantic_errors

    ctx = ValidationContext.build(get_config(), FASTAPI.inputs, PlannerSettings())

    def validate(data: dict[str, Any]) -> PlanContract:
        contract = validate_schema(PlanContract, data)
        errors = semantic_errors(contract, ctx)
        if errors:
            raise PlanInvalid("semantic", errors)
        return contract

    unscoped = {
        "goal": "Add a /health endpoint with a test.",
        "summary": "plan",
        "steps": [
            {
                "id": "S001",
                "title": "Implement /health",
                "kind": "implement",
                "capability": "coding",
                "goal": "Add a new route /health that returns ok.",
                "acceptance": [{"type": "diff", "must_change": ["*"], "must_not_change": [], "allow_empty": False}],
            },
        ],
    }
    fixed = json.loads(json.dumps(unscoped))
    fixed["steps"][0]["allowed_new_paths"] = ["app/health.py", "tests/test_health.py"]
    chat = ScriptedChat([unscoped, fixed])
    outcome = await _run(chat, validate)
    assert outcome.repair_attempts == 1 and outcome.attempts[0].phase == "semantic"
    repair = chat.calls[1].messages[-1].content
    assert "step S001: implement step names no files to work on" in repair and "allowed_new_paths" in repair
    assert outcome.value.steps[0].allowed_new_paths == ["app/health.py", "tests/test_health.py"]


async def test_live_first_plan_is_repaired_into_one_step_without_web_research() -> None:
    """Analysis 2026-10-16: the first plans of all live jobs had the same shape – web research about the repository
    and which framework to use, code and its test in separate steps, a verify step on top. All of it is rejected in
    one repair turn with the reasons, and the repaired plan is a single implement step with its tests."""
    from hermclaw.core.config import get_config
    from hermclaw.planner.validation import ValidationContext, semantic_errors

    ctx = ValidationContext.build(get_config(), FASTAPI.inputs, PlannerSettings())

    def validate(data: dict[str, Any]) -> PlanContract:
        contract = validate_schema(PlanContract, data)
        errors = semantic_errors(contract, ctx)
        if errors:
            raise PlanInvalid("semantic", errors)
        return contract

    diff = [{"type": "diff", "must_change": ["*"], "must_not_change": [], "allow_empty": False}]
    live = {  # plan v1 of live job e3cab55e, ids and texts as recorded
        "goal": "Add a /health endpoint that returns {'status': 'ok'}, with a test.",
        "summary": "plan",
        "research_needed": [
            {"question": "What is the programming language and web framework used in this repository?", "reason": "unknown"}
        ],
        "steps": [
            {"id": "S001", "title": "Discover project stack", "kind": "discover", "capability": "discover", "goal": "Find the stack."},
            {
                "id": "S002",
                "title": "Research framework implementation",
                "kind": "research",
                "capability": "research",
                "goal": "Research how to add a simple JSON health endpoint and a corresponding test in the identified framework.",
                "depends_on": ["S001"],
            },
            {
                "id": "S003",
                "title": "Implement health endpoint",
                "kind": "implement",
                "capability": "coding",
                "goal": "Implement the endpoint.",
                "allowed_new_paths": ["src/**/*", "app/**/*"],
                "depends_on": ["S002"],
                "acceptance": diff,
            },
            {
                "id": "S004",
                "title": "Implement health endpoint test",
                "kind": "implement",
                "capability": "coding",
                "goal": "Write the test.",
                "allowed_new_paths": ["tests/**/*"],
                "depends_on": ["S003"],
                "acceptance": diff,
            },
        ],
    }
    fixed = {
        "goal": live["goal"],
        "summary": "one step: endpoint and its test with the standard library",
        "assumptions": ["empty repository: Python standard library (http.server) and unittest, no dependencies"],
        "steps": [
            {
                "id": "S001",
                "title": "Health endpoint with test",
                "kind": "implement",
                "capability": "coding",
                "goal": "Implement the endpoint and its test.",
                "allowed_new_paths": ["app/", "tests/"],
                "acceptance": diff,
            }
        ],
    }
    chat = ScriptedChat([live, fixed])
    outcome = await _run(chat, validate)
    repair = chat.calls[1].messages[-1].content
    assert "research_needed[0]" in repair and "cannot be researched on the web" in repair
    assert "step S002: research step 'Research framework implementation' cannot be answered by web research" in repair
    assert "step S004 only writes tests for the code of step S003" in repair
    assert outcome.repair_attempts == 1 and len(outcome.value.steps) == 1


def test_model_facing_schemas_require_the_scope_fields() -> None:
    """BUG-159 (live job c04f9fb8): Ollama's grammar emits properties in schema order and lets the model skip
    optional ones; Gemma skipped repo_hints/allowed_new_paths and could not add them after rerun_reason. The schema
    sent to the model requires them (empty lists allowed); the contracts themselves keep their defaults."""
    from hermclaw.contracts.plan import PlanStep
    from hermclaw.planner.replanner import replan_json_schema

    for schema, name in ((plan_json_schema(), "PlanStep"), (replan_json_schema(), "ReplanStep")):
        required = schema["$defs"][name]["required"]
        assert {"depends_on", "repo_hints", "allowed_new_paths", "acceptance"} <= set(required), required
        assert "rerun_reason" not in required
        # BUG-288 (K2 on rc60): qwen3.8 repeated two diff criteria up to the contract bound for 25 minutes
        props = schema["$defs"][name]["properties"]
        assert props["acceptance"]["maxItems"] == 8 and props["repo_hints"]["maxItems"] == 20
    assert (
        PlanStep.model_validate({"id": "S001", "title": "abc", "kind": "research", "capability": "research", "goal": "find it"}).repo_hints
        == []
    )


def test_model_facing_schemas_leave_out_what_the_runtime_adds() -> None:
    """BUG-320 (ladder A7 #8 on rc71): qwen3.8 wrote scope and security up to eight times per step and restated the job
    as the plan goal – a third of the plan's output at 3–4 tok/s. The model-facing schema has no scope/security
    evidence and no summary and keeps goal, assumptions and risks short; the contracts still accept all of them."""
    import json

    from hermclaw.contracts.plan import PlanContract
    from hermclaw.planner.prompt import planner_system_prompt
    from hermclaw.planner.replanner import replan_json_schema

    for schema, name in ((plan_json_schema(), "PlanStep"), (replan_json_schema(), "ReplanStep")):
        text = json.dumps(schema)
        assert "ScopeEvidence" not in text and "SecurityEvidence" not in text
        items = schema["$defs"][name]["properties"]["acceptance"]["items"]
        assert {"scope", "security"}.isdisjoint(items["discriminator"]["mapping"])
        assert {"test", "diff", "command"} <= set(items["discriminator"]["mapping"])
        props = schema["properties"]
        assert "summary" not in props and props["goal"]["maxLength"] == 300
        assert props["assumptions"]["maxItems"] == 3 and props["risks"]["items"]["maxLength"] == 200
    system = planner_system_prompt({"implement": "coding"}, ["coding"], 8)
    assert '"type":"scope"' not in system and "adds the scope check" in system and "goal is one sentence" in system
    # BUG-321 (A7 #10 on rc72): the same diff criterion twelve times – one diff criterion per step, at most eight items
    assert "one diff criterion holds all must_change and must_not_change paths" in system
    plan = PlanContract.model_validate(
        {
            "goal": "x" * 1000,
            "summary": "kept",
            "steps": [
                {
                    "id": "S001",
                    "title": "abc",
                    "kind": "implement",
                    "capability": "coding",
                    "goal": "do it",
                    "acceptance": [{"type": "scope"}],
                }
            ],
        }
    )
    assert plan.summary == "kept" and plan.steps[0].acceptance[0].type == "scope"


async def test_a_repair_that_changes_nothing_raises_the_temperature() -> None:
    """BUG-188 (rc16 battery J5): two repair turns at 0.2 reproduced the same runaway answer verbatim."""
    bad = FASTAPI.answer()
    bad["steps"][0]["id"] = "one"
    chat = ScriptedChat([bad, dict(bad), FASTAPI.answer()])
    outcome = await _run(chat)
    assert outcome.repair_attempts == 2
    assert [c.temperature for c in chat.calls] == [0.2, 0.2, PlannerSettings().repeat_temperature]
    fixed = ScriptedChat([bad, FASTAPI.answer()])
    await _run(fixed)
    assert [c.temperature for c in fixed.calls] == [0.2, 0.2]


async def test_a_repair_turn_keeps_the_rules_earlier_answers_broke() -> None:
    """Live G1 (BUG-261): repair 1 merged the separate test steps into their code step, repair 2 fixed the resulting
    "too many new files" by splitting the tests off again – each turn only saw the latest errors – and the job failed
    with PLANNER_INVALID_OUTPUT. A repair turn lists the errors of earlier answers as rules that still hold."""
    first, second = FASTAPI.answer(), FASTAPI.answer()
    first["steps"][0]["id"] = "one"  # schema error of the first answer
    second["steps"][1]["id"] = "two"  # a different error in the second
    chat = ScriptedChat([first, second, FASTAPI.answer()])
    await _run(chat)
    second_repair = chat.calls[2].messages[-1].content
    first_errors = chat.calls[1].messages[-1].content.split("\n")[1]
    assert "Earlier answers were rejected for the following" in second_repair
    assert first_errors in second_repair.split("Earlier answers")[1], (first_errors, second_repair)
    assert "Earlier answers" not in chat.calls[1].messages[-1].content
