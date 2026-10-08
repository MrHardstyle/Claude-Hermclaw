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
