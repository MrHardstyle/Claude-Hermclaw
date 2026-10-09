"""Unit tests for the LiteLLM gateway (no DB, no network: httpx.MockTransport plays LiteLLM)."""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from hermclaw.core.errors import ConfigError, ModelError, ModelOutputInvalid, ModelTimeout, ValidationFailed
from hermclaw.models import gateway as gw_mod
from hermclaw.models.gateway import (
    EMBEDDING_DIMENSION_MISMATCH,
    MODEL_BAD_REQUEST,
    MODEL_LOAD_FAILED,
    MODEL_NOT_FOUND,
    MODEL_PROTOCOL_ERROR,
    MODEL_UNAVAILABLE,
    GatewayOptions,
    LiteLLMGateway,
    classify_http_error,
    extract_json,
    is_technical_failure,
    response_format_for,
    strip_reasoning,
)
from hermclaw.models.protocols import CallContext, ChatMessage, ChatModel, EmbeddingModel
from tests.integration.test_models_support import MASTER_KEY, models_config

CTX = CallContext(purpose="planner")


def completion(content: Any, *, reasoning: str | None = None, finish: str = "stop") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {
        "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }


class Script:
    """Per-alias queue of responses: dict (JSON 200), (status, body), or an exception instance."""

    def __init__(self) -> None:
        self.queues: dict[str, deque[Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def add(self, alias: str, *items: Any) -> None:
        self.queues.setdefault(alias, deque()).extend(items)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        queue = self.queues.get(body["model"])
        item = queue.popleft() if queue else completion('{"ok": true}')
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            status, payload = item
            if isinstance(payload, str):
                return httpx.Response(status, text=payload)
            return httpx.Response(status, json=payload)
        return httpx.Response(200, json=item)

    def models(self) -> list[str]:
        return [r["model"] for r in self.requests]


@pytest.fixture
def script() -> Script:
    return Script()


def make(script: Script, **kw: Any) -> LiteLLMGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(script.handler))
    return LiteLLMGateway(models_config(), api_key=MASTER_KEY, http_client=client, **kw)


class Plan(BaseModel):
    title: str
    steps: list[str] = Field(min_length=1)


# ----------------------------------------------------------------------------------------------- helpers
def test_protocol_conformance(script: Script) -> None:
    gw = make(script)
    assert isinstance(gw, ChatModel) and isinstance(gw, EmbeddingModel)
    assert gw.dimensions == 8 and gw.model_name == "embeddinggemma-2:740m"


@pytest.mark.parametrize(
    ("raw", "expected", "removed"),
    [
        ('<think>secret</think>\n{"a":1}', '{"a":1}', len("<think>secret</think>")),
        ("<THINKING>x</THINKING>answer", "answer", len("<THINKING>x</THINKING>")),
        ("<think>never closed", "", len("<think>never closed")),
        ("plain answer", "plain answer", 0),
        ("text <think>a</think> more", "text  more", len("<think>a</think>")),
    ],
)
def test_strip_reasoning(raw: str, expected: str, removed: int) -> None:
    out, n = strip_reasoning(raw)
    assert out == expected and n == removed


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ("```\n[1, 2]\n```", [1, 2]),
        ('Here you go: {"a": {"b": 2}} trailing', {"a": {"b": 2}}),
        ('﻿ {"a": 1}', {"a": 1}),
    ],
)
def test_extract_json(text: str, value: Any) -> None:
    assert extract_json(text) == value


@pytest.mark.parametrize("text", ["", "   ", "no json here", "{broken"])
def test_extract_json_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        extract_json(text)


def test_response_format_wrapping() -> None:
    schema = Plan.model_json_schema()
    rf = response_format_for(schema)
    assert rf == {"type": "json_schema", "json_schema": {"name": "Plan", "schema": schema, "strict": True}}
    wrapped = response_format_for({"name": "my plan!", "schema": {"type": "object"}, "strict": False})
    assert wrapped["json_schema"] == {"name": "my_plan_", "schema": {"type": "object"}, "strict": False}


