"""E2E harness for the Hermclaw web UI (P31).

Run with the repository venv from the repository root (``PYTHONPATH=<repo>``):

    seed.py prepare  --state STATE           create a throw-away PostgreSQL database + run migrations
    seed.py seed     --state STATE           seed realistic data (one job through the real API, the rest via the ORM)
    seed.py emit     --state STATE --job ID --text TEXT [--type status]
                                             append one event (NOTIFY on commit -> real SSE delivery)
    seed.py job      --state STATE --title T --status S
                                             create a job in a given status (prints its id)
    seed.py status   --state STATE --job ID --status S
                                             force a job status directly in the DB (simulates a concurrent transition)
    seed.py teardown --state STATE           drop the database and the data directory

The state file (JSON) carries everything the Playwright tests need: token, ids, URLs.
No secrets besides the throw-away test token are written; the token is only valid for this test database.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import struct
import sys
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[3]
ADMIN_URL_DEFAULT = "postgresql://postgres@127.0.0.1:5432/postgres"


def _load_state(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _apply_env(state: dict[str, Any]) -> None:
    os.environ["HERMCLAW_DATABASE_URL"] = str(state["database_url"])
    os.environ["HERMCLAW_ENV"] = "test"
    os.environ["HERMCLAW_DATA_DIR"] = str(state["data_dir"])
    os.environ["HERMCLAW_API_TOKEN_REF"] = f"literal:{state['token']}"


# ============================================================================================ prepare / teardown
def prepare(state_path: Path, admin_url: str, data_dir: Path) -> dict[str, Any]:
    name = f"hermclaw_ui_e2e_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    base = admin_url.rsplit("/", 1)[0]
    state: dict[str, Any] = {
        "db_name": name,
        "admin_url": admin_url,
        "database_url": f"{base.replace('postgresql://', 'postgresql+psycopg://', 1)}/{name}",
        "token": f"ui-e2e-{uuid.uuid4().hex}",
        "data_dir": str(data_dir),
    }
    if data_dir.exists():
        shutil.rmtree(data_dir)
    (data_dir / "artifacts").mkdir(parents=True)
    _apply_env(state)
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    _write_state(state_path, state)
    return state


def teardown(state_path: Path) -> None:
    if not state_path.exists():
        return
    state = _load_state(state_path)
    with psycopg.connect(str(state["admin_url"]), autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (state["db_name"],),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{state["db_name"]}"')
    shutil.rmtree(str(state["data_dir"]), ignore_errors=True)


# ============================================================================================ helpers
def _png(width: int = 8, height: int = 6) -> bytes:
    """A small valid RGB PNG (gradient) so the media preview renders a real image."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes((x * 30) % 256 for x in range(width) for _ in range(3)) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


DIFF_TEXT = """diff --git a/src/api.py b/src/api.py
index 1111111..2222222 100644
--- a/src/api.py
+++ b/src/api.py
@@ -1,6 +1,9 @@
 from fastapi import FastAPI
+from fastapi.responses import JSONResponse

 app = FastAPI()

-@app.get("/health")
-def health():
-    return {"ok": True}
+
+@app.get("/health")
+def health() -> JSONResponse:
+    return JSONResponse({"ok": True, "version": "1.2.0"})
+
diff --git a/tests/test_api.py b/tests/test_api.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_api.py
@@ -0,0 +1,4 @@
+def test_health(client):
+    r = client.get("/health")
+    assert r.status_code == 200
+    assert r.json()["ok"] is True
"""


