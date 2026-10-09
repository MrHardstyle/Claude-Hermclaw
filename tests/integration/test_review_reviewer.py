"""HeavyReviewer against real PostgreSQL with a scripted fake ChatModel (22.2–22.5): persistence, events, invariants."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.contracts.verification import VerificationCheck
from hermclaw.review import (
    REVIEW_SYSTEM_PROMPT,
    HeavyReviewer,
    ReviewDraft,
    ReviewInput,
)
from hermclaw.review.types import OVERRIDE_BLOCKING_FINDING, OVERRIDE_VERIFIER_FAILED
from tests.integration.test_review_support import (
    SIMPLE_DIFF,
    ScriptedChatModel,
    config,
    events_for,
    load_run,
    make_job_step,
    make_step,
    report,
)

pytestmark = pytest.mark.integration

SM = async_sessionmaker[AsyncSession]


async def _input(sm: SM, *, kind: str = "implement", diff: str = SIMPLE_DIFF, passed: bool = True, **kw: Any) -> ReviewInput:
    job_id, step_id = await make_job_step(sm, kind=kind)
    failures = kw.pop("failures", ())
    return ReviewInput(
        job_id=job_id,
        step_id=step_id,
        attempt_id=None,
        goal="Calculator: extend with subtraction",
        step=make_step(job_id, step_id, kind=kind),
        diff=diff,
        verification=report(passed=passed, failures=failures),
        **kw,
    )


async def test_pass_is_persisted_with_events(sessionmaker: SM) -> None:
    chat = ScriptedChatModel([{"verdict": "pass", "findings": [], "summary": "sub() is correct and tested"}])
    reviewer = HeavyReviewer(chat, sessionmaker, config())
    inp = await _input(sessionmaker)

    outcome = await reviewer.review(inp)

    assert outcome.status == "completed"
    assert outcome.passed and outcome.verdict == "pass"
    assert outcome.raw_verdict == "pass" and not outcome.invariant_override
    assert outcome.reason == "sub() is correct and tested"
    # the model call: heavy alias, tolerant ReviewContract schema, review purpose, system prompt first
    call = chat.calls[0]
    heavy = reviewer.profile()
    assert call.alias == heavy.alias == "heavy-review"
    assert heavy.role == "heavy" and heavy.model.startswith("qwen3.8") and heavy.think is False  # 22.2 profile
    assert 24_000 <= heavy.context_tokens <= 32_768
    assert call.schema is ReviewDraft
    assert call.ctx.purpose == "review" and call.ctx.step_id == inp.step_id and call.ctx.job_id == inp.job_id
    assert call.messages[0].role == "system" and call.messages[0].content == REVIEW_SYSTEM_PROMPT
    assert "+def sub(a, b):" in call.messages[1].content
    assert call.max_repairs == reviewer.settings.max_repairs
    assert call.max_tokens == heavy.max_output_tokens and call.temperature == heavy.temperature
    assert call.timeout_seconds is not None and call.timeout_seconds <= reviewer.policy.timeout_seconds

    run, rows = await load_run(sessionmaker, outcome.review_run_id)
    assert run.status == "completed" and run.verdict == "pass" and run.raw_verdict == "pass"
    assert run.invariant_override is False and run.finished_at is not None
    assert run.model_alias == "heavy-review" and run.step_id == inp.step_id
    assert rows == []
    types = [e.event_type for e in await events_for(sessionmaker, inp.step_id)]
    assert types == [EventType.REVIEW_STARTED, EventType.REVIEW_FINISHED]


async def test_fix_required_findings_rows_and_events_in_severity_order(sessionmaker: SM) -> None:
    answer = {
        "verdict": "fix_required",
        "summary": "sub() misses a test",
        "findings": [
            {"severity": "minor", "path": "app.py", "summary": "missing docstring", "evidence": "+def sub(a, b):"},
            {
                "severity": "blocker",
                "path": "./app.py:5",
                "summary": "no test for sub()",
                "evidence": "no tests/test_app.py in the diff",
                "suggested_fix": "add tests/test_app.py::test_sub",
            },
            {"severity": "major", "path": "app.py", "summary": "sub does not validate input types", "evidence": "+    return a - b"},
        ],
    }
    chat = ScriptedChatModel([answer])
    inp = await _input(sessionmaker)
    outcome = await HeavyReviewer(chat, sessionmaker, config()).review(inp)

    assert outcome.status == "completed" and outcome.verdict == "fix_required" and not outcome.passed
    assert not outcome.invariant_override  # the model already said fix_required
    assert [f.severity.value for f in outcome.review.findings] == ["blocker", "major", "minor"]
    blocker = outcome.review.findings[0]
    assert blocker.path == "app.py" and blocker.evidence.startswith("line 5")  # path:line split (22.4)
    assert len(outcome.finding_ids) == 3

    run, rows = await load_run(sessionmaker, outcome.review_run_id)
    assert run.verdict == "fix_required" and run.raw_verdict == "fix_required"
    assert sorted(r.severity for r in rows) == ["blocker", "major", "minor"]
    assert {r.id for r in rows} == set(outcome.finding_ids)
    by_sev = {r.severity: r for r in rows}
    assert by_sev["blocker"].suggested_fix == "add tests/test_app.py::test_sub"

    events = await events_for(sessionmaker, inp.step_id)
    types = [e.event_type for e in events]
    assert types[0] == EventType.REVIEW_STARTED and types[-1] == EventType.REVIEW_FINISHED
    created = [e for e in events if e.event_type == EventType.REVIEW_FINDING_CREATED]
    assert [e.payload["severity"] for e in created] == ["blocker", "major", "minor"]
    assert {e.payload["finding_id"] for e in created} == {str(i) for i in outcome.finding_ids}
    finished = events[-1].payload
    assert finished["findings"] == {"minor": 1, "major": 1, "blocker": 1}
    assert finished["verdict"] == "fix_required" and finished["status"] == "completed"
    assert events[-1].severity == "warning"


async def test_pass_with_major_is_overridden(sessionmaker: SM) -> None:
    answer = {
        "verdict": "pass",
        "findings": [{"severity": "major", "path": "app.py", "summary": "sub swaps its operands", "evidence": "+    return b - a"}],
    }
    inp = await _input(sessionmaker)
    outcome = await HeavyReviewer(ScriptedChatModel([answer]), sessionmaker, config()).review(inp)

    assert outcome.status == "completed"
    assert outcome.raw_verdict == "pass" and outcome.verdict == "fix_required" and not outcome.passed
    assert outcome.invariant_override and outcome.override_reasons == (OVERRIDE_BLOCKING_FINDING,)
    assert "invariant override" in outcome.reason
    run, rows = await load_run(sessionmaker, outcome.review_run_id)
    assert (run.raw_verdict, run.verdict, run.invariant_override) == ("pass", "fix_required", True)
    assert [r.severity for r in rows] == ["major"]
    finished = (await events_for(sessionmaker, inp.step_id))[-1]
    assert finished.payload["invariant_override"] is True and finished.payload["raw_verdict"] == "pass"


async def test_severity_synonyms_are_normalised_before_the_invariant(sessionmaker: SM) -> None:
    answer = {"decision": "approved", "issues": [{"level": "CRITICAL", "file": "app.py", "message": "sub() deletes data"}]}
    inp = await _input(sessionmaker)
    outcome = await HeavyReviewer(ScriptedChatModel([answer]), sessionmaker, config()).review(inp)
    assert outcome.review.findings[0].severity.value == "blocker"
    assert outcome.raw_verdict == "pass" and outcome.verdict == "fix_required" and outcome.invariant_override
    assert any("severity 'CRITICAL' -> blocker" in n for n in outcome.normalisation_notes)


async def test_failed_verifier_never_passes(sessionmaker: SM) -> None:
    fail = VerificationCheck(check_type="unit", name="pytest", status="fail", message="1 failed")
    inp = await _input(sessionmaker, passed=False, failures=[fail])
    outcome = await HeavyReviewer(ScriptedChatModel([{"verdict": "pass", "findings": []}]), sessionmaker, config()).review(inp)
    assert outcome.verdict == "fix_required" and outcome.invariant_override
    assert outcome.override_reasons == (OVERRIDE_VERIFIER_FAILED,)
    run, _ = await load_run(sessionmaker, outcome.review_run_id)
    assert run.verdict == "fix_required" and run.raw_verdict == "pass" and run.invariant_override


async def test_minor_findings_still_pass(sessionmaker: SM) -> None:
    answer = {"verdict": "pass", "findings": [{"severity": "nit", "path": "app.py", "summary": "name could be clearer"}]}
    inp = await _input(sessionmaker)
    outcome = await HeavyReviewer(ScriptedChatModel([answer]), sessionmaker, config()).review(inp)
    assert outcome.passed and outcome.review.findings[0].severity.value == "minor"
    _, rows = await load_run(sessionmaker, outcome.review_run_id)
    assert [r.severity for r in rows] == ["minor"]
    created = [e for e in await events_for(sessionmaker, inp.step_id) if e.event_type == EventType.REVIEW_FINDING_CREATED]
    assert created[0].severity == "info"


async def test_repair_then_valid_answer(sessionmaker: SM) -> None:
    chat = ScriptedChatModel(["not json at all", {"verdict": "maybe"}, {"verdict": "fix_required", "findings": []}])
    inp = await _input(sessionmaker)
    outcome = await HeavyReviewer(chat, sessionmaker, config()).review(inp)
    assert outcome.status == "completed" and outcome.repair_attempts == 2 and outcome.verdict == "fix_required"


async def test_secrets_and_reasoning_never_reach_db_or_events(sessionmaker: SM) -> None:
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    answer = {
        "verdict": "fix_required",
        "reasoning": "PRIVATE CHAIN OF THOUGHT",
        "summary": "<think>PRIVATE CHAIN OF THOUGHT</think>token leaked",
        "findings": [
            {
                "severity": "blocker",
                "path": "app.py",
                "summary": f"hard-coded token {secret}",
                "evidence": f"TOKEN = '{secret}'",
                "thinking": "PRIVATE CHAIN OF THOUGHT",
            }
        ],
    }
    inp = await _input(sessionmaker)
    outcome = await HeavyReviewer(ScriptedChatModel([answer]), sessionmaker, config()).review(inp)
    assert secret not in repr(outcome.review) and "PRIVATE" not in repr(outcome)
    run, rows = await load_run(sessionmaker, outcome.review_run_id)
    blob = repr([run.summary, *[(r.summary, r.evidence) for r in rows]])
    assert secret not in blob and "PRIVATE" not in blob
    events = await events_for(sessionmaker, inp.step_id)
    payloads = repr([e.payload for e in events])
    assert secret not in payloads and "PRIVATE CHAIN" not in payloads


async def test_concurrent_reviews_are_isolated(sessionmaker: SM) -> None:
    inputs = [await _input(sessionmaker) for _ in range(4)]
    reviewers = [
        HeavyReviewer(
            ScriptedChatModel(
                [{"verdict": "fix_required", "findings": [{"severity": "major", "path": "app.py", "summary": f"issue {i}"}]}]
            ),
            sessionmaker,
            config(),
        )
        for i in range(4)
    ]
    outcomes = await asyncio.gather(*(r.review(i) for r, i in zip(reviewers, inputs, strict=True)))
    assert len({o.review_run_id for o in outcomes}) == 4
    for i, (inp, out) in enumerate(zip(inputs, outcomes, strict=True)):
        run, rows = await load_run(sessionmaker, out.review_run_id)
        assert run.step_id == inp.step_id and [r.summary for r in rows] == [f"issue {i}"]


async def test_should_review_follows_policy() -> None:
    reviewer = HeavyReviewer(ScriptedChatModel(), None, config(required_for_kinds=["implement", "Database"]))  # type: ignore[arg-type]
    assert reviewer.should_review("implement", True)
    assert reviewer.should_review("database", True)
    assert not reviewer.should_review("implement", False)  # a failed verification goes straight to correction
    assert not reviewer.should_review("documentation", True)
    assert not reviewer.should_review("test", True)