@pytest.mark.parametrize(
    ("status", "message", "code"),
    [
        (408, "", "MODEL_TIMEOUT"),
        (504, "", "MODEL_TIMEOUT"),
        (401, "", "MODEL_AUTH_FAILED"),
        (429, "", "MODEL_RATE_LIMITED"),
        (404, "model 'x' not found", MODEL_NOT_FOUND),
        (500, "model requires more system memory", MODEL_LOAD_FAILED),
        (400, "llama runner process has terminated", MODEL_LOAD_FAILED),
        (500, "litellm.APIConnectionError: connection refused", MODEL_UNAVAILABLE),
        (503, "busy", MODEL_UNAVAILABLE),
        (400, "Invalid model name passed", MODEL_BAD_REQUEST),
    ],
)
def test_classify_http_error(status: int, message: str, code: str) -> None:
    assert classify_http_error(status, message) == code


def test_technical_failure_codes() -> None:
    assert is_technical_failure(ModelTimeout("t"))
    assert is_technical_failure(ModelError("x", code=MODEL_LOAD_FAILED))
    assert not is_technical_failure(ModelOutputInvalid("x"))
    assert not is_technical_failure(ModelError("x", code=MODEL_BAD_REQUEST))
    assert not is_technical_failure(ValueError("x"))


# ----------------------------------------------------------------------------------------------- chat
async def test_chat_body_and_result(script: Script) -> None:
    script.add("coder-main", completion("<think>inline</think>final", reasoning="hidden reasoning"))
    async with make(script) as gw:
        res = await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CTX, temperature=0.5, json_schema={"type": "object"})
    body = script.requests[0]
    assert body["model"] == "coder-main"
    assert body["max_tokens"] == 6144  # profile default
    assert body["temperature"] == 0.5
    assert body["think"] is False and body["num_ctx"] == 32768 and body["keep_alive"] == "10m"
    assert body["stream"] is False and body["timeout"] == 60
    assert body["response_format"]["type"] == "json_schema"
    assert script.headers[0]["authorization"] == f"Bearer {MASTER_KEY}"
    assert res.content == "final"
    assert res.reasoning_chars == len("hidden reasoning") + len("<think>inline</think>")
    assert "hidden" not in repr(res) and "inline" not in repr(res)
    assert res.prompt_tokens == 11 and res.completion_tokens == 3 and res.raw_usage["total_tokens"] == 14
    assert res.invocation_id is not None and not res.fallback_used


async def test_chat_uses_profile_temperature_and_content_parts(script: Script) -> None:
    script.add("fast-router", completion([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]))
    async with make(script) as gw:
        res = await gw.chat("fast-router", [ChatMessage("user", "x")], ctx=CTX)
    assert res.content == "ab"
    assert script.requests[0]["temperature"] == 0.1


async def test_passthrough_can_be_disabled(script: Script) -> None:
    async with make(script, options=GatewayOptions(ollama_passthrough=False)) as gw:
        await gw.chat("fast-router", [ChatMessage("user", "x")], ctx=CTX)
    assert not {"think", "num_ctx", "keep_alive"} & set(script.requests[0])


async def test_context_overflow_rejected_before_http(script: Script) -> None:
    async with make(script) as gw:
        with pytest.raises(ValidationFailed) as exc:
            await gw.chat("fast-router", [ChatMessage("user", "x" * 60_000)], ctx=CTX)
    assert exc.value.code == "CONTEXT_OVERFLOW"
    assert exc.value.details["context_tokens"] == 16384
    assert script.requests == []


async def test_unknown_and_wrong_kind_alias(script: Script) -> None:
    async with make(script) as gw:
        with pytest.raises(ConfigError):
            await gw.chat("nope", [ChatMessage("user", "x")], ctx=CTX)
        with pytest.raises(ConfigError) as exc:
            await gw.chat("embedding", [ChatMessage("user", "x")], ctx=CTX)
    assert exc.value.code == "MODEL_KIND_MISMATCH"


