"""Gateway persistence (model_invocations + events), profile DB sync (8.2) and invocation metrics (8.11) against
real PostgreSQL and a real local HTTP server speaking the LiteLLM API."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.core.errors import ModelError, ModelOutputInvalid, ModelTimeout
from hermclaw.models.gateway import GatewayOptions, LiteLLMGateway
from hermclaw.models.health import error_breakdown, invocation_metrics
from hermclaw.models.profiles import get_profile_row, get_profile_row_by_role, list_profile_rows, profile_from_row, sync_profiles
from hermclaw.models.protocols import CallContext, ChatMessage
from hermclaw.persistence.models import Event, ModelInvocation, ModelProfile
from tests.integration.test_models_support import MASTER_KEY, FakeLiteLLM, Scripted, models_config, serve

pytestmark = pytest.mark.integration


class Verdict(BaseModel):
    passed: bool
    findings: list[str]


@pytest.fixture
async def fake() -> AsyncIterator[tuple[FakeLiteLLM, str]]:
    server = FakeLiteLLM()
    async with serve(server.app) as url:
        yield server, url


def ctx(purpose: str = "review") -> CallContext:
    return CallContext(purpose=purpose, job_id=uuid.uuid4(), step_id=uuid.uuid4(), attempt_id=uuid.uuid4())


async def rows_for(sm: Any, job_id: uuid.UUID) -> list[ModelInvocation]:
    async with sm() as s:
        stmt = select(ModelInvocation).where(ModelInvocation.job_id == job_id).order_by(ModelInvocation.started_at, ModelInvocation.repair_attempt)
        return list((await s.execute(stmt)).scalars())


async def events_for(sm: Any, job_id: uuid.UUID) -> list[Event]:
    async with sm() as s:
        return list((await s.execute(select(Event).where(Event.job_id == job_id).order_by(Event.sequence))).scalars())


# ----------------------------------------------------------------------------------------------- invocations
async def test_chat_persists_invocation_and_events(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script("heavy-review", Scripted(body=FakeLiteLLM.completion('{"passed": true, "findings": []}', reasoning="REASONING-SECRET", prompt=99, completion=12)))
    c = ctx()
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker) as gw:
        res = await gw.chat("heavy-review", [ChatMessage("user", "review this diff")], ctx=c)
    rows = await rows_for(sessionmaker, c.job_id)  # type: ignore[arg-type]
    assert len(rows) == 1
    row = rows[0]
    assert row.id == res.invocation_id
    assert (row.alias, row.model, row.role, row.purpose) == ("heavy-review", "qwen3.8:27b", "heavy", "review")
    assert (row.step_id, row.attempt_id) == (c.step_id, c.attempt_id)
    assert row.status == "succeeded" and row.response_valid is True and row.repair_attempt == 0 and not row.fallback_used
    assert (row.prompt_tokens, row.completion_tokens, row.finish_reason) == (99, 12, "stop")
    assert row.reasoning_chars == len("REASONING-SECRET")
    assert row.latency_ms is not None and row.latency_ms >= 0 and row.finished_at is not None
    assert row.request_hash is not None and re.fullmatch(r"[0-9a-f]{64}", row.request_hash)
    assert row.response_excerpt == '{"passed": true, "findings": []}'
    events = await events_for(sessionmaker, c.job_id)  # type: ignore[arg-type]
    assert [e.event_type for e in events] == [EventType.MODEL_INVOCATION_STARTED, EventType.MODEL_INVOCATION_FINISHED]
    assert all(e.payload["invocation_id"] == str(row.id) for e in events)
    assert all(e.step_id == c.step_id and e.source_type == "model_gateway" for e in events)
    fin = events[1].payload
    assert fin["status"] == "succeeded" and fin["usage"] == {"prompt": 99, "completion": 12}
    assert fin["reasoning_chars"] == len("REASONING-SECRET")
    assert fin["estimated_prompt"] > 0 and fin["num_ctx"] == 24576 and fin["max_output"] == 4096
    assert "REDACTED" not in str(fin)
    dumped = str([e.payload for e in events])
    assert "REASONING-SECRET" not in dumped and "review this diff" not in dumped and "findings" not in dumped  # no prompt/content in events
    async with sessionmaker() as s:
        assert "REASONING-SECRET" not in str((await s.get(ModelInvocation, row.id)).__dict__)


async def test_structured_repairs_are_traceable(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script(
        "heavy-review",
        Scripted(body=FakeLiteLLM.completion(None, reasoning="long thoughts")),
        Scripted(body=FakeLiteLLM.completion('{"passed": "nope"}')),
        Scripted(body=FakeLiteLLM.completion('{"passed": false, "findings": ["major: x"]}')),
    )
    c = ctx()
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker) as gw:
        out = await gw.structured("heavy-review", [ChatMessage("user", "review")], Verdict, ctx=c)
    assert out.repair_attempts == 2 and out.value.findings == ["major: x"]
    rows = await rows_for(sessionmaker, c.job_id)  # type: ignore[arg-type]
    assert [(r.repair_attempt, r.status, r.response_valid) for r in rows] == [(0, "invalid", False), (1, "invalid", False), (2, "succeeded", True)]
    assert rows[0].error_code == "MODEL_OUTPUT_INVALID" and "no final content" in (rows[0].error_message or "")
    assert rows[0].response_excerpt == "" and rows[0].reasoning_chars == len("long thoughts")
    assert "passed" in (rows[1].error_message or "")
    fin = [e for e in await events_for(sessionmaker, c.job_id) if e.event_type == EventType.MODEL_INVOCATION_FINISHED]  # type: ignore[arg-type]
    assert [e.severity for e in fin] == ["warning", "warning", "info"]


async def test_exhausted_repairs_raise_and_persist(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script("heavy-review", *[Scripted(body=FakeLiteLLM.completion("not json")) for _ in range(2)])
    c = ctx()
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker) as gw:
        with pytest.raises(ModelOutputInvalid) as exc:
            await gw.structured("heavy-review", [ChatMessage("user", "r")], Verdict, ctx=c, max_repairs=1)
    rows = await rows_for(sessionmaker, c.job_id)  # type: ignore[arg-type]
    assert [r.status for r in rows] == ["invalid", "invalid"]
    assert exc.value.details["invocation_ids"] == [str(r.id) for r in rows]


async def test_failures_and_timeouts_are_persisted(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script("heavy-review", Scripted(status=500, body={"error": {"message": "failed to load model: out of memory"}}))
    server.script("coder-main", Scripted(delay=3.0, body=FakeLiteLLM.completion("late")), Scripted(delay=3.0, body=FakeLiteLLM.completion("late")))
    c = ctx()
    opts = GatewayOptions(timeout_grace_seconds=0.0)
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker, options=opts) as gw:
        with pytest.raises(ModelError):
            await gw.chat("heavy-review", [ChatMessage("user", "r")], ctx=c)
        with pytest.raises(ModelTimeout):
            await gw.chat("coder-main", [ChatMessage("user", "r")], ctx=c, timeout_seconds=1.0)
    rows = await rows_for(sessionmaker, c.job_id)  # type: ignore[arg-type]
    assert [(r.alias, r.status, r.error_code) for r in rows] == [
        ("heavy-review", "failed", "MODEL_LOAD_FAILED"),
        ("coder-main", "timeout", "MODEL_TIMEOUT"),
        ("coder-main", "timeout", "MODEL_TIMEOUT"),  # "repeated timeout": one retry on the same alias
    ]
    assert all(r.finished_at is not None and r.response_valid is None for r in rows)


async def test_excerpt_is_truncated_and_redacted(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    secret = "glpat-" + "A" * 24
    server.script("coder-main", Scripted(body=FakeLiteLLM.completion(f"token: {secret} " + "y" * 9000)))
    c = ctx("coder_turn")
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker) as gw:
        res = await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=c)
    assert secret in res.content  # the caller gets the real answer …
    row = (await rows_for(sessionmaker, c.job_id))[0]  # type: ignore[arg-type]
    assert row.response_excerpt is not None and len(row.response_excerpt) == 4000  # … the DB only a redacted excerpt
    assert secret not in row.response_excerpt and "REDACTED" in row.response_excerpt


async def test_cancellation_is_recorded(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script("coder-main", Scripted(delay=5.0, body=FakeLiteLLM.completion("late")))
    c = ctx("coder_turn")
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker) as gw:
        task = asyncio.create_task(gw.chat("coder-main", [ChatMessage("user", "x")], ctx=c))
        for _ in range(100):
            if server.requests:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    for _ in range(50):
        rows = await rows_for(sessionmaker, c.job_id)  # type: ignore[arg-type]
        if rows and rows[0].status != "started":
            break
        await asyncio.sleep(0.05)
    assert [(r.status, r.error_code) for r in rows] == [("cancelled", "CANCELLED")]


async def test_embedding_invocation(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script("embedding", Scripted(body={"data": [{"index": 0, "embedding": [0.5] * 8}], "usage": {"prompt_tokens": 4}}))
    c = ctx("embedding")
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker) as gw:
        vec = await gw.embed(["hello"], ctx=c)
    assert vec == [[0.5] * 8]
    row = (await rows_for(sessionmaker, c.job_id))[0]  # type: ignore[arg-type]
    assert (row.alias, row.role, row.status, row.prompt_tokens, row.finish_reason) == ("embedding", "embedding", "succeeded", 4, "embedded")
    assert row.response_excerpt == "1 vectors x 8 dims"


# ----------------------------------------------------------------------------------------------- metrics (8.11)
async def test_invocation_metrics(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script("fast-router", *[Scripted(body=FakeLiteLLM.completion("ok", prompt=10, completion=2)) for _ in range(3)])
    server.script("fast-router", Scripted(status=500, body={"error": {"message": "failed to load"}}))
    server.script("planner-gemma", Scripted(status=500, body={"error": {"message": "requires more system memory"}}))
    server.script("planner-gemma-fallback", Scripted(body=FakeLiteLLM.completion("ok", prompt=20, completion=5)))
    c = ctx("triage")
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker) as gw:
        for _ in range(3):
            await gw.chat("fast-router", [ChatMessage("user", "x")], ctx=c)
        with pytest.raises(ModelError):
            await gw.chat("fast-router", [ChatMessage("user", "x")], ctx=c)
        res = await gw.chat("planner-gemma", [ChatMessage("user", "x")], ctx=c)
    assert res.fallback_used
    async with sessionmaker() as s:
        metrics = {m.alias: m for m in await invocation_metrics(s, job_id=c.job_id)}
        assert set(metrics) == {"fast-router", "planner-gemma", "planner-gemma-fallback"}
        fr = metrics["fast-router"]
        assert (fr.calls, fr.succeeded, fr.failed, fr.in_flight) == (4, 3, 1, 0)
        assert fr.prompt_tokens == 30 and fr.completion_tokens == 6
        assert fr.error_rate == pytest.approx(0.25) and fr.invalid_rate == 0
        assert fr.p50_latency_ms is not None and fr.p95_latency_ms is not None and fr.p95_latency_ms >= fr.p50_latency_ms
        assert fr.max_latency_ms is not None and fr.avg_latency_ms is not None
        assert metrics["planner-gemma-fallback"].fallback_calls == 1
        assert metrics["fast-router"].as_dict()["error_rate"] == 0.25
        only = await invocation_metrics(s, job_id=c.job_id, alias="planner-gemma", since=datetime.now(UTC) - timedelta(hours=1))
        assert [m.alias for m in only] == ["planner-gemma"] and only[0].failed == 1
        assert await invocation_metrics(s, job_id=c.job_id, until=datetime.now(UTC) - timedelta(hours=1)) == []
        assert await invocation_metrics(s, job_id=c.job_id, purpose="other") == []
        breakdown = await error_breakdown(s, alias="planner-gemma")
        assert breakdown["planner-gemma"]["MODEL_LOAD_FAILED"] >= 1
        fb_events = [e for e in await events_for(sessionmaker, c.job_id) if e.event_type == EventType.PLANNER_FALLBACK_USED]  # type: ignore[arg-type]
        assert len(fb_events) == 1 and fb_events[0].payload["error_code"] == "MODEL_LOAD_FAILED"


# ----------------------------------------------------------------------------------------------- profile sync (8.2)
async def test_sync_profiles_roundtrip(sessionmaker: Any) -> None:
    cfg = models_config()
    async with sessionmaker() as s:
        # start from a clean table (session-wide DB shared with other tests)
        for row in await list_profile_rows(s):
            await s.delete(row)
        await s.flush()
        report = await sync_profiles(s, cfg)
        assert sorted(report.created) == sorted(p.alias for p in cfg.profiles) and not report.updated and not report.disabled
        await s.commit()
    async with sessionmaker() as s:
        again = await sync_profiles(s, cfg)
        assert again.as_dict() == {"created": [], "updated": [], "disabled": []}
        changed = cfg.model_copy(update={"profiles": [p.model_copy(update={"context_tokens": 20480}) if p.alias == "heavy-review" else p
                                                      for p in cfg.profiles if p.alias != "fast-router"]})
        report = await sync_profiles(s, changed)
        assert report.updated == ["heavy-review"] and report.disabled == ["fast-router"]
        await s.commit()
    async with sessionmaker() as s:
        heavy = await get_profile_row(s, "heavy-review")
        assert heavy.context_tokens == 20480 and heavy.host_worker_id == "model-224"
        assert heavy.metadata_["think"] is False and heavy.metadata_["timeout_seconds"] == 60
        assert (await get_profile_row(s, "fast-router")).enabled is False
        assert (await get_profile_row_by_role(s, "planner")).alias == "planner-gemma"
        assert [r.alias for r in await list_profile_rows(s, enabled_only=True)] == sorted(p.alias for p in changed.profiles)
        rebuilt = profile_from_row(await get_profile_row(s, "embedding"))
        assert rebuilt == cfg.by_alias("embedding")
        from hermclaw.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await get_profile_row(s, "ghost")
        with pytest.raises(NotFoundError):
            await get_profile_row_by_role(s, "fast")
        # re-enable on next sync
        report = await sync_profiles(s, cfg)
        assert "fast-router" in report.updated
        await s.commit()
    async with sessionmaker() as s:
        assert (await s.get(ModelProfile, "fast-router")).enabled is True  # type: ignore[union-attr]
