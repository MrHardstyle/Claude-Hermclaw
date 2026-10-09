"""Heavy-review failure modes: every error is persisted as status 'error' with the fail-closed verdict fix_required."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.errors import ModelError, ModelTimeout
from hermclaw.review import (
    EMPTY_DIFF,
    REVIEW_CANCELLED,
    REVIEW_CONTEXT_ERROR,
    REVIEW_INTERNAL_ERROR,
    REVIEW_PROFILE_MISSING,
    REVIEW_TIMEOUT,
    HeavyReviewer,
    ReviewInput,
    ReviewOutcome,
    build_correction_request,
)
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


async def _input(sm: SM, *, kind: str = "implement", diff: str = SIMPLE_DIFF, commands: tuple[str, ...] = ()) -> ReviewInput:
    job_id, step_id = await make_job_step(sm, kind=kind)
    return ReviewInput(
        job_id=job_id,
        step_id=step_id,
        attempt_id=None,
        goal="goal",
        step=make_step(job_id, step_id, kind=kind),
        diff=diff,
        verification=report(),
        command_log=commands,
    )


async def _assert_fail_closed(sm: SM, outcome: ReviewOutcome, code: str) -> None:
    assert outcome.status == "error" and outcome.fail_closed
    assert outcome.verdict == "fix_required" and not outcome.passed
    assert outcome.error_code == code
    assert outcome.reason.startswith("fail-closed:")
    run, rows = await load_run(sm, outcome.review_run_id)
    assert run.status == "error" and run.verdict == "fix_required" and run.finished_at is not None
    assert run.summary == outcome.reason and rows == []
    events = await events_for(sm, run.step_id)
    assert [e.event_type for e in events] == [EventType.REVIEW_STARTED, EventType.REVIEW_FINISHED]
    finished = events[-1]
    assert finished.severity == "error"
    assert finished.payload["status"] == "error" and finished.payload["fail_closed"] is True
    assert finished.payload["error_code"] == code and finished.payload["verdict"] == "fix_required"


async def test_invalid_output_after_repairs_fails_closed(sessionmaker: SM) -> None:
    chat = ScriptedChatModel(["I think it is fine", {"verdict": "looks good"}, {"findings": "none"}])
    outcome = await HeavyReviewer(chat, sessionmaker, config()).review(await _input(sessionmaker))
    await _assert_fail_closed(sessionmaker, outcome, "MODEL_OUTPUT_INVALID")
    assert chat.attempts == 3  # first answer + 2 repairs
    assert "after 3 attempt(s)" in outcome.reason and "validation error" in outcome.reason
    assert outcome.raw_verdict is None


async def test_review_timeout_fails_closed(sessionmaker: SM) -> None:
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}], delay_seconds=5)
    outcome = await HeavyReviewer(chat, sessionmaker, config(timeout_seconds=1)).review(await _input(sessionmaker))
    await _assert_fail_closed(sessionmaker, outcome, REVIEW_TIMEOUT)
    assert "did not finish within 1s" in outcome.reason
    assert 900 <= outcome.duration_ms < 4_000


async def test_gateway_timeout_fails_closed(sessionmaker: SM) -> None:
    chat = ScriptedChatModel([ModelTimeout("model call to 'qwen3.8:27b' timed out after 1500s")])
    outcome = await HeavyReviewer(chat, sessionmaker, config()).review(await _input(sessionmaker))
    await _assert_fail_closed(sessionmaker, outcome, REVIEW_TIMEOUT)
    assert "MODEL_TIMEOUT" in outcome.reason


async def test_model_error_fails_closed_with_redacted_reason(sessionmaker: SM) -> None:
    secret = "sk-" + "A" * 40
    chat = ScriptedChatModel([ModelError(f"upstream 502 with api_key={secret}")])
    outcome = await HeavyReviewer(chat, sessionmaker, config()).review(await _input(sessionmaker))
    await _assert_fail_closed(sessionmaker, outcome, "MODEL_ERROR")
    assert secret not in outcome.reason
    run, _ = await load_run(sessionmaker, outcome.review_run_id)
    assert secret not in (run.summary or "")


async def test_unexpected_exception_fails_closed(sessionmaker: SM) -> None:
    chat = ScriptedChatModel([RuntimeError("boom")])
    outcome = await HeavyReviewer(chat, sessionmaker, config()).review(await _input(sessionmaker))
    await _assert_fail_closed(sessionmaker, outcome, REVIEW_INTERNAL_ERROR)
    assert "RuntimeError" in outcome.reason


async def test_empty_diff_for_mutating_step_fails_closed_without_model_call(sessionmaker: SM) -> None:
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}])
    outcome = await HeavyReviewer(chat, sessionmaker, config()).review(await _input(sessionmaker, diff="  \n"))
    await _assert_fail_closed(sessionmaker, outcome, EMPTY_DIFF)
    assert chat.calls == []
    assert "produced no changes" in outcome.reason
    req = build_correction_request(step_id=outcome.review_run_id, attempt_id=None, review=outcome)
    assert req.required_changes[0].startswith("The previous attempt produced no changes")


async def test_empty_diff_with_command_evidence_is_reviewed(sessionmaker: SM) -> None:
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}])
    inp = await _input(sessionmaker, kind="database", diff="", commands=("[exit 0] ssh: psql -c 'CREATE INDEX ix ON t(c)'",))
    outcome = await HeavyReviewer(chat, sessionmaker, config()).review(inp)
    assert outcome.status == "completed" and outcome.passed
    assert "EXECUTED COMMANDS" in chat.calls[0].messages[1].content


async def test_empty_diff_for_non_mutating_kind_is_reviewed(sessionmaker: SM) -> None:
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}])
    outcome = await HeavyReviewer(chat, sessionmaker, config()).review(await _input(sessionmaker, kind="test", diff=""))
    assert outcome.status == "completed" and len(chat.calls) == 1


async def test_missing_heavy_profile_fails_closed(sessionmaker: SM) -> None:
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}])
    outcome = await HeavyReviewer(chat, sessionmaker, config(heavy_enabled=False)).review(await _input(sessionmaker))
    await _assert_fail_closed(sessionmaker, outcome, REVIEW_PROFILE_MISSING)
    assert chat.calls == []


async def test_cancellation_marks_run_and_reraises(sessionmaker: SM) -> None:
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}], delay_seconds=30)
    inp = await _input(sessionmaker)
    task = asyncio.create_task(HeavyReviewer(chat, sessionmaker, config()).review(inp))
    for _ in range(200):
        if chat.calls:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = await events_for(sessionmaker, inp.step_id)
    assert [e.event_type for e in events] == [EventType.REVIEW_STARTED, EventType.REVIEW_FINISHED]
    run_id = events[0].payload["review_run_id"]
    import uuid

    run, _ = await load_run(sessionmaker, uuid.UUID(run_id))
    assert run.status == "error" and run.verdict == "fix_required"
    assert events[-1].payload["error_code"] == REVIEW_CANCELLED


async def test_explicit_fail_closed_run(sessionmaker: SM) -> None:
    job_id, step_id = await make_job_step(sessionmaker)
    reviewer = HeavyReviewer(ScriptedChatModel(), sessionmaker, config())
    outcome = await reviewer.fail_closed(
        job_id=job_id,
        step_id=step_id,
        attempt_id=None,
        step_kind="implement",
        error_code=REVIEW_CONTEXT_ERROR,
        reason="fail-closed: diff unreadable",
    )
    await _assert_fail_closed(sessionmaker, outcome, REVIEW_CONTEXT_ERROR)