async def test_http_errors_map_to_codes_and_redact(script: Script) -> None:
    script.add("coder-main", (400, {"error": {"message": f"bad request api_key={MASTER_KEY}"}}))
    async with make(script) as gw:
        with pytest.raises(ModelError) as exc:
            await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CTX)
    assert exc.value.code == MODEL_BAD_REQUEST and exc.value.details["http_status"] == 400
    assert MASTER_KEY not in exc.value.message


async def test_protocol_errors(script: Script) -> None:
    script.add("coder-main", (200, "not json"), {"choices": []})
    async with make(script) as gw:
        for _ in range(2):
            with pytest.raises(ModelError) as exc:
                await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CTX)
            assert exc.value.code == MODEL_PROTOCOL_ERROR


async def test_transport_errors(script: Script) -> None:
    script.add("coder-main", httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    async with make(script) as gw:
        with pytest.raises(ModelError) as exc:
            await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CTX)
        assert exc.value.code == MODEL_UNAVAILABLE
        with pytest.raises(ModelTimeout):
            await gw.chat("coder-main", [ChatMessage("user", "x")], ctx=CTX)
    assert script.models() == ["coder-main"] * 3  # timeout retried once (timeout_retries=1), coder has no fallback


async def test_empty_content_returned_but_marked(script: Script) -> None:
    script.add("fast-router", completion(None, reasoning="only thinking"))
    async with make(script) as gw:
        res = await gw.chat("fast-router", [ChatMessage("user", "x")], ctx=CTX)
    assert res.content == "" and res.reasoning_chars == len("only thinking")


# ----------------------------------------------------------------------------------------------- fallback
async def test_fallback_on_load_error(script: Script) -> None:
    script.add("planner-gemma", (500, {"error": {"message": "model requires more system memory"}}))
    script.add("planner-gemma-fallback", completion("from 12b"))
    async with make(script) as gw:
        res = await gw.chat("planner-gemma", [ChatMessage("user", "x")], ctx=CTX)
    assert res.content == "from 12b" and res.alias == "planner-gemma-fallback" and res.model == "gemma4:12b" and res.fallback_used
    assert script.models() == ["planner-gemma", "planner-gemma-fallback"]
    assert script.requests[1]["num_ctx"] == 32768


async def test_fallback_after_repeated_timeout_only(script: Script) -> None:
    script.add("planner-gemma", httpx.ReadTimeout("t"), httpx.ReadTimeout("t"))
    async with make(script) as gw:
        res = await gw.chat("planner-gemma", [ChatMessage("user", "x")], ctx=CTX)
    assert script.models() == ["planner-gemma", "planner-gemma", "planner-gemma-fallback"]
    assert res.fallback_used


async def test_single_timeout_recovers_on_primary(script: Script) -> None:
    script.add("planner-gemma", httpx.ReadTimeout("t"), completion("primary ok"))
    async with make(script) as gw:
        res = await gw.chat("planner-gemma", [ChatMessage("user", "x")], ctx=CTX)
    assert res.alias == "planner-gemma" and not res.fallback_used and res.content == "primary ok"


@pytest.mark.parametrize("item", [(400, {"error": {"message": "bad"}}), (401, {"error": {"message": "no"}})])
async def test_no_fallback_for_non_technical_errors(script: Script, item: Any) -> None:
    script.add("planner-gemma", item)
    async with make(script) as gw:
        with pytest.raises(ModelError):
            await gw.chat("planner-gemma", [ChatMessage("user", "x")], ctx=CTX)
    assert script.models() == ["planner-gemma"]


async def test_fallback_failure_carries_both_errors(script: Script) -> None:
    script.add("planner-gemma", (500, {"error": {"message": "failed to load model"}}))
    script.add("planner-gemma-fallback", (500, {"error": {"message": "failed to load model"}}))
    async with make(script) as gw:
        with pytest.raises(ModelError) as exc:
            await gw.chat("planner-gemma", [ChatMessage("user", "x")], ctx=CTX)
    assert exc.value.details["primary_alias"] == "planner-gemma"
    assert exc.value.details["fallback_alias"] == "planner-gemma-fallback"
    assert exc.value.details["primary_error_code"] == MODEL_LOAD_FAILED


