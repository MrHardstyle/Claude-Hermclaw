import asyncio
import json
import socket
import uuid

import httpx
import pytest
import uvicorn

from hermclaw.api.app import create_app
from hermclaw.api.auth import reset_auth_cache
from hermclaw.core.settings import reset_settings_cache
from hermclaw.events.store import append_event
from hermclaw.persistence.models import ApiToken, Artifact
from hermclaw.security.tokens import generate_token, hash_token

pytestmark = pytest.mark.integration
TOKEN = "test-admin-token-123456"


@pytest.fixture
def app(engine, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMCLAW_API_TOKEN_REF", f"literal:{TOKEN}")
    monkeypatch.setenv("HERMCLAW_DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    reset_auth_cache()
    from hermclaw.api.state import AppState

    a = create_app(lifespan_hooks=False)
    a.state.hermclaw = AppState()
    yield a
    reset_settings_cache()
    reset_auth_cache()


def _client(app, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers)


async def test_auth_required_and_scopes(app, sessionmaker):
    async with _client(app, token=None) as c:
        assert (await c.get("/api/jobs")).status_code == 401
        assert (await c.get("/api/health")).status_code == 200  # health is public
    read_token = generate_token()
    async with sessionmaker() as s:
        s.add(ApiToken(name=f"ro-{uuid.uuid4().hex[:6]}", token_hash=hash_token(read_token), scopes=["read"]))
        await s.commit()
    async with _client(app, token=read_token) as c:
        assert (await c.get("/api/jobs")).status_code == 200
        r = await c.post("/api/jobs", json={"prompt": "do something"})
        assert r.status_code == 403
    async with _client(app, token="wrong") as c:
        assert (await c.get("/api/jobs")).status_code == 401


async def test_job_lifecycle_controls(app):
    async with _client(app) as c:
        r = await c.post(
            "/api/jobs",
            json={
                "prompt": "Add a /health endpoint\nwith tests",
                "repository": {"url": "file:///srv/git/demo.git", "base_branch": "main"},
                "constraints": ["keep API stable"],
            },
        )
        assert r.status_code == 201, r.text
        job = r.json()
        assert job["status"] == "queued" and job["title"] == "Add a /health endpoint"
        jid = job["id"]
        assert (await c.get(f"/api/jobs/{jid}")).json()["steps"] == []
        assert any(j["id"] == jid for j in (await c.get("/api/jobs?status=queued")).json()["items"])
        evs = (await c.get(f"/api/jobs/{jid}/events")).json()
        assert [e["event_type"] for e in evs][:1] == ["job.created"]
        assert (await c.post(f"/api/jobs/{jid}/pause")).json()["accepted"]
        assert (await c.get(f"/api/jobs/{jid}")).json()["pause_requested"] is True
        assert (await c.post(f"/api/jobs/{jid}/resume")).status_code == 200
        assert (await c.post(f"/api/jobs/{jid}/replan", params={"reason": "new info"})).status_code == 200
        r = await c.post(f"/api/jobs/{jid}/cancel")
        assert r.json()["status"] == "cancelled"
        assert (await c.post(f"/api/jobs/{jid}/cancel")).status_code == 409
        assert (await c.post(f"/api/jobs/{jid}/retry")).status_code == 409  # cancelled is terminal
        repos = (await c.get("/api/repositories")).json()
        assert any(r["url"] == "file:///srv/git/demo.git" for r in repos)
        assert (await c.get(f"/api/jobs/{uuid.uuid4()}")).status_code == 404
        assert (await c.post("/api/jobs", json={"prompt": "x", "repository": {"url": "ftp://bad"}})).status_code in (422,)


async def test_system_endpoints(app):
    async with _client(app) as c:
        assert (await c.get("/api/workers")).status_code == 200
        models = (await c.get("/api/models")).json()
        assert {p["alias"] for p in models["profiles"]} >= {"planner-gemma", "coder-main", "heavy-review", "fast-router", "embedding"}
        assert "active_leases" in (await c.get("/api/resources")).json()
        assert (await c.get("/api/bugs")).status_code == 200
        assert (await c.get("/api/stats")).json()["jobs_total"] >= 0


async def test_artifact_download_confined(app, sessionmaker, tmp_path):
    from hermclaw.core.settings import get_settings

    adir = get_settings().artifacts_dir
    adir.mkdir(parents=True, exist_ok=True)
    good = adir / "diff.patch"
    good.write_text("diff --git a/x b/x\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    async with _client(app) as c:
        jid = (await c.post("/api/jobs", json={"prompt": "artifact job"})).json()["id"]
    async with sessionmaker() as s:
        a1 = Artifact(job_id=uuid.UUID(jid), kind="diff", name="diff.patch", path=str(good), media_type="text/x-diff", size_bytes=19)
        a2 = Artifact(job_id=uuid.UUID(jid), kind="other", name="secret.txt", path=str(outside))
        s.add_all([a1, a2])
        await s.commit()
        ids = (a1.id, a2.id)
    async with _client(app) as c:
        assert (await c.get(f"/api/artifacts/{ids[0]}/download")).text.startswith("diff --git")
        assert (await c.get(f"/api/artifacts/{ids[1]}/download")).status_code == 404
        assert (await c.get(f"/api/jobs/{jid}/diff")).json()["diff"].startswith("diff --git")
        assert len((await c.get(f"/api/jobs/{jid}/artifacts")).json()) == 2


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def test_sse_stream_over_real_http(app, sessionmaker):
    app.router.lifespan_context = None  # state already attached by fixture
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as c:
            jid = (await c.post("/api/jobs", json={"prompt": "stream job"}, headers={"Authorization": f"Bearer {TOKEN}"})).json()["id"]
            seen = []
            async with c.stream("GET", f"/api/jobs/{jid}/events/stream", params={"access_token": TOKEN}) as resp:
                assert resp.headers["content-type"].startswith("text/event-stream")
                assert resp.headers.get("x-accel-buffering") == "no"

                async def produce():
                    await asyncio.sleep(0.3)
                    async with sessionmaker() as s:
                        await append_event(s, "status", source_type="test", job_id=uuid.UUID(jid), payload={"text": "live!"})
                        await s.commit()

                prod = asyncio.create_task(produce())
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        seen.append(json.loads(line[5:]))
                        if any(e["payload"].get("text") == "live!" for e in seen):
                            break
                await prod
            types = [e["event_type"] for e in seen]
            assert "job.created" in types and seen[-1]["payload"]["text"] == "live!"
            # reconnect with Last-Event-ID: only newer events
            last = seen[-1]["sequence"]
            async with sessionmaker() as s:
                await append_event(s, "status", source_type="test", job_id=uuid.UUID(jid), payload={"text": "after"})
                await s.commit()
            async with c.stream(
                "GET", f"/api/jobs/{jid}/events/stream", params={"access_token": TOKEN}, headers={"Last-Event-ID": str(last)}
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        ev = json.loads(line[5:])
                        assert ev["sequence"] > last and ev["payload"]["text"] == "after"
                        break
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
