"""P07 orchestrator worker API ``/api/workers`` (heartbeat auth + registry views) and the daemon heartbeat
sender end-to-end against it (PostgreSQL)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION, LoadedModel
from hermclaw.persistence.models import Event, Worker, WorkerHealth
from hermclaw.workers.api import WorkerApiContext, install_workers_api
from hermclaw.workers.auth import StaticTokenStore, WorkerRequestSigner, sign_headers
from hermclaw.workers.registry import RegistrySettings, WorkerRegistry
from hermclaw.workers.schemas import worker_api_schemas
from tests.integration.test_workers_support import TOKEN, make_settings
from worker.common.heartbeat import HeartbeatError, HeartbeatExtras, HeartbeatSender
from worker.common.state import DaemonState

pytestmark = pytest.mark.integration


def heartbeat_body(worker_id: str, **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "worker_id": worker_id,
        "hostname": "host",
        "kind": "execution",
        "state": "ready",
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "worker_version": "0.1.0rc1",
        "capabilities": ["sandbox"],
        "sent_at": datetime.now(UTC).isoformat(),
    }
    body.update(kw)
    return body


@pytest.fixture
def worker_id() -> str:
    return f"exec-{uuid.uuid4().hex[:10]}"


@pytest.fixture
async def orchestrator(sessionmaker: async_sessionmaker[AsyncSession], worker_id: str) -> AsyncIterator[tuple[FastAPI, WorkerApiContext]]:
    app = FastAPI()
    ctx = WorkerApiContext(
        sessionmaker=sessionmaker,
        token_store=StaticTokenStore({worker_id: TOKEN}),
        registry=WorkerRegistry(RegistrySettings(heartbeat_interval_seconds=7)),
    )
    install_workers_api(app, ctx)
    yield app, ctx
    async with sessionmaker() as s:
        await s.execute(Worker.__table__.delete().where(Worker.id == worker_id))
        await s.commit()


def client(app: FastAPI, auth: httpx.Auth | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://orchestrator", auth=auth)


async def test_signed_heartbeat_accepted_and_visible(orchestrator: tuple[FastAPI, WorkerApiContext], worker_id: str) -> None:
    app, _ctx = orchestrator
    async with client(app, WorkerRequestSigner(worker_id, TOKEN)) as c:
        r = await c.post("/api/workers/heartbeat", content=json.dumps(heartbeat_body(worker_id)).encode())
        assert r.status_code == 200, r.text
        ack = r.json()
        assert ack["accepted"] and ack["compatible"] and ack["state"] == "ready" and ack["heartbeat_interval_seconds"] == 7
        assert ack["expected_protocol_version"] == WORKER_PROTOCOL_VERSION
    async with client(app) as c:
        listed = (await c.get("/api/workers")).json()
        assert worker_id in {w["id"] for w in listed}
        detail = (await c.get(f"/api/workers/{worker_id}", params={"health_limit": 5})).json()
        assert detail["state"] == "ready" and detail["capabilities"] == ["sandbox"] and len(detail["health"]) == 1
        assert (await c.get("/api/workers", params={"kind": "model"})).json() == [] or all(
            w["kind"] == "model" for w in (await c.get("/api/workers", params={"kind": "model"})).json()
        )
        missing = await c.get("/api/workers/exec-does-not-exist")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "WORKER_NOT_FOUND"
        assert (await c.get("/api/workers/bad%20id")).status_code == 422


async def test_heartbeat_auth_failures(orchestrator: tuple[FastAPI, WorkerApiContext], worker_id: str) -> None:
    app, _ctx = orchestrator
    body = json.dumps(heartbeat_body(worker_id)).encode()
    async with client(app) as c:
        r = await c.post("/api/workers/heartbeat", content=body)
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_MISSING"
        headers = sign_headers(worker_id=worker_id, token=TOKEN, method="POST", path="/api/workers/heartbeat", body=body)
        tampered = json.dumps(heartbeat_body(worker_id, state="busy")).encode()
        r = await c.post("/api/workers/heartbeat", content=tampered, headers=headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_BAD_SIGNATURE"
        assert (await c.post("/api/workers/heartbeat", content=body, headers=headers)).status_code == 200
        r = await c.post("/api/workers/heartbeat", content=body, headers=headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_REPLAY"
        old = sign_headers(worker_id=worker_id, token=TOKEN, method="POST", path="/api/workers/heartbeat", body=body, now=0)
        r = await c.post("/api/workers/heartbeat", content=body, headers=old)
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_SKEW"
        stranger = sign_headers(worker_id="exec-unknown", token=TOKEN, method="POST", path="/api/workers/heartbeat", body=body)
        r = await c.post("/api/workers/heartbeat", content=body, headers=stranger)
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_UNKNOWN"
    async with client(app, WorkerRequestSigner(worker_id, TOKEN)) as c:
        # authenticated as worker A but claims to be worker B
        r = await c.post("/api/workers/heartbeat", content=json.dumps(heartbeat_body("exec-other")).encode())
        assert r.status_code == 403 and r.json()["error"]["code"] == "WORKER_ID_MISMATCH"
        r = await c.post("/api/workers/heartbeat", content=json.dumps(heartbeat_body(worker_id, state="flying")).encode())
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_FAILED"
        r = await c.post("/api/workers/heartbeat", content=b"x" * (300 * 1024))
        assert r.status_code == 413


async def test_incompatible_worker_gets_ack_and_error_state(
    orchestrator: tuple[FastAPI, WorkerApiContext], worker_id: str, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    app, _ctx = orchestrator
    async with client(app, WorkerRequestSigner(worker_id, TOKEN)) as c:
        body = heartbeat_body(worker_id, protocol_version=WORKER_PROTOCOL_VERSION + 1)
        ack = (await c.post("/api/workers/heartbeat", content=json.dumps(body).encode())).json()
    assert ack["accepted"] and not ack["compatible"] and ack["state"] == "error" and "protocol_version" in ack["message"]
    async with sessionmaker() as s:
        evs = (await s.execute(select(Event).where(Event.source_id == worker_id, Event.event_type == EventType.WORKER_STATE))).scalars().all()
        assert any(e.payload.get("reason") == "protocol_version_mismatch" for e in evs)


async def test_drain_endpoint(orchestrator: tuple[FastAPI, WorkerApiContext], worker_id: str) -> None:
    app, _ctx = orchestrator
    async with client(app, WorkerRequestSigner(worker_id, TOKEN)) as c:
        await c.post("/api/workers/heartbeat", content=json.dumps(heartbeat_body(worker_id)).encode())
    async with client(app) as c:
        r = await c.post(f"/api/workers/{worker_id}/drain", json={"drain": True, "reason": "maintenance"})
        assert r.status_code == 200 and r.json()["state"] == "draining" and r.json()["admin_drain"]
        r = await c.post(f"/api/workers/{worker_id}/drain", json={"drain": False})
        assert r.json()["state"] == "ready"
        r = await c.post("/api/workers/exec-nobody/drain", json={"drain": True})
        assert r.status_code == 404


async def test_heartbeat_sender_end_to_end(
    orchestrator: tuple[FastAPI, WorkerApiContext], worker_id: str, tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Real daemon heartbeat sender (signing, metrics, extras) -> real orchestrator API -> PostgreSQL."""
    app, _ctx = orchestrator
    settings = make_settings(
        tmp_path, WorkerKind.model, worker_id, orchestrator_url="http://orchestrator", heartbeat_seconds=30, capabilities=("embedding",)
    )
    # registered as model worker via auto-registration from the first heartbeat
    state = DaemonState(worker_id, WorkerKind.model)
    state.starting = False

    async def extras() -> HeartbeatExtras:
        return HeartbeatExtras(loaded_models=[LoadedModel(name="qwen3:8b", size_vram_bytes=5)], service_versions={"ollama": "0.32.12"})

    sender = HeartbeatSender(settings, state, lambda: TOKEN, extras=extras, transport=httpx.ASGITransport(app=app))
    ack = await sender.send_once()
    assert ack.accepted and ack.state == WorkerState.ready and sender.interval_seconds == 7 and state.orchestrator_reachable
    with state.work("load:x", kind="model_load", job_id="job-9", step_id="step-9"):
        ack = await sender.send_once()
        assert ack.state == WorkerState.busy
    async with sessionmaker() as s:
        row = await s.get(Worker, worker_id)
        assert row is not None and row.kind == "model" and row.state == "busy" and row.active_job_id == "job-9"
        assert row.metadata_["service_versions"]["ollama"] == "0.32.12"
        assert row.metadata_["resources"]["ram_total_mb"] > 0
        health = (await s.execute(select(WorkerHealth).where(WorkerHealth.worker_id == worker_id))).scalars().all()
        assert len(health) == 2 and health[-1].loaded_models[0]["name"] == "qwen3:8b"
    # background loop + final offline heartbeat on stop
    sender.start()
    assert sender.running
    await sender.stop(final=True)
    async with sessionmaker() as s:
        row = await s.get(Worker, worker_id)
        assert row is not None and row.state == "offline"
    assert sender.sent >= 2


async def test_heartbeat_sender_reports_rejection(orchestrator: tuple[FastAPI, WorkerApiContext], worker_id: str, tmp_path: Path) -> None:
    app, _ctx = orchestrator
    settings = make_settings(tmp_path, WorkerKind.execution, worker_id, orchestrator_url="http://orchestrator")
    sender = HeartbeatSender(settings, DaemonState(worker_id, WorkerKind.execution), lambda: "w" * 64, transport=httpx.ASGITransport(app=app))
    with pytest.raises(HeartbeatError) as exc:
        await sender.send_once()
    assert exc.value.code == "WORKER_AUTH_BAD_TOKEN" and exc.value.status_code == 401
    assert sender.failures == 1 and sender.state.orchestrator_reachable is True
    await sender.stop(final=False)


def test_worker_api_schemas_cover_protocol() -> None:
    schemas = worker_api_schemas()
    for name in ("WorkerHeartbeat", "HeartbeatAck", "CommandRequest", "CommandResult", "ModelLoadRequest", "DaemonHealth", "ErrorResponse"):
        assert name in schemas and schemas[name]["type"] == "object"