async def test_no_fallback_configured_raises_primary_error(script: Script) -> None:
    script.add("heavy-review", (500, {"error": {"message": "failed to load"}}))
    async with make(script) as gw:
        with pytest.raises(ModelError) as exc:
            await gw.chat("heavy-review", [ChatMessage("user", "x")], ctx=CTX)
    assert exc.value.code == MODEL_LOAD_FAILED and script.models() == ["heavy-review"]


# ----------------------------------------------------------------------------------------------- structured
async def test_structured_first_try(script: Script) -> None:
    script.add("planner-gemma", completion('```json\n{"title": "t", "steps": ["a"]}\n```'))
    async with make(script) as gw:
        out = await gw.structured("planner-gemma", [ChatMessage("user", "plan")], Plan, ctx=CTX)
    assert out.value == Plan(title="t", steps=["a"]) and out.repair_attempts == 0
    assert script.requests[0]["response_format"]["json_schema"]["schema"] == Plan.model_json_schema()


async def test_structured_repairs_with_validation_error_not_reasoning(script: Script) -> None:
    script.add(
        "planner-gemma",
        completion('{"title": "t", "steps": []}', reasoning="SECRET-REASONING"),
        completion(None, reasoning="SECRET-REASONING-2"),
        completion('{"title": "t", "steps": ["one"]}'),
    )
    async with make(script) as gw:
        out = await gw.structured("planner-gemma", [ChatMessage("system", "s"), ChatMessage("user", "plan")], Plan, ctx=CTX)
    assert out.repair_attempts == 2 and out.value.steps == ["one"]
    first_repair = script.requests[1]["messages"]
    assert [m["role"] for m in first_repair] == ["system", "user", "assistant", "user"]
    assert first_repair[2]["content"] == '{"title": "t", "steps": []}'
    assert "steps" in first_repair[3]["content"] and "at least 1" in first_repair[3]["content"]
    second_repair = script.requests[2]["messages"]
    # empty content: no assistant echo, explicit hint, and conversation does not grow unboundedly
    assert [m["role"] for m in second_repair] == ["system", "user", "user"]
    assert "no final content" in second_repair[-1]["content"]
    assert all("SECRET" not in json.dumps(r) for r in script.requests)


async def test_structured_exhausts_budget(script: Script) -> None:
    script.add("planner-gemma", *[completion("nope") for _ in range(3)])
    async with make(script) as gw:
        with pytest.raises(ModelOutputInvalid) as exc:
            await gw.structured("planner-gemma", [ChatMessage("user", "plan")], Plan, ctx=CTX, max_repairs=2)
    assert len(script.requests) == 3
    assert exc.value.details["attempts"] == 3 and len(exc.value.details["errors"]) == 3
    assert len(exc.value.details["invocation_ids"]) == 3
    assert script.models() == ["planner-gemma"] * 3  # invalid output never triggers the model fallback


async def test_structured_zero_repairs(script: Script) -> None:
    script.add("planner-gemma", completion("nope"))
    async with make(script) as gw:
        with pytest.raises(ModelOutputInvalid):
            await gw.structured("planner-gemma", [ChatMessage("user", "plan")], Plan, ctx=CTX, max_repairs=0)
        with pytest.raises(ValueError):
            await gw.structured("planner-gemma", [ChatMessage("user", "plan")], Plan, ctx=CTX, max_repairs=-1)
    assert len(script.requests) == 1


async def test_structured_pins_fallback_for_repairs(script: Script) -> None:
    script.add("planner-gemma", (500, {"error": {"message": "failed to load"}}))
    script.add("planner-gemma-fallback", completion("invalid"), completion('{"title": "x", "steps": ["s"]}'))
    async with make(script) as gw:
        out = await gw.structured("planner-gemma", [ChatMessage("user", "plan")], Plan, ctx=CTX)
    assert script.models() == ["planner-gemma", "planner-gemma-fallback", "planner-gemma-fallback"]
    assert out.result.fallback_used and out.result.alias == "planner-gemma-fallback" and out.repair_attempts == 1


