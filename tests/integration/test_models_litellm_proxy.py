"""EMPIRICAL contract test: Hermclaw gateway → real LiteLLM proxy → (fake) Ollama.

Asserts exactly which request fields reach Ollama (``format``, ``think``, ``options.num_ctx``, ``keep_alive`` …),
that reasoning is discarded, how LiteLLM errors map onto gateway error codes, and that the generated proxy config
disables LiteLLM-side retries. Needs a ``litellm`` CLI with the proxy extras (``HERMCLAW_TEST_LITELLM_BIN`` or the
repo venv) – otherwise skipped with a reason (see docs/architecture/models.md).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.core.errors import ModelError, ModelTimeout
from hermclaw.models.gateway import MODEL_AUTH_FAILED, MODEL_BAD_REQUEST, MODEL_NOT_FOUND, GatewayOptions, LiteLLMGateway
from hermclaw.models.health import ModelHealthChecker
from hermclaw.models.litellm_config import build_litellm_config, write_litellm_config
from hermclaw.models.protocols import CallContext, ChatMessage
from hermclaw.persistence.models import Event, ModelInvocation
from tests.integration.test_models_support import MASTER_KEY, FakeOllama, litellm_proxy, litellm_proxy_binary, models_config, serve

pytestmark = pytest.mark.integration

INSTALLED = ["qwen3:8b", "gemma4:26b", "gemma4:12b", "qwen3-coder:30b", "qwen3.8:27b", "embeddinggemma-2:740m"]


class Answer(BaseModel):
    ok: bool
    answer: str


@pytest.fixture(scope="module")
def proxy_bin() -> str:
    binary = litellm_proxy_binary()
    if binary is None:
        pytest.skip("no litellm CLI with proxy extras (set HERMCLAW_TEST_LITELLM_BIN to a venv with 'litellm[proxy]')")
    return binary


@pytest.fixture(scope="module")
async def stack(proxy_bin: str, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[dict[str, Any]]:
    fake = FakeOllama(INSTALLED)
    async with serve(fake.app) as ollama_url:
        cfg = models_config()
        conf = build_litellm_config(cfg, ollama_urls_by_host={"model-224": ollama_url}, keep_alive="10m")
        path = write_litellm_config(tmp_path_factory.mktemp("litellm") / "config.yaml", conf)
        async with litellm_proxy(proxy_bin, path) as proxy_url:
            yield {"fake": fake, "ollama_url": ollama_url, "proxy_url": proxy_url, "config": conf, "path": path}


def gateway(stack: dict[str, Any], **kw: Any) -> LiteLLMGateway:
    cfg = kw.pop("models", None) or models_config(stack["proxy_url"])
    return LiteLLMGateway(cfg, api_key=kw.pop("api_key", MASTER_KEY), **kw)


@pytest.fixture(autouse=True)
def _reset(request: pytest.FixtureRequest) -> None:
    if "stack" in request.fixturenames:
        fake: FakeOllama = request.getfixturevalue("stack")["fake"]
        fake.requests.clear()
        fake.behaviour.clear()
        fake.installed = list(INSTALLED)


CTX = CallContext(purpose="planner")


async def test_chat_request_fields_reach_ollama(stack: dict[str, Any]) -> None:
    fake: FakeOllama = stack["fake"]
    schema = Answer.model_json_schema()
    async with gateway(stack) as gw:
        res = await gw.chat(
            "fast-router",
            [ChatMessage("system", "be brief"), ChatMessage("user", "hi")],
            ctx=CTX,
            max_tokens=123,
            temperature=0.3,
            json_schema=schema,
        )
    body = fake.bodies("/api/chat")[-1]
    assert body["model"] == "qwen3:8b"
    assert body["format"] == schema  # response_format.json_schema.schema -> Ollama `format`
    assert body["think"] is False  # top-level `think` passes through
    assert body["options"]["num_ctx"] == 16384  # top-level `num_ctx` -> options.num_ctx
    assert body["options"]["num_predict"] == 123
    assert body["options"]["temperature"] == 0.3
    assert body["keep_alive"] == "10m"
    assert body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "timeout" not in body and "timeout" not in body["options"]  # LiteLLM-side only
    # only the final content comes back; the reasoning is counted and dropped
    assert res.content == fake.default_content
    assert res.reasoning_chars == len(fake.b("qwen3:8b").thinking)
    assert "PRIVATE-CHAIN" not in repr(res)
    assert res.prompt_tokens == 42 and res.completion_tokens == 7 and res.finish_reason == "stop"
    assert res.alias == "fast-router" and res.model == "qwen3:8b"


async def test_request_level_think_overrides_proxy_default(stack: dict[str, Any]) -> None:
    fake: FakeOllama = stack["fake"]
    cfg = models_config(stack["proxy_url"])
    heavy = cfg.by_alias("heavy-review")
    cfg = cfg.model_copy(update={"profiles": [p.model_copy(update={"think": True}) if p is heavy else p for p in cfg.profiles]})
    async with gateway(stack, models=cfg) as gw:
        await gw.chat("heavy-review", [ChatMessage("user", "review")], ctx=CTX)
    body = fake.bodies("/api/chat")[-1]
    assert body["model"] == "qwen3.8:27b"
    assert body["think"] is True  # proxy config says false, the request wins
    assert body["options"]["num_ctx"] == 24576


async def test_request_level_num_ctx_and_keep_alive_override_proxy_defaults(stack: dict[str, Any]) -> None:
    """The proxy config says num_ctx=32768 / keep_alive=10m for coder-main; the per-request values must win."""
    fake: FakeOllama = stack["fake"]
    cfg = models_config(stack["proxy_url"])
    coder = cfg.by_alias("coder-main")
    cfg = cfg.model_copy(update={"profiles": [p.model_copy(update={"context_tokens": 30000}) if p is coder else p for p in cfg.profiles]})
    async with gateway(stack, models=cfg, options=GatewayOptions(keep_alive="45m")) as gw:
        await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CTX, max_tokens=64)
    body = fake.bodies("/api/chat")[-1]
    assert body["model"] == "qwen3-coder:30b"
    assert body["options"]["num_ctx"] == 30000
    assert body["keep_alive"] == "45m"


async def test_proxy_config_defaults_apply_without_passthrough(stack: dict[str, Any]) -> None:
    """Defence in depth: even a client that sends nothing Ollama-specific gets num_ctx/keep_alive/think from the
    generated proxy config."""
    fake: FakeOllama = stack["fake"]
    async with gateway(stack, options=GatewayOptions(ollama_passthrough=False)) as gw:
        await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CTX, max_tokens=50)
    body = fake.bodies("/api/chat")[-1]
    assert body["options"]["num_ctx"] == 32768
    assert body["keep_alive"] == "10m"
    assert body["think"] is False


async def test_structured_repair_through_proxy_never_resends_reasoning(stack: dict[str, Any]) -> None:
    fake: FakeOllama = stack["fake"]
    beh = fake.b("gemma4:26b")
    beh.contents.extend(['```json\n{"ok": "maybe"}\n```', '{"ok": true, "answer": "fixed"}'])
    async with gateway(stack) as gw:
        out = await gw.structured("planner-gemma", [ChatMessage("user", "plan")], Answer, ctx=CTX)
    assert out.value == Answer(ok=True, answer="fixed")
    assert out.repair_attempts == 1
    chats = fake.bodies("/api/chat")
    assert len(chats) == 2
    repair = chats[1]["messages"]
    assert repair[-1]["role"] == "user" and "ok" in repair[-1]["content"] and "answer" in repair[-1]["content"]
    assert all("PRIVATE-CHAIN" not in m.get("content", "") and "thinking" not in m for m in repair)
    assert chats[1]["format"] == Answer.model_json_schema()


async def test_embeddings_through_proxy(stack: dict[str, Any]) -> None:
    fake: FakeOllama = stack["fake"]
    async with gateway(stack) as gw:
        vectors = await gw.embed(["alpha", "beta", "gamma"], ctx=CallContext(purpose="embedding"))
    assert len(vectors) == 3 and all(len(v) == 8 for v in vectors)
    assert vectors[0] != vectors[1]
    body = fake.bodies("/api/embed")[-1]
    assert body["model"] == "embeddinggemma-2:740m"
    assert body["input"] == ["alpha", "beta", "gamma"]
    assert body["options"]["num_ctx"] == 2048
    assert body["keep_alive"] == "10m"


async def test_load_error_falls_back_once_without_litellm_retries(stack: dict[str, Any], sessionmaker: Any) -> None:
    fake: FakeOllama = stack["fake"]
    beh = fake.b("gemma4:26b")
    beh.status, beh.error = 500, "model requires more system memory (25.1 GiB) than is available (12.0 GiB)"
    job_ctx = CallContext(purpose="planner", job_id=uuid.uuid4())
    async with gateway(stack, session_factory=sessionmaker) as gw:
        res = await gw.chat("planner-gemma", [ChatMessage("user", "plan")], ctx=job_ctx)
    assert res.alias == "planner-gemma-fallback" and res.fallback_used and res.model == "gemma4:12b"
    models_hit = [b["model"] for b in fake.bodies("/api/chat")]
    assert models_hit == ["gemma4:26b", "gemma4:12b"]  # num_retries: 0 – LiteLLM did not retry or switch itself
    async with sessionmaker() as s:
        rows = (
            (await s.execute(select(ModelInvocation).where(ModelInvocation.job_id == job_ctx.job_id).order_by(ModelInvocation.started_at)))
            .scalars()
            .all()
        )
        assert [(r.alias, r.status, r.fallback_used) for r in rows] == [
            ("planner-gemma", "failed", False),
            ("planner-gemma-fallback", "succeeded", True),
        ]
        assert rows[0].error_code == "MODEL_LOAD_FAILED"
        events = (await s.execute(select(Event).where(Event.job_id == job_ctx.job_id).order_by(Event.sequence))).scalars().all()
        fb = [e for e in events if e.event_type == EventType.PLANNER_FALLBACK_USED]
        assert len(fb) == 1 and fb[0].severity == "warning"
        assert fb[0].payload["primary_alias"] == "planner-gemma" and fb[0].payload["fallback_alias"] == "planner-gemma-fallback"
        assert "system memory" in fb[0].payload["reason"]


async def test_missing_model_is_not_found(stack: dict[str, Any]) -> None:
    fake: FakeOllama = stack["fake"]
    fake.installed.remove("qwen3-coder:30b")
    async with gateway(stack) as gw:
        with pytest.raises(ModelError) as exc:
            await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CTX)
    assert exc.value.code == MODEL_NOT_FOUND


async def test_proxy_timeout_maps_to_model_timeout(stack: dict[str, Any]) -> None:
    fake: FakeOllama = stack["fake"]
    fake.b("qwen3-coder:30b").delay = 6.0
    async with gateway(stack, options=GatewayOptions(timeout_retries=0, timeout_grace_seconds=3.0)) as gw:
        t0 = time.monotonic()
        with pytest.raises(ModelTimeout) as exc:
            await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CTX, timeout_seconds=1.5)
    elapsed = time.monotonic() - t0
    # the body `timeout` is enforced by the proxy (HTTP 408) well before the client-side limit (1.5 s + 3 s grace)
    assert exc.value.details.get("http_status") == 408, exc.value.details
    assert elapsed < 4.0, elapsed
    assert len(fake.bodies("/api/chat")) == 1


async def test_wrong_key_is_rejected_without_fallback(stack: dict[str, Any]) -> None:
    fake: FakeOllama = stack["fake"]
    async with gateway(stack, api_key="sk-wrong-key-000000000000000000") as gw:
        with pytest.raises(ModelError) as exc:
            await gw.chat("planner-gemma", [ChatMessage("user", "x")], ctx=CTX)
    assert exc.value.code in (MODEL_AUTH_FAILED, MODEL_BAD_REQUEST)
    assert "sk-wrong-key" not in exc.value.message
    assert fake.bodies("/api/chat") == []


async def test_health_against_real_proxy(stack: dict[str, Any]) -> None:
    fake: FakeOllama = stack["fake"]
    fake.installed.remove("qwen3.8:27b")
    checker = ModelHealthChecker(models_config(stack["proxy_url"]), ollama_urls={"model-224": stack["ollama_url"]}, api_key=MASTER_KEY)
    try:
        report = await checker.check()
    finally:
        await checker.aclose()
    assert report.litellm_liveliness.ok and report.litellm_readiness.ok
    assert report.litellm_models is not None and report.litellm_models.ok
    assert all(p.registered_in_proxy for p in report.profiles)
    assert report.profile("fast-router") is not None and report.profile("fast-router").available  # type: ignore[union-attr]
    heavy = report.profile("heavy-review")
    assert heavy is not None and not heavy.available and heavy.installed is False
    assert not report.healthy
    assert fake.bodies("/api/chat") == []  # health never triggers inference/model loads


def test_generated_config_shape(stack: dict[str, Any]) -> None:
    conf = stack["config"]
    names = [m["model_name"] for m in conf["model_list"]]
    assert names == ["fast-router", "planner-gemma", "planner-gemma-fallback", "coder-main", "heavy-review", "embedding"]
    assert conf["router_settings"]["num_retries"] == 0 and conf["litellm_settings"]["num_retries"] == 0
    assert conf["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    text = Path(stack["path"]).read_text(encoding="utf-8")
    assert MASTER_KEY not in text and "sk-" not in text