def _write_artifact(data_dir: Path, job_id: uuid.UUID, name: str, content: bytes) -> tuple[str, int, str]:
    folder = data_dir / "artifacts" / str(job_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(content)
    return str(path), len(content), hashlib.sha256(content).hexdigest()


def _api_create_job(api_url: str, token: str, body: dict[str, Any]) -> str:
    r = httpx.post(f"{api_url}/api/jobs", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
    r.raise_for_status()
    return str(r.json()["id"])


# ============================================================================================ seed
async def _seed_db(state: dict[str, Any], api_job_ids: dict[str, str]) -> dict[str, Any]:
    from sqlalchemy import select

    from hermclaw.contracts.common import Severity
    from hermclaw.contracts.events import EventType
    from hermclaw.core.config import get_config
    from hermclaw.events.store import append_event
    from hermclaw.persistence import models as m
    from hermclaw.persistence.db import dispose_engine, get_sessionmaker, init_engine
    from hermclaw.runtime.transitions import emit_status

    init_engine(str(state["database_url"]))
    data_dir = Path(str(state["data_dir"]))
    now = datetime.now(UTC)

    def t(minutes: float) -> datetime:
        return now - timedelta(minutes=minutes)

    profiles = {p.role: p for p in get_config().models.profiles}
    coder = profiles["coder"]
    out: dict[str, Any] = {"jobs": dict(api_job_ids), "steps": {}}

    main_id = uuid.UUID(api_job_ids["main"])
    queued_id = uuid.UUID(api_job_ids["queued"])
    try:
        async with get_sessionmaker()() as s:
            repo = (await s.execute(select(m.Repository).where(m.Repository.name == "demo-repo"))).scalar_one_or_none()
            assert repo is not None, "repository must have been registered through the API"

            # ------------------------------------------------------------------ main job: running, replanned once
            main = await s.get(m.Job, main_id)
            assert main is not None
            main.status = "running"
            main.started_at = t(40)
            main.current_plan_version = 2
            main.replan_count = 1

            plan = m.Plan(job_id=main_id, status="active", current_version=2)
            s.add(plan)
            await s.flush()

            def plan_step(sid: str, title: str, kind: str, cap: str, deps: list[str]) -> dict[str, Any]:
                return {
                    "id": sid,
                    "title": title,
                    "kind": kind,
                    "capability": cap,
                    "goal": f"{title} (Ziel)",
                    "depends_on": deps,
                    "risk": "low",
                }

            v1_steps = [
                plan_step("S1", "Repository inventarisieren", "inventory", "repo.inventory", []),
                plan_step("S2", "FastAPI-SSE recherchieren", "research", "research.web", ["S1"]),
                plan_step("S3", "Health-Endpoint implementieren", "implement", "code.python", ["S1", "S2"]),
            ]
            v2_steps = [*v1_steps, plan_step("S4", "Tests ergänzen und ausführen", "test", "test.pytest", ["S3"])]
            pv1 = m.PlanVersion(
                plan_id=plan.id,
                job_id=main_id,
                version=1,
                source="planner",
                model_alias="planner-gemma",
                plan_json={
                    "goal": "Health-Endpoint mit Version",
                    "summary": "Erster Plan",
                    "assumptions": ["FastAPI vorhanden"],
                    "steps": v1_steps,
                },
                validation_errors=[],
                repair_attempts=1,
                created_at=t(38),
            )
            pv2 = m.PlanVersion(
                plan_id=plan.id,
                job_id=main_id,
                version=2,
                source="replanner",
                model_alias="planner-gemma",
                plan_json={
                    "goal": "Health-Endpoint mit Version",
                    "summary": "Neuplanung nach fehlgeschlagenem Test: eigener Testschritt",
                    "assumptions": ["FastAPI vorhanden", "pytest im Repository"],
                    "risks": ["Bestehende Clients erwarten das alte Format"],
                    "research_needed": [{"question": "Wie liefert FastAPI JSON-Antworten?", "reason": "API-Vertrag"}],
                    "steps": v2_steps,
                },
                validation_errors=[],
                repair_attempts=0,
                reason="Test fehlgeschlagen – Neuplanung",
                created_at=t(20),
            )
            s.add_all([pv1, pv2])
            await s.flush()

            def step(key: str, title: str, kind: str, cap: str, status: str, **kw: Any) -> m.Step:
                row = m.Step(
                    job_id=main_id,
                    plan_version_id=pv2.id,
                    step_key=key,
                    title=title,
                    kind=kind,
                    capability=cap,
                    goal=f"{title} (Ziel)",
                    status=status,
                    **kw,
                )
                s.add(row)
                return row

            s1 = step("S1", "Repository inventarisieren", "inventory", "repo.inventory", "completed", started_at=t(39), finished_at=t(37))
            s2 = step(
                "S2",
                "FastAPI-SSE recherchieren",
                "research",
                "research.web",
                "completed",
                started_at=t(37),
                finished_at=t(30),
                network=True,
            )
            s3 = step(
                "S3",
                "Health-Endpoint implementieren",
                "implement",
                "code.python",
                "verifying",
                attempt_count=2,
                correction_count=1,
                assigned_worker_id="coder-222",
                current_scope_version=2,
                started_at=t(29),
                acceptance=[
                    {"type": "presence", "path_glob": "src/api.py", "pattern": "version", "description": "Version im Health-Endpoint"},
                    {"type": "test", "command": "pytest -q tests/test_api.py", "framework": "pytest", "description": "API-Tests grün"},
                ],
            )
            s4 = step("S4", "Tests ergänzen und ausführen", "test", "test.pytest", "pending")
            # superseded step of plan v1 (same key, replaced by the replan)
            s3_old = m.Step(
                job_id=main_id,
                plan_version_id=pv1.id,
                step_key="S3",
                title="Health-Endpoint implementieren (v1)",
                kind="implement",
                capability="code.python",
                goal="Erster Versuch",
                status="failed",
                superseded=True,
                error_code="TESTS_FAILED",
                error_message="2 Tests fehlgeschlagen",
            )
            s.add(s3_old)
            await s.flush()
            for child, parents in ((s2, [s1]), (s3, [s1, s2]), (s4, [s3])):
                for p in parents:
                    s.add(m.StepDependency(step_id=child.id, depends_on_step_id=p.id))
            out["steps"] = {"S1": str(s1.id), "S2": str(s2.id), "S3": str(s3.id), "S4": str(s4.id)}

            a1 = m.StepAttempt(
                step_id=s3.id,
                job_id=main_id,
                attempt_no=1,
                kind="implement",
                status="failed",
                worker_id="coder-222",
                model_alias=coder.alias,
                turns_used=7,
                outcome="tests_failed",
                error_code="TESTS_FAILED",
                summary="Endpoint geändert, 2 Tests rot",
                started_at=t(29),
                finished_at=t(24),
            )
            a2 = m.StepAttempt(
                step_id=s3.id,
                job_id=main_id,
                attempt_no=2,
                kind="correction",
                status="running",
                worker_id="coder-222",
                model_alias=coder.alias,
                turns_used=3,
                summary="Korrektur nach Testfehlern",
                started_at=t(18),
            )
            s.add_all([a1, a2])
            await s.flush()
            s.add_all(
                [
                    m.ScopeContractRow(
                        job_id=main_id,
                        step_id=s3.id,
                        version=1,
                        status="superseded",
                        contract={
                            "source": "planner_and_repo_intelligence",
                            "target_paths": ["src/api.py"],
                            "allowed_new_paths": [],
                            "forbidden_paths": [".git/**"],
                            "allowed_operations": ["modify"],
                        },
                        reason="Planer-Scope",
                    ),
                    m.ScopeContractRow(
                        job_id=main_id,
                        step_id=s3.id,
                        version=2,
                        status="active",
                        contract={
                            "source": "runtime_expansion",
                            "target_paths": ["src/api.py"],
                            "allowed_new_paths": ["tests/test_api.py"],
                            "forbidden_paths": [".git/**"],
                            "allowed_operations": ["create", "modify"],
                        },
                        reason="Scope erweitert: Testdatei anlegen",
                    ),
                    m.ToolCallRow(
                        job_id=main_id,
                        step_id=s3.id,
                        attempt_id=a1.id,
                        turn=1,
                        tool="read_file",
                        arguments={"path": "src/api.py"},
                        status="succeeded",
                        result_summary="42 Zeilen gelesen",
                        duration_ms=12,
                    ),
                    m.ToolCallRow(
                        job_id=main_id,
                        step_id=s3.id,
                        attempt_id=a1.id,
                        turn=2,
                        tool="apply_patch",
                        arguments={"path": "src/api.py"},
                        status="succeeded",
                        result_summary="1 Datei geändert",
                        duration_ms=40,
                    ),
                    m.CommandRun(
                        job_id=main_id,
                        step_id=s3.id,
                        attempt_id=a1.id,
                        worker_id="coder-222",
                        target="sandbox",
                        command="pytest -q tests/test_api.py",
                        classification="test",
                        cwd="/workspace",
                        exit_code=1,
                        stdout_excerpt="10 passed, 2 failed",
                        stderr_excerpt="AssertionError: version missing",
                        duration_ms=5400,
                    ),
                    m.TestRun(
                        job_id=main_id,
                        step_id=s3.id,
                        attempt_id=a1.id,
                        command="pytest -q tests/test_api.py",
                        framework="pytest",
                        status="failed",
                        passed=10,
                        failed=2,
                        errors=0,
                        skipped=1,
                        output_excerpt="FAILED tests/test_api.py::test_health_version",
                        duration_ms=5400,
                        created_at=t(25),
                    ),
                    m.TestRun(
                        job_id=main_id,
                        step_id=s3.id,
                        attempt_id=a2.id,
                        command="pytest -q tests/test_api.py",
                        framework="pytest",
                        status="passed",
                        passed=12,
                        failed=0,
                        errors=0,
                        skipped=1,
                        duration_ms=5100,
                        created_at=t(10),
                    ),
                ]
            )
            vr = m.VerificationRun(
                job_id=main_id,
                step_id=s3.id,
                attempt_id=a2.id,
                passed=False,
                status="completed",
                summary="1 blockierender Check fehlgeschlagen",
                changed_files=["src/api.py", "tests/test_api.py"],
                finished_at=t(8),
            )
            rr = m.ReviewRun(
                job_id=main_id,
                step_id=s3.id,
                attempt_id=a2.id,
                model_alias="heavy-review",
                status="completed",
                verdict="fix_required",
                raw_verdict="pass",
                invariant_override=True,
                summary="Fehlerbehandlung fehlt",
                finished_at=t(6),
            )
            s.add_all([vr, rr])
            await s.flush()
            s.add_all(
                [
                    m.VerificationCheckRow(
                        verification_run_id=vr.id, check_type="scope", name="Scope eingehalten", status="pass", blocking=True
                    ),
                    m.VerificationCheckRow(verification_run_id=vr.id, check_type="lint", name="ruff", status="pass", blocking=False),
                    m.VerificationCheckRow(
                        verification_run_id=vr.id,
                        check_type="unit",
                        name="pytest tests/test_api.py",
                        status="fail",
                        blocking=True,
                        message="test_health_version fehlgeschlagen",
                        evidence={"exit_code": 1},
                    ),
                    m.VerificationCheckRow(
                        verification_run_id=vr.id, check_type="secrets", name="Keine Secrets im Diff", status="pass", blocking=True
                    ),
                    m.ReviewFindingRow(
                        review_run_id=rr.id,
                        severity="major",
                        path="src/api.py",
                        summary="Fehlende Fehlerbehandlung im Health-Endpoint",
                        evidence="health() fängt Datenbankfehler nicht ab",
                        suggested_fix="try/except um den DB-Check und 503 zurückgeben",
                    ),
                    m.ReviewFindingRow(review_run_id=rr.id, severity="minor", path="tests/test_api.py", summary="Testname unpräzise"),
                ]
            )

            # ------------------------------------------------------------------ research
            run = m.ResearchRun(
                job_id=main_id,
                step_id=s2.id,
                question="Wie streamt FastAPI Server-Sent Events?",
                status="completed",
                queries=["fastapi server sent events", "EventSourceResponse"],
                synthesis="FastAPI liefert SSE über EventSourceResponse; Heartbeats als Kommentare.",
                contradictions=[{"group": 1, "note": "Uneinig über Standard-Retry"}],
                model_alias="fast-router",
                finished_at=t(30),
            )
            s.add(run)
            await s.flush()
            src_official = m.ResearchSource(
                research_run_id=run.id,
                title="FastAPI – Server-Sent Events",
                url="https://fastapi.tiangolo.com/advanced/server-sent-events/",
                domain="fastapi.tiangolo.com",
                source_type="official_docs",
                authority_score=0.95,
                relevance_score=0.9,
                content_hash="a" * 64,
                excerpt="EventSourceResponse streams events …",
                status="read",
            )
            src_mdn = m.ResearchSource(
                research_run_id=run.id,
                title="MDN – Using server-sent events",
                url="https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events",
                domain="developer.mozilla.org",
                source_type="reference",
                authority_score=0.85,
                relevance_score=0.8,
                content_hash="b" * 64,
                status="read",
            )
            src_bad = m.ResearchSource(
                research_run_id=run.id,
                title="Untrusted forum post",
                url="javascript:alert(1)",
                domain="forum.example",
                source_type="forum",
                authority_score=0.2,
                relevance_score=0.4,
                content_hash="c" * 64,
                status="refused",
                error="Nicht-HTTP-URL verworfen",
            )
            s.add_all([src_official, src_mdn, src_bad])
            await s.flush()
            c1 = m.ResearchClaim(
                research_run_id=run.id,
                claim="EventSource sendet beim Reconnect den Header Last-Event-ID.",
                confidence=0.9,
                used_for_decision=True,
                decision_ref="DECISION-SSE",
            )
            c2 = m.ResearchClaim(
                research_run_id=run.id, claim="Der Standard-Retry beträgt 3 Sekunden.", confidence=0.6, contradiction_group=1
            )
            c3 = m.ResearchClaim(
                research_run_id=run.id, claim="Browser wählen den Retry-Wert selbst.", confidence=0.5, contradiction_group=1
            )
            s.add_all([c1, c2, c3])
            await s.flush()
            for claim, srcs in ((c1, [src_official, src_mdn]), (c2, [src_official]), (c3, [src_mdn])):
                for src in srcs:
                    s.add(m.ResearchClaimSource(claim_id=claim.id, source_id=src.id))

            # ------------------------------------------------------------------ artifacts (files under HERMCLAW_DATA_DIR/artifacts)
            for kind, name, media, content, step_id in (
                ("diff", "changes.diff", "text/x-diff", DIFF_TEXT.encode(), s3.id),
                ("image", "screenshot.png", "image/png", _png(), s3.id),
                ("log", "pytest.log", "text/plain", b"10 passed, 2 failed\n", s3.id),
            ):
                path, size, sha = _write_artifact(data_dir, main_id, name, content)
                s.add(
                    m.Artifact(
                        job_id=main_id, step_id=step_id, kind=kind, name=name, path=path, media_type=media, size_bytes=size, sha256=sha
                    )
                )

            # ------------------------------------------------------------------ failed job (retry control)
            failed = m.Job(
                title="Fehlgeschlagener Migrationsjob",
                prompt="Migriere die Datenbank auf Schema v2",
                status="failed",
                priority=40,
                error_code="VERIFY_FAILED",
                error_message="Verifier: 2 blockierende Checks fehlgeschlagen",
                started_at=t(90),
                finished_at=t(80),
                created_at=t(95),
                metadata_={"allow_network_research": False, "auto_commit": True},
            )
            s.add(failed)
            await s.flush()
            out["jobs"]["failed"] = str(failed.id)

            # ------------------------------------------------------------------ workers, health, capabilities
            for wid, host, addr, kind, wstate, active in (
                ("coder-222", "hermclaw-222", "192.168.178.222", "execution", "busy", main_id),
                ("model-224", "hermclaw-224", "192.168.178.224", "model", "ready", None),
                ("media-224", "hermclaw-224-media", "192.168.178.224", "media", "sleeping", None),
                ("spare-225", "hermclaw-225", "192.168.178.225", "execution", "offline", None),
            ):
                s.add(
                    m.Worker(
                        id=wid,
                        hostname=host,
                        address=addr,
                        kind=kind,
                        state=wstate,
                        worker_version="0.1.0",
                        protocol_version=1,
                        last_heartbeat_at=t(0.2) if wstate in ("busy", "ready") else t(120),
                        active_job_id=str(active) if active else None,
                        active_step_id=str(s3.id) if active else None,
                    )
                )
            await s.flush()
            for wid, cap in (
                ("coder-222", "code.python"),
                ("coder-222", "test.pytest"),
                ("model-224", "llm.chat"),
                ("media-224", "media.image"),
            ):
                s.add(m.WorkerCapability(worker_id=wid, capability=cap))
            s.add(
                m.WorkerHealth(
                    worker_id="model-224",
                    state="ready",
                    cpu_percent=23.5,
                    ram_total_mb=65536,
                    ram_used_mb=30210,
                    disk_free_mb=512000,
                    gpus=[{"name": "RTX 3090", "memory_total_mb": 24576, "memory_used_mb": 19456, "utilization_percent": 71}],
                    loaded_models=[{"name": "gemma4:26b", "size_vram_mb": 19000}],
                    uptime_seconds=86400,
                )
            )

            # ------------------------------------------------------------------ resources
            s.add(
                m.ResourceLease(
                    resource=coder.resource_group,
                    resource_group=coder.resource_group,
                    owner_job_id=main_id,
                    owner_step_id=s3.id,
                    owner_kind="coder",
                    holder="runtime-e2e",
                    priority=coder.priority,
                    state="active",
                    preemptible=True,
                    exclusive=coder.exclusive,
                    weight=coder.memory_gb,
                    expires_at=now + timedelta(minutes=10),
                )
            )
            s.add(
                m.ResourceRequest(
                    resource="gpu-224",
                    owner_job_id=queued_id,
                    owner_kind="media",
                    holder="runtime-e2e",
                    priority=10,
                    state="waiting",
                    expires_at=now + timedelta(minutes=30),
                )
            )

            # ------------------------------------------------------------------ bugs + model stats
            s.add_all(
                [
                    m.BugRecord(
                        bug_key="BUG-UI-E2E-1",
                        title="SSE-Verbindung hinter Proxy gepuffert",
                        severity="P1",
                        component="api",
                        phase="P31",
                        status="open",
                        blocking=True,
                        reproduction="Nginx ohne X-Accel-Buffering",
                        expected="Events sofort",
                        actual="Events im Block",
                    ),
                    m.BugRecord(bug_key="BUG-UI-E2E-2", title="Tippfehler im Dashboard", severity="P3", component="ui", status="fixed"),
                ]
            )
            for alias, status, latency in (
                ("planner-gemma", "succeeded", 4200),
                ("planner-gemma", "succeeded", 3800),
                (coder.alias, "error", 9000),
            ):
                s.add(
                    m.ModelInvocation(
                        job_id=main_id,
                        alias=alias,
                        model="seeded",
                        role="planner" if alias == "planner-gemma" else "coder",
                        purpose="plan" if alias == "planner-gemma" else "implement",
                        status=status,
                        prompt_tokens=1000,
                        completion_tokens=400,
                        latency_ms=latency,
                    )
                )

            # ------------------------------------------------------------------ events (status lines + typed events)
            ev = EventType

            async def event(
                etype: str, *, job: uuid.UUID = main_id, step: uuid.UUID | None = None, severity: str = "info", **payload: Any
            ) -> None:
                await append_event(s, etype, source_type="runtime", job_id=job, step_id=step, severity=severity, payload=payload)

            await append_event(
                s,
                ev.JOB_TRANSITION,
                source_type="runtime",
                job_id=main_id,
                payload={"from": "queued", "to": "inventory", "reason": "Scheduler"},
            )
            await emit_status(s, main_id, "Repository wird inventarisiert", step_id=s1.id)
            await event(ev.REPO_INVENTORY_FINISHED, step=s1.id, files=42, languages=["python"])
            await emit_status(s, main_id, "Gemma erstellt Plan")
            await event(ev.PLANNER_PLAN_CREATED, version=1, steps=3, model_alias="planner-gemma")
            await emit_status(s, main_id, "Research liest offizielle Dokumentation", step_id=s2.id)
            await event(ev.RESEARCH_SOURCE_READ, step=s2.id, url="https://fastapi.tiangolo.com/advanced/server-sent-events/")
            await event(ev.RESOURCE_ACQUIRED, resource=coder.resource_group, owner_kind="coder")
            await emit_status(s, main_id, "Coder bearbeitet src/api.py", step_id=s3.id)
            await event(ev.FILE_CHANGED, step=s3.id, path="src/api.py", operation="modify")
            await event(ev.TEST_FAILED, step=s3.id, severity="warning", command="pytest -q tests/test_api.py", failed=2)
            await emit_status(s, main_id, "Test fehlgeschlagen", step_id=s3.id, severity=Severity.warning)
            await event(ev.REPLAN_CREATED, version=2, reason="Test fehlgeschlagen")
            await emit_status(s, main_id, "Coder korrigiert", step_id=s3.id)
            await event(ev.TEST_PASSED, step=s3.id, command="pytest -q tests/test_api.py", passed=12)
            await emit_status(s, main_id, "Verifier prüft 4 Checks", step_id=s3.id)
            await event(
                ev.VERIFIER_CHECK_FAILED, step=s3.id, severity="error", name="pytest tests/test_api.py", message="test_health_version"
            )
            await emit_status(s, main_id, "Reviewer meldet Major Finding", step_id=s3.id)
            await event(ev.REVIEW_FINDING_CREATED, step=s3.id, severity_level="major", summary="Fehlende Fehlerbehandlung")
            await event(ev.GIT_OPERATION, operation="branch.create", branch="hermclaw/job-main")
            await event(ev.CHECKPOINT_CREATED, step=s3.id, commit="0f1e2d3c4b5a69788796a5b4c3d2e1f0aabbccdd")
            await event(
                ev.GIT_COMMIT_CREATED,
                sha="9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b",
                message="feat: health endpoint mit Version",
                branch="hermclaw/job-main",
            )
            await event(ev.MEDIA_FINISHED, step=s3.id, artifact="screenshot.png", media_type="image/png")
            await event(ev.ERROR, severity="error", message="LiteLLM Timeout beim Review (wird wiederholt)")
            # a mutating SSH command waiting for the operator (P26 approvals tab)
            from hermclaw.sshtools.policy import approval_token

            ssh_cmd = "sudo /usr/bin/systemctl reload nginx"
            out["ssh_approval"] = approval_token("webui-223", ssh_cmd, s3.id)
            await event(
                ev.SSH_COMMAND,
                step=s3.id,
                severity="warning",
                tool="ssh_command",
                host="webui-223",
                outcome="approval_required",
                command=ssh_cmd,
                classification="mutate",
                approval=out["ssh_approval"],
            )
            await event(
                ev.JOB_FAILED,
                job=failed.id,
                severity="error",
                error_code="VERIFY_FAILED",
                message="Verifier: 2 blockierende Checks fehlgeschlagen",
            )
            await emit_status(s, failed.id, "Job fehlgeschlagen: Verifier")
            await s.commit()
    finally:
        await dispose_engine()
    return out


def seed(state_path: Path) -> dict[str, Any]:
    state = _load_state(state_path)
    _apply_env(state)
    api = str(state["api_url"])
    token = str(state["token"])
    ids = {
        "main": _api_create_job(
            api,
            token,
            {
                "prompt": "Erweitere den Health-Endpoint um die Version und ergänze Tests.",
                "title": "Health-Endpoint erweitern",
                "repository": {"name": "demo-repo", "url": "file:///srv/git/demo-repo.git", "base_branch": "main"},
                "constraints": ["Keine neuen Abhängigkeiten"],
                "priority": 70,
                "allow_network_research": True,
                "auto_commit": True,
            },
        ),
        "queued": _api_create_job(
            api, token, {"prompt": "Dokumentation der API aktualisieren", "title": "Doku-Job in Warteschlange", "priority": 30}
        ),
        "pausable": _api_create_job(api, token, {"prompt": "Refactoring des Loggings", "title": "Pausierbarer Job", "priority": 55}),
    }
    out = asyncio.run(_seed_db(state, ids))
    state.update(out)
    # mark the pausable job as running (pause/resume/replan are tested on it)
    asyncio.run(_set_status(state, ids["pausable"], "running"))
    _write_state(state_path, state)
    return state


async def _set_status(state: dict[str, Any], job_id: str, status: str) -> None:
    from hermclaw.persistence import models as m
    from hermclaw.persistence.db import dispose_engine, get_sessionmaker, init_engine

    init_engine(str(state["database_url"]))
    try:
        async with get_sessionmaker()() as s:
            job = await s.get(m.Job, uuid.UUID(job_id))
            assert job is not None
            job.status = status
            await s.commit()
    finally:
        await dispose_engine()


async def _make_job(state: dict[str, Any], title: str, status: str) -> str:
    """Create a job directly in a given status (control tests need fresh jobs per test)."""
    from hermclaw.persistence import models as m
    from hermclaw.persistence.db import dispose_engine, get_sessionmaker, init_engine

    init_engine(str(state["database_url"]))
    try:
        async with get_sessionmaker()() as s:
            job = m.Job(title=title, prompt=f"{title} (E2E)", status=status, priority=50, metadata_={})
            if status == "failed":
                job.error_code = "E2E_FAILED"
                job.error_message = "absichtlich fehlgeschlagen (E2E)"
            s.add(job)
            await s.commit()
            return str(job.id)
    finally:
        await dispose_engine()


async def _emit(state: dict[str, Any], job_id: str, text: str, etype: str) -> int:
    from hermclaw.events.store import append_event
    from hermclaw.persistence.db import dispose_engine, get_sessionmaker, init_engine

    init_engine(str(state["database_url"]))
    try:
        async with get_sessionmaker()() as s:
            payload: dict[str, Any] = {"text": text} if etype == "status" else {"message": text}
            severity = "error" if etype == "error" else "info"
            row = await append_event(s, etype, source_type="runtime", job_id=uuid.UUID(job_id), severity=severity, payload=payload)
            await s.commit()
            return int(row.sequence)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["prepare", "seed", "emit", "job", "status", "teardown"])
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--admin-url", default=os.environ.get("HERMCLAW_TEST_ADMIN_URL", ADMIN_URL_DEFAULT))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--api-url")
    parser.add_argument("--job")
    parser.add_argument("--text", default="")
    parser.add_argument("--type", default="status")
    parser.add_argument("--title", default="E2E-Job")
    parser.add_argument("--status", default="queued")
    args = parser.parse_args(argv)
    state_path: Path = args.state.resolve()
    if args.command == "prepare":
        data_dir = (args.data_dir or state_path.parent / "data").resolve()
        prepare(state_path, args.admin_url, data_dir)
    elif args.command == "seed":
        if args.api_url:
            st = _load_state(state_path)
            st["api_url"] = args.api_url.rstrip("/")
            _write_state(state_path, st)
        seed(state_path)
    elif args.command == "emit":
        if not args.job:
            parser.error("--job is required for emit")
        seq = asyncio.run(_emit(_load_state(state_path), args.job, args.text, args.type))
        print(json.dumps({"sequence": seq}))
    elif args.command == "job":
        job_id = asyncio.run(_make_job(_load_state(state_path), args.title, args.status))
        print(json.dumps({"id": job_id}))
    elif args.command == "status":
        if not args.job:
            parser.error("--job is required for status")
        asyncio.run(_set_status(_load_state(state_path), args.job, args.status))
        print(json.dumps({"id": args.job, "status": args.status}))
    else:
        teardown(state_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