# ----------------------------------------------------------------------------------------------- embeddings
def emb_response(vectors: list[list[float]], *, shuffle: bool = False) -> dict[str, Any]:
    data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    if shuffle:
        data.reverse()
    return {"object": "list", "data": data, "usage": {"prompt_tokens": 3}}


async def test_embed_batches_and_orders(script: Script) -> None:
    v = [[float(i)] * 8 for i in range(5)]
    script.add("embedding", emb_response(v[:2], shuffle=True), emb_response(v[2:4]), emb_response(v[4:]))
    async with make(script, options=GatewayOptions(embed_batch_size=2)) as gw:
        out = await gw.embed([f"t{i}" for i in range(5)], ctx=CallContext(purpose="embedding"))
        assert await gw.embed([], ctx=CallContext(purpose="embedding")) == []
    assert out == v
    assert [r["input"] for r in script.requests] == [["t0", "t1"], ["t2", "t3"], ["t4"]]
    assert script.requests[0]["options"] == {"num_ctx": 2048} and script.requests[0]["keep_alive"] == "10m"
    assert "encoding_format" not in script.requests[0]  # LiteLLM rejects it for ollama (UnsupportedParamsError)


async def test_embed_dimension_and_count_checks(script: Script) -> None:
    script.add("embedding", emb_response([[0.1] * 3]), emb_response([[0.1] * 8]))
    async with make(script) as gw:
        with pytest.raises(ModelError) as exc:
            await gw.embed(["a"], ctx=CallContext(purpose="embedding"))
        assert exc.value.code == EMBEDDING_DIMENSION_MISMATCH
        with pytest.raises(ModelError) as exc:
            await gw.embed(["a", "b"], ctx=CallContext(purpose="embedding"))
        assert exc.value.code == MODEL_PROTOCOL_ERROR


async def test_embed_rejects_oversized_input(script: Script) -> None:
    async with make(script) as gw:
        with pytest.raises(ValidationFailed) as exc:
            await gw.embed(["ok", "x" * 10_000], ctx=CallContext(purpose="embedding"))
    assert exc.value.code == "CONTEXT_OVERFLOW" and exc.value.details["index"] == 1
    assert script.requests == []


async def test_embed_never_falls_back(script: Script) -> None:
    cfg = models_config()
    emb = cfg.by_alias("embedding")
    extra = emb.model_copy(update={"alias": "embedding-alt", "role": "research", "fallback_for": "embedding"})
    cfg = cfg.model_copy(update={"profiles": [*cfg.profiles, extra]})
    script.add("embedding", (500, {"error": {"message": "failed to load"}}))
    client = httpx.AsyncClient(transport=httpx.MockTransport(script.handler))
    async with LiteLLMGateway(cfg, api_key=MASTER_KEY, http_client=client) as gw:
        with pytest.raises(ModelError):
            await gw.embed(["a"], ctx=CallContext(purpose="embedding"))
    assert script.models() == ["embedding"]


def test_architecture_violation_refuses_gateway() -> None:
    cfg = models_config()
    coder = cfg.by_alias("coder-main")
    bad = cfg.model_copy(update={"profiles": [p.model_copy(update={"model": "llama3:70b"}) if p is coder else p for p in cfg.profiles]})
    with pytest.raises(ConfigError) as exc:
        LiteLLMGateway(bad, api_key="x")
    assert exc.value.code == "MODEL_ARCHITECTURE_VIOLATION"
    LiteLLMGateway(bad, api_key="x", options=GatewayOptions(validate_architecture=False))


