"""Failure behaviour of the model gateway / residency: unreachable services, persistence failures, secret leaks,
concurrency."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import LoadedModel
from hermclaw.core.errors import ModelError
from hermclaw.models.gateway import MODEL_UNAVAILABLE, LiteLLMGateway
from hermclaw.models.protocols import CallContext, ChatMessage
from hermclaw.models.residency import ModelResidency, OllamaHostClient
from hermclaw.persistence.models import Event, ModelInvocation
from tests.integration.test_models_support import MASTER_KEY, FakeLiteLLM, FakeOllama, Scripted, models_config, serve


@pytest.fixture
async def fake() -> AsyncIterator[tuple[FakeLiteLLM, str]]:
    server = FakeLiteLLM()
    async with serve(server.app) as url:
        yield server, url


async def test_unreachable_litellm_tries_fallback_then_fails() -> None:
    async with LiteLLMGateway(models_config("http://127.0.0.1:9"), api_key=MASTER_KEY) as gw:
        with pytest.raises(ModelError) as exc:
            await gw.chat("planner-gemma", [ChatMessage("user", "x")], ctx=CallContext(purpose="planner"))
    assert exc.value.code == MODEL_UNAVAILABLE
    assert exc.value.details["fallback_alias"] == "planner-gemma-fallback"
    assert exc.value.details["primary_error_code"] == MODEL_UNAVAILABLE


class FailingSession:
    """Session factory wrapper whose N-th session fails on commit."""

    def __init__(self, sm: Any, fail_on: int) -> None:
        self.sm = sm
        self.fail_on = fail_on
        self.n = 0

    def __call__(self) -> Any:
        self.n += 1
        session = self.sm()
        if self.n == self.fail_on:

            async def boom() -> None:
                raise RuntimeError("db down")

            session.commit = boom  # type: ignore[method-assign]
        return session


async def test_finish_persistence_failure_keeps_result(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script("coder-main", Scripted(body=FakeLiteLLM.completion("expensive answer")))
    job = uuid.uuid4()
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=FailingSession(sessionmaker, 2)) as gw:
        res = await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CallContext(purpose="coder_turn", job_id=job))
    assert res.content == "expensive answer"
    async with sessionmaker() as s:
        row = (await s.execute(select(ModelInvocation).where(ModelInvocation.job_id == job))).scalar_one()
    assert row.status == "started"  # visible as orphan for operators, answer was not lost


async def test_start_persistence_failure_prevents_untracked_call(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=FailingSession(sessionmaker, 1)) as gw:
        with pytest.raises(RuntimeError):
            await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CallContext(purpose="coder_turn"))
    assert server.requests == []


async def test_echoed_secrets_never_persisted(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script("heavy-review", Scripted(status=400, body={"error": {"message": f"Authorization: Bearer {MASTER_KEY} rejected"}}))
    job = uuid.uuid4()
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker) as gw:
        with pytest.raises(ModelError) as exc:
            await gw.chat("heavy-review", [ChatMessage("user", "x")], ctx=CallContext(purpose="review", job_id=job))
    assert MASTER_KEY not in exc.value.message
    async with sessionmaker() as s:
        row = (await s.execute(select(ModelInvocation).where(ModelInvocation.job_id == job))).scalar_one()
        evs = (await s.execute(select(Event).where(Event.job_id == job))).scalars().all()
    assert row.error_message is not None and MASTER_KEY not in row.error_message
    assert all(MASTER_KEY not in str(e.payload) for e in evs)
    assert server.requests[0]["headers"]["Authorization"] == f"Bearer {MASTER_KEY}"


async def test_concurrent_calls_are_all_recorded(fake: tuple[FakeLiteLLM, str], sessionmaker: Any) -> None:
    server, url = fake
    server.script("fast-router", *[Scripted(delay=0.05, body=FakeLiteLLM.completion(f"a{i}")) for i in range(20)])
    job = uuid.uuid4()
    async with LiteLLMGateway(models_config(url), api_key=MASTER_KEY, session_factory=sessionmaker) as gw:
        results = await asyncio.gather(
            *(gw.chat("fast-router", [ChatMessage("user", str(i))], ctx=CallContext(purpose="triage", job_id=job)) for i in range(20))
        )
    assert len({r.invocation_id for r in results}) == 20
    assert sorted(r.content for r in results) == sorted(f"a{i}" for i in range(20))
    async with sessionmaker() as s:
        rows = (await s.execute(select(ModelInvocation).where(ModelInvocation.job_id == job))).scalars().all()
    assert len(rows) == 20 and {r.status for r in rows} == {"succeeded"}


async def test_concurrent_switches_leave_one_large_model() -> None:
    fake = FakeOllama(["gemma4:26b", "qwen3-coder:30b", "qwen3.8:27b", "qwen3:8b"])
    async with serve(fake.app) as url:
        res = ModelResidency(models_config(), OllamaHostClient(url, poll_seconds=0.02))
        results = await asyncio.gather(*(res.ensure_loaded(a) for a in ["planner-gemma", "coder-main", "heavy-review", "fast-router"]))
    large = {"gemma4:26b", "qwen3-coder:30b", "qwen3.8:27b"}
    assert len(large & set(fake.loaded)) == 1  # serialised per host: never two exclusive models resident
    assert "qwen3:8b" in fake.loaded
    assert sum(len(r.unloaded) for r in results) == 2


async def test_ollama_error_bodies_are_redacted() -> None:
    fake = FakeOllama(["gemma4:26b"])
    b = fake.b("gemma4:26b")
    b.status, b.error = 500, "load failed token=supersecretvalue123"
    async with serve(fake.app) as url:
        client = OllamaHostClient(url)
        with pytest.raises(ModelError) as exc:
            await client.load("gemma4:26b", 32768, "10m")
    assert "supersecretvalue123" not in exc.value.message


async def test_failed_conflict_unload_blocks_switch_and_is_evented(sessionmaker: Any) -> None:
    """If an exclusive group member cannot be unloaded, the target is NOT loaded next to it and the failure is evented."""
    fake = FakeOllama(["gemma4:26b", "qwen3-coder:30b"])
    fake.loaded = {"gemma4:26b": 32768}

    class StickyHost(OllamaHostClient):
        async def loaded_models(self) -> list[LoadedModel]:  # Ollama keeps reporting the old model as resident
            return [LoadedModel(name="gemma4:26b", context_length=32768)]

    job = uuid.uuid4()
    async with serve(fake.app) as url:
        res = ModelResidency(models_config(), StickyHost(url, unload_wait_seconds=0.3, poll_seconds=0.05), session_factory=sessionmaker)
        with pytest.raises(ModelError) as exc:
            await res.ensure_loaded("coder-main", job_id=job, lease_id="lease-1")
    assert exc.value.code == "MODEL_UNLOAD_FAILED"
    gen = fake.bodies("/api/generate")
    assert gen and all(b.get("keep_alive") == 0 for b in gen)  # only the unload attempt, never a load
    assert "qwen3-coder:30b" not in fake.loaded
    async with sessionmaker() as s:
        evs = list((await s.execute(select(Event).where(Event.job_id == job).order_by(Event.sequence))).scalars())
    assert [e.event_type for e in evs] == [EventType.MODEL_LOAD_FINISHED]
    payload = evs[0].payload
    assert payload["ok"] is False and payload["phase"] == "unload" and payload["error_code"] == "MODEL_UNLOAD_FAILED"
    assert payload["lease_id"] == "lease-1" and evs[0].severity == "error"
