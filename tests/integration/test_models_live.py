"""P08 live verification: gateway → real LiteLLM (.225) → real Ollama (.224) (BLOCKER-001: skipped by default).

Run on the LAN (from ``.225``, with the production ``models.yaml`` in ``HERMCLAW_CONFIG_DIR``) with::

    HERMCLAW_LIVE_LITELLM_KEY_REF=file:/etc/hermclaw/secrets/litellm-master-key \\
    .venv/bin/pytest -m live tests/integration/test_models_live.py

Optional overrides: ``HERMCLAW_LIVE_LITELLM_URL`` (default: ``models.litellm.base_url``),
``HERMCLAW_LIVE_OLLAMA_URL`` (default: the ``ollama`` service of the model host in ``hosts.yaml``).
These tests load models on ``.224`` – run them only when no job holds a model lease.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel

from hermclaw.core.config import HermclawConfig, load_config
from hermclaw.models.gateway import LiteLLMGateway
from hermclaw.models.health import ModelHealthChecker
from hermclaw.models.profiles import ProfileRegistry, ollama_base_url, resolve_api_key
from hermclaw.models.protocols import CallContext, ChatMessage
from hermclaw.models.residency import ModelResidency, OllamaHostClient

pytestmark = [pytest.mark.live, pytest.mark.integration]


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} not set (live test, BLOCKER-001)")
    return value


class Verdict(BaseModel):
    ok: bool
    reason: str


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    key = resolve_api_key(_env("HERMCLAW_LIVE_LITELLM_KEY_REF"), required=True)
    cfg: HermclawConfig = load_config(None)
    models = cfg.models
    url = os.environ.get("HERMCLAW_LIVE_LITELLM_URL")
    if url:
        models = models.model_copy(update={"litellm": models.litellm.model_copy(update={"base_url": url})})
    registry = ProfileRegistry(models)
    host = registry.by_role("planner").host
    ollama = os.environ.get("HERMCLAW_LIVE_OLLAMA_URL") or ollama_base_url(cfg.hosts, host)
    return {"models": models, "key": key, "registry": registry, "ollama": ollama, "host": host}


@pytest.fixture
async def gw(live: dict[str, Any]) -> AsyncIterator[LiteLLMGateway]:
    async with LiteLLMGateway(live["models"], api_key=live["key"]) as gateway:
        yield gateway


def _ctx(purpose: str) -> CallContext:
    return CallContext(purpose=purpose, job_id=uuid.uuid4())


async def test_live_health_all_profiles_available(live: dict[str, Any]) -> None:
    checker = ModelHealthChecker(live["models"], ollama_urls={live["host"]: live["ollama"]}, api_key=live["key"])
    try:
        report = await checker.check()
    finally:
        await checker.aclose()
    assert report.litellm_liveliness.ok and report.litellm_readiness.ok, report.issues
    assert report.healthy, report.issues


async def test_live_fast_router_returns_only_final_content(gw: LiteLLMGateway, live: dict[str, Any]) -> None:
    alias = live["registry"].by_role("fast").alias
    res = await gw.chat(alias, [ChatMessage("user", "Reply with the single word: pong")], ctx=_ctx("triage"), max_tokens=64)
    assert res.content.strip() and "<think>" not in res.content.lower()
    assert res.prompt_tokens and res.completion_tokens and res.finish_reason in ("stop", "length")


async def test_live_planner_structured_output(gw: LiteLLMGateway, live: dict[str, Any]) -> None:
    alias = live["registry"].by_role("planner").alias
    out = await gw.structured(
        alias,
        [ChatMessage("system", "Answer as JSON."), ChatMessage("user", "Is 2+2=4? Give ok and a short reason.")],
        Verdict,
        ctx=_ctx("planner"),
        max_tokens=256,
    )
    assert out.value.ok is True and out.value.reason


async def test_live_planner_fallback_alias_serves(gw: LiteLLMGateway, live: dict[str, Any]) -> None:
    alias = live["registry"].by_role("planner_fallback").alias
    res = await gw.chat(alias, [ChatMessage("user", "Reply with: ok")], ctx=_ctx("planner"), max_tokens=32)
    assert res.content.strip()


async def test_live_residency_loads_coder_with_configured_context(live: dict[str, Any]) -> None:
    client = OllamaHostClient(live["ollama"])
    try:
        residency = ModelResidency(live["models"], client)
        coder = live["registry"].by_role("coder")
        result = await residency.ensure_loaded(coder.alias, lease_id="live-test")
        assert result.context_verified and result.context_length == coder.context_tokens
        resident = {m.name for m in await residency.status(coder.host)}
        others = {p.model for p in live["registry"].group_members(coder.resource_group, host=coder.host) if p.model != coder.model}
        assert not (resident & others)  # exclusive group: no second large model resident
    finally:
        await client.aclose()


async def test_live_embedding_dimensions(gw: LiteLLMGateway) -> None:
    vectors = await gw.embed(["hermclaw", "model gateway"], ctx=_ctx("embedding"))
    assert len(vectors) == 2 and all(len(v) == gw.dimensions for v in vectors)
