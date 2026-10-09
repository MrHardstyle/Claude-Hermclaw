"""P11 live verification (BLOCKER-001: skipped by default): the semantic index with the real EmbeddingGemma model
behind LiteLLM (``.225`` → Ollama ``.224``), against the PostgreSQL/pgvector of the test session.

Run on the LAN with the production ``models.yaml`` in ``HERMCLAW_CONFIG_DIR``::

    HERMCLAW_LIVE_LITELLM_KEY_REF=file:/etc/hermclaw/secrets/litellm-master-key \\
    .venv/bin/pytest -m live tests/integration/test_repo_intelligence_live.py
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from hermclaw.core.config import load_config
from hermclaw.models.gateway import LiteLLMGateway
from hermclaw.models.profiles import resolve_api_key
from hermclaw.persistence.models import EMBEDDING_DIM
from hermclaw.repo_intelligence import RepoIntelligence
from tests.integration.test_repo_intelligence_support import build_fixture_repo

pytestmark = [pytest.mark.live, pytest.mark.integration]


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} not set (live test, BLOCKER-001)")
    return value


async def test_live_embeddinggemma_index_and_semantic_search(sessionmaker: Any, tmp_path: Path) -> None:
    key = resolve_api_key(_env("HERMCLAW_LIVE_LITELLM_KEY_REF"), required=True)
    models = load_config(None).models
    url = os.environ.get("HERMCLAW_LIVE_LITELLM_URL")
    if url:
        models = models.model_copy(update={"litellm": models.litellm.model_copy(update={"base_url": url})})
    repo = build_fixture_repo(tmp_path / "fx")
    async with LiteLLMGateway(models, api_key=key) as gateway:
        assert gateway.dimensions in (0, EMBEDDING_DIM)
        svc = RepoIntelligence(sessionmaker, embedder=gateway)
        bound = svc.open(repo, f"live-{uuid.uuid4().hex[:8]}")
        stats = await bound.index()
        assert stats.embedding_error is None and stats.embedding_pending == 0 and stats.embedded == stats.chunks
        hits = await bound.semantic("add an item to the shopping cart", k=5)
        assert hits and hits[0].path == "web/src/cart.ts"
        fused = await bound.search("where is the invoice total calculated including tax", k=5)
        assert fused[0].path == "app/services/billing.py" and "semantic" in fused[0].signals
        await svc.aclose()
