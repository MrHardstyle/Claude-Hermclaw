"""Local soak test (P40): real processes, continuous jobs, failures in between, integrity checks at the end.

Processes: ``hermclaw api`` (uvicorn), the scheduler (scripts/soak/soak_scheduler.py: real SchedulerProcess with
scripted model answers), a real execution worker daemon (``python -m worker.execution``) heartbeating to the API.
Jobs are submitted through the HTTP API (admin token) against local file:// repositories.

During the run: the execution worker is SIGKILLed and restarted (40.7 reconnect), the scheduler is restarted
gracefully (SIGTERM) once. Every ``--sample-seconds`` the RSS of each process, job/step/lease counts and the event
sequence are sampled. At the end: all jobs terminal, no stuck leases (40.6), event order (40.8), DB integrity
(40.9), RSS trend per process (40.5). Writes a JSON report.

Usage (from the repository root, PostgreSQL reachable via HERMCLAW_TEST_ADMIN_URL):
    .venv/bin/python -m scripts.soak.run_soak --minutes 60 --out docs/operations/soak-local.json
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get("HERMCLAW_TEST_ADMIN_URL", "postgresql://postgres@127.0.0.1:5432/postgres")
TERMINAL = ("succeeded", "failed", "cancelled")
VALUE_SEED = {"src/module.py": "VALUE = 1\n", "README.md": "# demo\n"}
FASTAPI_SEED = {
    "app/__init__.py": "",
    "app/main.py": (
        'from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get("/items")\ndef items() -> list[str]:\n    return ["a", "b"]\n'
    ),
    "tests/__init__.py": "",
    "tests/test_items.py": (
        "from fastapi.testclient import TestClient\n\nfrom app.main import app\n\n\n"
        'def test_items() -> None:\n    assert TestClient(app).get("/items").json() == ["a", "b"]\n'
    ),
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def make_upstream(root: Path, files: dict[str, str]) -> Path:
    seed = root / "seed"
    seed.mkdir(parents=True)
    git(seed, "init", "-q", "-b", "main")
    for rel, content in files.items():
        p = seed / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "-c", "user.email=soak@hermclaw", "-c", "user.name=soak", "commit", "-q", "-m", "initial")
    bare = root / f"{root.name}.git"  # unique basename per repository (see BUG-043)
    git(root, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


def rss_mb(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


@dataclass
class Proc:
    name: str
    argv: list[str]
    env: dict[str, str]
    log: Path
    p: subprocess.Popen[bytes] | None = None
    restarts: int = 0
    rss: list[tuple[float, float]] = field(default_factory=list)

    def start(self) -> None:
        fh = self.log.open("ab")
        self.p = subprocess.Popen(self.argv, cwd=ROOT, env=self.env, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def stop(self, sig: int = signal.SIGTERM, timeout: float = 60) -> int | None:
        if self.p is None:
            return None
        with suppress(ProcessLookupError):
            self.p.send_signal(sig)
        try:
            return self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            return self.p.wait()


class Soak:
    def __init__(self, ns: argparse.Namespace) -> None:
        self.ns = ns
        self.work = Path(ns.work)
        if self.work.exists():
            shutil.rmtree(self.work)
        self.work.mkdir(parents=True)
        self.db = f"hermclaw_soak_{uuid.uuid4().hex[:8]}"
        base = ADMIN_URL.rsplit("/", 1)[0]
        self.db_url = f"{base.replace('postgresql://', 'postgresql+psycopg://', 1)}/{self.db}"
        self.pg_url = f"{base}/{self.db}"
        self.api_port, self.worker_port = free_port(), free_port()
        self.admin_token = secrets.token_urlsafe(32)
        self.worker_token = secrets.token_urlsafe(32)
        self.worker_id = "exec-soak"
        self.samples: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.jobs: list[str] = []
        self.started = time.monotonic()

    # ------------------------------------------------------------------------------------- setup
    def setup_db(self) -> None:
        with psycopg.connect(ADMIN_URL, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{self.db}"')
        env = {**os.environ, "HERMCLAW_DATABASE_URL": self.db_url}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True)

    def base_env(self) -> dict[str, str]:
        token_file = self.work / "worker-token"
        token_file.write_text(self.worker_token + "\n")
        token_file.chmod(0o600)
        return {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "HERMCLAW_ENV": "test",
            "HERMCLAW_DATABASE_URL": self.db_url,
            "HERMCLAW_DATA_DIR": str(self.work / "data"),
            "HERMCLAW_API_HOST": "127.0.0.1",
            "HERMCLAW_API_PORT": str(self.api_port),
            "HERMCLAW_API_TOKEN_REF": f"literal:{self.admin_token}",
            "HERMCLAW_WORKER_TOKEN_REFS": json.dumps({self.worker_id: f"file:{token_file}"}),
            "HERMCLAW_LOG_JSON": "true",
        }

    def processes(self) -> list[Proc]:
        env = self.base_env()
        logs = self.work / "logs"
        logs.mkdir()
        py = sys.executable
        self.api = Proc("api", [py, "-m", "hermclaw.cli", "api"], env, logs / "api.log")
        self.scheduler = Proc(
            "scheduler",
            [
                py,
                "-m",
                "scripts.soak.soak_scheduler",
                "--data-dir",
                str(self.work / "data"),
                "--instance",
                "soak-225",
                "--concurrency",
                "3",
            ],
            env,
            logs / "scheduler.log",
        )
        wenv = {
            **env,
            "WORKER_ID": self.worker_id,
            "WORKER_KIND": "execution",
            "WORKER_TOKEN_FILE": str(self.work / "worker-token"),
            "ORCHESTRATOR_URL": f"http://127.0.0.1:{self.api_port}",
            "WORKER_BIND": f"127.0.0.1:{self.worker_port}",
            "WORKER_HEARTBEAT_SECONDS": "5",
            "WORKER_DATA_DIR": str(self.work / "worker"),
            "WORKER_CAPABILITIES": "coding,testing",
        }
        self.worker = Proc("exec-worker", [py, "-m", "worker.execution"], wenv, logs / "worker.log")
        return [self.api, self.scheduler, self.worker]

    # ------------------------------------------------------------------------------------- helpers
    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"http://127.0.0.1:{self.api_port}", headers={"Authorization": f"Bearer {self.admin_token}"}, timeout=30
        )

    def note(self, what: str, **data: Any) -> None:
        entry = {"t": round(time.monotonic() - self.started, 1), "event": what, **data}
        self.events.append(entry)
        print(json.dumps(entry), flush=True)

    def wait_api(self, timeout: float = 60) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with suppress(httpx.HTTPError):
                if httpx.get(f"http://127.0.0.1:{self.api_port}/api/health", timeout=2).status_code == 200:
                    return
            time.sleep(0.5)
        raise RuntimeError("API did not come up")

    def worker_state(self) -> str | None:
        with self.client() as c:
            r = c.get(f"/api/workers/{self.worker_id}")
        return r.json().get("state") if r.status_code == 200 else None

    def submit(self, n: int) -> None:
        kind = "fastapi" if n % 3 == 2 else "value"
        upstream = make_upstream(self.work / "repos" / f"r{n:04d}", FASTAPI_SEED if kind == "fastapi" else VALUE_SEED)
        prompt = "Add GET /health with a test." if kind == "fastapi" else "Change VALUE in src/module.py to 2."
        body = {"title": f"{kind}: soak job {n}", "prompt": prompt, "repository": {"url": f"file://{upstream}", "base_branch": "main"}}
        with self.client() as c:
            r = c.post("/api/jobs", json=body)
        r.raise_for_status()
        self.jobs.append(r.json()["id"])

    def sample(self) -> None:
        with psycopg.connect(self.pg_url) as c:
            jobs = dict(c.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
            leases = c.execute("SELECT count(*) FROM resource_leases WHERE state IN ('active','preempting')").fetchone()[0]
            inflight = c.execute(
                "SELECT count(*) FROM steps WHERE status IN ('leased','running','verifying','reviewing','committing')"
            ).fetchone()[0]
            events = c.execute("SELECT count(*), max(sequence) FROM events").fetchone()
        s = {
            "t": round(time.monotonic() - self.started, 1),
            "jobs": jobs,
            "active_leases": leases,
            "steps_in_flight": inflight,
            "events": events[0],
        }
        for proc in (self.api, self.scheduler, self.worker):
            if proc.alive() and proc.p is not None:
                mb = rss_mb(proc.p.pid)
                if mb is not None:
                    proc.rss.append((s["t"], mb))
                    s[f"rss_{proc.name}"] = mb
        self.samples.append(s)
        print(json.dumps(s), flush=True)

    # ------------------------------------------------------------------------------------- checks
    def final_checks(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with psycopg.connect(self.pg_url) as c:
            out["jobs_by_status"] = dict(c.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
            out["stuck_leases"] = c.execute("SELECT count(*) FROM resource_leases WHERE state IN ('active','preempting')").fetchone()[0]
            out["open_requests"] = c.execute("SELECT count(*) FROM resource_requests WHERE state = 'waiting'").fetchone()[0]
            out["inflight_steps_of_terminal_jobs"] = c.execute(
                "SELECT count(*) FROM steps s JOIN jobs j ON j.id = s.job_id WHERE j.status IN ('succeeded','failed','cancelled') "
                "AND s.superseded = false AND s.status IN ('leased','running','verifying','reviewing','committing','ready','pending')"
            ).fetchone()[0]
            out["succeeded_without_report"] = c.execute(
                "SELECT count(*) FROM jobs j WHERE j.status = 'succeeded' AND NOT EXISTS "
                "(SELECT 1 FROM artifacts a WHERE a.job_id = j.id AND a.kind = 'report')"
            ).fetchone()[0]
            out["failed_without_error_code"] = c.execute(
                "SELECT count(*) FROM jobs WHERE status = 'failed' AND error_code IS NULL"
            ).fetchone()[0]
            out["open_attempts_of_terminal_jobs"] = c.execute(
                "SELECT count(*) FROM step_attempts a JOIN jobs j ON j.id = a.job_id "
                "WHERE j.status IN ('succeeded','failed','cancelled') AND a.finished_at IS NULL"
            ).fetchone()[0]
            # 40.8 event ordering: per job, sequence order == insertion order (timestamps never go backwards by more than 1 s)
            out["event_order_violations"] = c.execute(
                "SELECT count(*) FROM (SELECT job_id, ts, lag(ts) OVER (PARTITION BY job_id ORDER BY sequence) AS prev "
                "FROM events WHERE job_id IS NOT NULL) x WHERE prev IS NOT NULL AND ts < prev - interval '1 second'"
            ).fetchone()[0]
            out["duplicate_sequences"] = c.execute("SELECT count(*) - count(DISTINCT sequence) FROM events").fetchone()[0]
            # 40.9 DB integrity: every FK/check constraint validated, no orphans in FK-less links
            out["unvalidated_constraints"] = c.execute("SELECT count(*) FROM pg_constraint WHERE NOT convalidated").fetchone()[0]
            out["orphan_step_dependencies"] = c.execute(
                "SELECT count(*) FROM step_dependencies d LEFT JOIN steps s ON s.id = d.depends_on_step_id WHERE s.id IS NULL"
            ).fetchone()[0]
            out["events_total"] = c.execute("SELECT count(*) FROM events").fetchone()[0]
        trend: dict[str, Any] = {}
        for proc in (self.api, self.scheduler, self.worker):
            pts = proc.rss
            if len(pts) >= 4:
                q = max(1, len(pts) // 4)
                first, last = sum(v for _, v in pts[:q]) / q, sum(v for _, v in pts[-q:]) / q
                trend[proc.name] = {
                    "rss_first_quarter_mb": round(first, 1),
                    "rss_last_quarter_mb": round(last, 1),
                    "growth_pct": round((last - first) / first * 100, 1),
                    "max_mb": max(v for _, v in pts),
                }
        out["rss_trend"] = trend
        return out

    # ------------------------------------------------------------------------------------- run
    def run(self) -> dict[str, Any]:
        self.setup_db()
        procs = self.processes()
        self.api.start()
        self.wait_api()
        self.worker.start()
        self.scheduler.start()
        self.note("started", api_port=self.api_port, worker_port=self.worker_port, db=self.db)
        duration = self.ns.minutes * 60
        interval = 60 / self.ns.jobs_per_minute
        next_job = next_sample = time.monotonic()
        crash_at, restart_sched_at = self.started + duration * 0.4, self.started + duration * 0.7
        crashed = sched_restarted = False
        reconnect: dict[str, Any] = {}
        n = 0
        try:
            while time.monotonic() - self.started < duration:
                now = time.monotonic()
                if now >= next_job:
                    self.submit(n)
                    n += 1
                    next_job = now + interval
                if now >= next_sample:
                    self.sample()
                    next_sample = now + self.ns.sample_seconds
                if not crashed and now >= crash_at:
                    crashed = True
                    self.note("worker SIGKILL", state_before=self.worker_state())
                    self.worker.stop(signal.SIGKILL)
                    t0 = time.monotonic()
                    while time.monotonic() - t0 < 120 and self.worker_state() not in ("offline",):
                        time.sleep(2)
                    reconnect["offline_detected_s"] = round(time.monotonic() - t0, 1)
                    reconnect["state_after_kill"] = self.worker_state()
                    self.worker.start()
                    self.worker.restarts += 1
                    t1 = time.monotonic()
                    while time.monotonic() - t1 < 120 and self.worker_state() not in ("ready", "busy"):
                        time.sleep(1)
                    reconnect["ready_again_s"] = round(time.monotonic() - t1, 1)
                    reconnect["state_after_restart"] = self.worker_state()
                    self.note("worker restarted", **reconnect)
                if not sched_restarted and now >= restart_sched_at:
                    sched_restarted = True
                    rc = self.scheduler.stop(signal.SIGTERM, timeout=120)
                    self.note("scheduler graceful restart", exit_code=rc)
                    self.scheduler.start()
                    self.scheduler.restarts += 1
                for proc in procs:
                    if not proc.alive() and not (proc is self.worker and crashed and not reconnect.get("state_after_restart")):
                        self.note("process died", name=proc.name, rc=proc.p.returncode if proc.p else None)
                        proc.start()
                        proc.restarts += 1
                time.sleep(0.5)
            # drain: wait until all submitted jobs are terminal
            self.note("draining", jobs=len(self.jobs))
            t0 = time.monotonic()
            while time.monotonic() - t0 < self.ns.drain_minutes * 60:
                with psycopg.connect(self.pg_url) as c:
                    open_jobs = c.execute("SELECT count(*) FROM jobs WHERE status NOT IN ('succeeded','failed','cancelled')").fetchone()[0]
                if open_jobs == 0:
                    break
                time.sleep(2)
            self.sample()
            final = self.final_checks()
        finally:
            for proc in (self.scheduler, self.worker, self.api):
                proc.stop(signal.SIGTERM)
        report = {
            "started_at": datetime.now(UTC).isoformat(),
            "minutes": self.ns.minutes,
            "jobs_submitted": len(self.jobs),
            "restarts": {p.name: p.restarts for p in procs},
            "worker_reconnect": reconnect,
            "final": final,
            "samples": self.samples,
            "events": self.events,
        }
        report["verdict"] = verdict(report)
        return report


def verdict(r: dict[str, Any]) -> dict[str, Any]:
    f = r["final"]
    checks = {
        "all_jobs_terminal": sum(f["jobs_by_status"].get(s, 0) for s in TERMINAL) == r["jobs_submitted"],
        "all_jobs_succeeded": f["jobs_by_status"].get("succeeded", 0) == r["jobs_submitted"],
        "no_stuck_leases": f["stuck_leases"] == 0 and f["open_requests"] == 0,
        "no_inflight_steps_of_terminal_jobs": f["inflight_steps_of_terminal_jobs"] == 0 and f["open_attempts_of_terminal_jobs"] == 0,
        "event_order": f["event_order_violations"] == 0 and f["duplicate_sequences"] == 0,
        "db_integrity": f["unvalidated_constraints"] == 0 and f["orphan_step_dependencies"] == 0,
        "reports_for_succeeded_jobs": f["succeeded_without_report"] == 0,
        "worker_reconnected": r["worker_reconnect"].get("state_after_restart") in ("ready", "busy"),
        "no_rss_leak": all(t["growth_pct"] < 50 for t in f["rss_trend"].values()),
    }
    return {"passed": all(checks.values()), "checks": checks}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--jobs-per-minute", type=float, default=2)
    p.add_argument("--sample-seconds", type=float, default=30)
    p.add_argument("--drain-minutes", type=float, default=10)
    p.add_argument("--work", default=str(Path(tempfile.gettempdir()) / "hermclaw-soak"))
    p.add_argument("--out", default="soak-report.json")
    ns = p.parse_args()
    report = Soak(ns).run()
    Path(ns.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["verdict"], indent=2))
    return 0 if report["verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