def test_base_url_validation() -> None:
    with pytest.raises(ConfigError):
        LiteLLMGateway(models_config(), base_url="file:///etc/passwd")
    with pytest.raises(ConfigError):
        LiteLLMGateway(models_config(), base_url="http://user:pw@127.0.0.1:4000")


def test_from_config_resolves_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermclaw.core.config import load_config

    monkeypatch.setenv("HERMCLAW_TEST_LITELLM_KEY", "sk-from-env-key-1234567890abcdef")
    cfg = load_config()
    cfg = cfg.model_copy(
        update={
            "models": cfg.models.model_copy(
                update={"litellm": cfg.models.litellm.model_copy(update={"api_key_ref": "env:HERMCLAW_TEST_LITELLM_KEY"})}
            )
        }
    )
    gw = LiteLLMGateway.from_config(cfg)
    assert gw._headers()["Authorization"] == "Bearer sk-from-env-key-1234567890abcdef"
    from hermclaw.core.redaction import DEFAULT_REDACTOR

    assert "sk-from-env-key" not in DEFAULT_REDACTOR.text("key sk-from-env-key-1234567890abcdef")


def test_module_exports_stable_codes() -> None:
    assert {MODEL_UNAVAILABLE, MODEL_LOAD_FAILED, MODEL_NOT_FOUND, "MODEL_TIMEOUT", MODEL_PROTOCOL_ERROR} == gw_mod.TECHNICAL_FAILURE_CODES


# ----------------------------------------------------------------------------------------------- self-review regressions
def test_extract_json_hostile_inputs() -> None:
    """Untrusted model output must always surface as ValueError (→ repair), never as RecursionError & co."""
    with pytest.raises(ValueError, match="too deep"):
        extract_json("[" * 100_000)
    with pytest.raises(ValueError):
        extract_json('{"a": ' + "1" * 5000 + "}")  # int digit limit
    t0 = time.monotonic()
    with pytest.raises(ValueError):
        extract_json("{x " * 50_000)  # bounded candidate scan
    assert time.monotonic() - t0 < 2.0
    assert extract_json("{x " * 10 + '{"ok": 1}') == {"ok": 1}
    with pytest.raises(ValueError):
        extract_json("{x " * 10 + '{"ok": 1}', max_candidates=5)


async def test_structured_hostile_nesting_triggers_repair(script: Script) -> None:
    script.add("planner-gemma", completion("[" * 60_000), completion('{"title": "t", "steps": ["a"]}'))
    async with make(script) as gw:
        out = await gw.structured("planner-gemma", [ChatMessage("user", "plan")], Plan, ctx=CTX)
    assert out.repair_attempts == 1 and out.value.steps == ["a"]
    assert "too deep" in script.requests[1]["messages"][-1]["content"]
    # the echo of the rejected answer is capped
    assert len(script.requests[1]["messages"][-2]["content"]) <= gw_mod.MAX_REPAIR_ECHO_CHARS + 1


async def test_structured_repair_drops_echo_when_context_is_tight(script: Script) -> None:
    # fast-router: 16384 ctx, 2048 output → the prompt (~13.5K tokens) fits, prompt + echo (~1.9K tokens) does not
    big = "x" * int(3.2 * 13_500)
    script.add("fast-router", completion("not json " * 800), completion('{"title": "t", "steps": ["a"]}'))
    async with make(script) as gw:
        out = await gw.structured("fast-router", [ChatMessage("user", big)], Plan, ctx=CTX)
    assert out.repair_attempts == 1
    repair = script.requests[1]["messages"]
    assert [m["role"] for m in repair] == ["user", "user"]
    assert "rejected" in repair[-1]["content"] and "not json" not in repair[-1]["content"]


async def test_structured_repair_keeps_echo_when_it_fits(script: Script) -> None:
    script.add("fast-router", completion("not json"), completion('{"title": "t", "steps": ["a"]}'))
    async with make(script) as gw:
        await gw.structured("fast-router", [ChatMessage("user", "short")], Plan, ctx=CTX)
    assert [m["role"] for m in script.requests[1]["messages"]] == ["user", "assistant", "user"]
