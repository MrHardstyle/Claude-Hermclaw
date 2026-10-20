"""Knowledge API of the "Wissen" page (D-019): the operator lists, searches, writes and retires entries; reading needs
``read``, writing ``control``; the vector never leaves the server."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import delete

from hermclaw.api.app import create_app
from hermclaw.api.auth import reset_auth_cache
from hermclaw.core.config import MemoryPolicy
from hermclaw.core.settings import reset_settings_cache
from hermclaw.memory.store import MemoryStore
from hermclaw.persistence.models import ApiToken, MemoryEntry
from hermclaw.security.tokens import generate_token, hash_token
from tests.integration.test_repo_intelligence_support import HashingEmbedder

pytestmark = pytest.mark.integration
TOKEN = "test-admin-token-memory-1"


@pytest.fixture
async def app(engine, sessionmaker, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMCLAW_API_TOKEN_REF", f"literal:{TOKEN}")
    monkeypatch.setenv("HERMCLAW_DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    reset_auth_cache()
    from hermclaw.api.state import AppState

    async with sessionmaker() as s:
        await s.execute(delete(MemoryEntry))
        await s.commit()
    a = create_app(lifespan_hooks=False)
    a.state.hermclaw = AppState()
    a.state.hermclaw.memory = MemoryStore(sessionmaker, HashingEmbedder(), MemoryPolicy(min_score=0.2))
    yield a
    reset_settings_cache()
    reset_auth_cache()


def _client(app, token=TOKEN):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers={"Authorization": f"Bearer {token}"})


async def test_the_operator_writes_searches_edits_and_retires_knowledge(app) -> None:
    async with _client(app) as c:
        r = await c.post(
            "/api/memory",
            json={"kind": "lesson", "title": "Lint findings", "content": "Fix every ruff finding, also in tests", "tags": ["ruff"]},
        )
        assert r.status_code == 201, r.text
        entry = r.json()
        assert entry["status"] == "active" and entry["source"] == "operator" and entry["kind_label"] == "Lehre"
        assert entry["embedded"] is True and "embedding" not in entry
        hits = (await c.get("/api/memory/search", params={"q": "ruff finding tests"})).json()
        assert [h["id"] for h in hits] == [entry["id"]]
        assert (await c.get(f"/api/memory/{entry['id']}")).json()["recalls"] == 0  # the page's search is no recall
        r = await c.patch(f"/api/memory/{entry['id']}", json={"status": "retired", "content": "obsolete"})
        assert r.status_code == 200 and r.json()["status"] == "retired"
        assert (await c.get("/api/memory/search", params={"q": "ruff finding tests"})).json() == []
        assert [e["id"] for e in (await c.get("/api/memory", params={"status": "retired"})).json()] == [entry["id"]]
        assert (await c.get(f"/api/memory/{uuid.uuid4()}")).status_code == 404
        assert (await c.patch(f"/api/memory/{entry['id']}", json={})).status_code == 422
        store = app.state.hermclaw.memory  # a system entry whose text the operator corrects is the operator's
        learned = await store.add(kind="incident", title="seen twice", content="system text", signature="tool:x:Y")
        r = await c.patch(f"/api/memory/{learned}", json={"content": "operator text"})
        assert r.json()["source"] == "operator"
        r = await c.patch(f"/api/memory/{learned}", json={"status": "active"})
        assert r.json()["source"] == "operator" and r.json()["status"] == "active"
        assert (await c.post("/api/memory", json={"kind": "gossip", "title": "x", "content": "y"})).status_code == 422


async def test_reading_needs_read_and_writing_needs_control(app, sessionmaker) -> None:
    read_token = generate_token()
    async with sessionmaker() as s:
        s.add(ApiToken(name=f"ro-{uuid.uuid4().hex[:6]}", token_hash=hash_token(read_token), scopes=["read"]))
        await s.commit()
    async with _client(app, token=read_token) as c:
        assert (await c.get("/api/memory")).status_code == 200
        assert (await c.post("/api/memory", json={"title": "x", "content": "y"})).status_code == 403
