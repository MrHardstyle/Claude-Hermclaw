"""P32.9 ``hermclaw doctor``: real PostgreSQL + data paths, HTTP endpoints (LiteLLM, worker daemons, SearXNG) mocked."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, text

from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION
from hermclaw.core.config import get_config, load_config
from hermclaw.core.settings import Settings
from hermclaw.doctor import MIN_PGVECTOR, Doctor, _version_tuple
from hermclaw.persistence.migrate import head_revision
from hermclaw.persistence.models import ApiToken, Worker
from hermclaw.workers.auth import StaticTokenStore

pytestmark = pytest.mark.asyncio(loop_scope="session")
ROOT = Path(__file__).resolve().parents[2]
TOKEN = "doctor-test-worker-token-0123456789abcdef"


def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    for sub in ("workspaces", "artifacts", "repos"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    return Settings(env="test", data_dir=data, disk_warn_free_mb=1, disk_critical_free_mb=1)


class Endpoints:
    """Mock LAN: LiteLLM, the two worker daemons and SearXNG."""

    def __init__(self, cfg: Any) -> None:
        self.aliases = [p.alias for p in cfg.models.profiles if p.enabled]
        self.installed = sorted({p.model for p in cfg.models.profiles if p.enabled})
        self.images_ok = True
        self.searx_status = 200
        self.litellm_up = True
        self.llama_served = ["qwen3.6:35b"]
        self.llama_status = 200

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.url.port == 18080:
            if path == "/health":
                return httpx.Response(self.llama_status, json={"status": "ok" if self.llama_status == 200 else "loading model"})
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": m} for m in self.llama_served]})
        if req.url.port == 4000:
            if not self.litellm_up:
                raise httpx.ConnectError("refused", request=req)
            if path == "/health/liveliness":
                return httpx.Response(200, text='"I\'m alive!"')
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": a} for a in self.aliases]})
        if req.url.port == 8888 and path == "/search":
            if self.searx_status != 200:
                return httpx.Response(self.searx_status, text="forbidden")
            return httpx.Response(200, json={"results": [{"url": "https://docs.python.org/3/"}]})
        if req.url.port == 8787:
            kind = "model" if req.url.host.endswith(".224") else "execution"
            if path == "/health":
                return httpx.Response(200, json={"status": "ok", "worker_id": "w", "kind": kind, "state": "ready", "worker_version": "x"})
            if path == "/v1/models":
                return httpx.Response(200, json={"loaded": [], "installed": self.installed})
            if path == "/v1/selftest":
                checks = [{"name": "sandbox_engine", "ok": True}, {"name": "sandbox_run", "ok": True}]
                checks.insert(1, {"name": "sandbox_images", "ok": self.images_ok, "detail": "" if self.images_ok else "missing: php"})
                return httpx.Response(200, json={"ok": self.images_ok, "worker_id": "exec-222", "checks": checks})
        return httpx.Response(404, json={"detail": f"unexpected {req.method} {req.url}"})


@pytest.fixture
async def db(sessionmaker: Any) -> Any:
    now = datetime.now(UTC)
    async with sessionmaker() as s:
        await s.execute(delete(Worker).where(Worker.id.in_(["exec-222", "model-224"])))
        await s.execute(delete(ApiToken).where(ApiToken.name.like("doctor-%")))
        for wid, kind, addr in (("exec-222", "execution", "192.168.178.222"), ("model-224", "model", "192.168.178.224")):
            s.add(
                Worker(
                    id=wid,
                    hostname=wid,
                    address=addr,
                    kind=kind,
                    state="ready",
                    api_url=f"http://{addr}:8787",
                    worker_version="0.1.0rc1",
                    protocol_version=WORKER_PROTOCOL_VERSION,
                    last_heartbeat_at=now,
                )
            )
        s.add(ApiToken(name=f"doctor-{uuid.uuid4().hex[:6]}", token_hash="x", scopes=["read", "control", "admin"]))
        await s.commit()
    yield sessionmaker
    async with sessionmaker() as s:
        await s.execute(delete(Worker).where(Worker.id.in_(["exec-222", "model-224"])))
        await s.commit()


def doctor(cfg: Any, st: Settings, sm: Any, ep: Endpoints, **kw: Any) -> Doctor:
    return Doctor(
        cfg,
        st,
        sm,
        transport=httpx.MockTransport(ep),
        token_store=StaticTokenStore({"exec-222": TOKEN, "model-224": TOKEN}),
        litellm_key=lambda: "sk-test",
        head_revision=head_revision,
        **kw,
    )


def by_name(report: Any) -> dict[str, Any]:
    return {c.name: c for c in report.checks}


async def test_healthy_installation_passes_every_check(db: Any, tmp_path: Path) -> None:
    cfg = get_config()
    report = await doctor(cfg, settings(tmp_path), db, Endpoints(cfg)).run()
    checks = by_name(report)
    async with db() as s:
        vector = (await s.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))).scalar_one()
    # the build host runs Ubuntu's pgvector 0.6; production requires PGDG ≥ 0.8.2 – doctor must say exactly that
    expected_fail = ["database:pgvector"] if _version_tuple(vector) < MIN_PGVECTOR else []
    assert [c.name for c in report.checks if c.status == "fail"] == expected_fail, report.render()
    if expected_fail:
        assert "CVE-2026-3172" in checks["database:pgvector"].detail
    checks["database:pgvector"].status = "ok"
    for name in ("config:roles", "config:hosts", "database", "paths:workspaces", "api_tokens", "litellm"):
        assert checks[name].status == "ok", (name, checks[name])
    assert checks["worker:exec-222"].status == "ok" and checks["worker:model-224"].status == "ok"
    assert checks["models:model-224"].status == "ok" and "qwen3-coder:30b" in checks["models:model-224"].detail
    assert checks["sandbox:exec-222"].status == "ok" and checks["research"].status == "ok"
    roles = checks["config:roles"].data["roles"]
    assert roles["planner"][0].endswith("gemma4:26b") and roles["coder"][0].endswith("qwen3-coder:30b")
    assert roles["heavy"][0].endswith("qwen3.8:27b") and roles["fast"][0].endswith("qwen3:8b")
    assert json.dumps(report.to_dict())


async def test_problems_fail_with_actionable_hints(db: Any, tmp_path: Path) -> None:
    cfg = get_config()
    ep = Endpoints(cfg)
    ep.installed = [m for m in ep.installed if m != "qwen3.8:27b"]
    ep.aliases = [a for a in ep.aliases if a != "heavy-review"]
    ep.images_ok = False
    ep.searx_status = 403
    async with db() as s:
        await s.execute(
            Worker.__table__.update()
            .where(Worker.id == "exec-222")
            .values(protocol_version=WORKER_PROTOCOL_VERSION + 1, last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await s.commit()
    report = await doctor(cfg, settings(tmp_path), db, ep).run()
    checks = by_name(report)
    assert not report.ok
    assert checks["models:model-224"].status == "fail" and "hermclaw-model-pull" in checks["models:model-224"].detail
    assert checks["models:model-224"].data["missing"] == ["qwen3.8:27b"]
    assert checks["litellm"].status == "fail" and "heavy-review" in checks["litellm"].detail
    assert checks["worker:exec-222"].status == "fail" and "protocol" in checks["worker:exec-222"].detail
    assert "sandbox:exec-222" not in checks, "an incompatible worker is not probed further"
    assert checks["research"].status == "fail" and "json" in checks["research"].detail


async def test_unreachable_gateway_missing_images_and_stale_schema(db: Any, tmp_path: Path) -> None:
    cfg = get_config()
    ep = Endpoints(cfg)
    ep.litellm_up = False
    ep.images_ok = False
    d = Doctor(
        cfg,
        settings(tmp_path),
        db,
        transport=httpx.MockTransport(ep),
        token_store=StaticTokenStore({"exec-222": TOKEN, "model-224": TOKEN}),
        litellm_key=lambda: None,
        head_revision=lambda: "9999",
    )
    report = await d.run()
    checks = by_name(report)
    assert checks["litellm"].status == "fail" and "hermclaw-litellm" in checks["litellm"].detail
    assert checks["sandbox:exec-222"].status == "fail" and "sandbox_images" in checks["sandbox:exec-222"].detail
    assert checks["database"].status == "fail" and "hermclaw migrate" in checks["database"].detail


async def test_offline_and_missing_paths(db: Any, tmp_path: Path) -> None:
    cfg = get_config()
    st = Settings(env="test", data_dir=tmp_path / "nowhere")
    report = await doctor(cfg, st, db, Endpoints(cfg), offline=True).run()
    checks = by_name(report)
    assert checks["network"].status == "skip"
    assert checks["paths:data"].status == "fail" and "missing" in checks["paths:data"].detail
    assert not any(n.startswith(("worker:", "litellm")) for n in checks)


async def test_cli_doctor_offline_json(db_url: str, tmp_path: Path) -> None:
    st = settings(tmp_path)
    env = {**os.environ, "HERMCLAW_DATABASE_URL": db_url, "HERMCLAW_DATA_DIR": str(st.data_dir), "HERMCLAW_ENV": "test"}
    proc = subprocess.run(
        [sys.executable, "-m", "hermclaw.cli", "doctor", "--offline", "--json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = json.loads(proc.stdout)
    names = {c["name"]: c["status"] for c in out["checks"]}
    assert names["database"] == "ok" and names["network"] == "skip"
    assert proc.returncode == (0 if out["ok"] else 1)


async def test_version_compare() -> None:
    assert _version_tuple("0.8.2") >= MIN_PGVECTOR and _version_tuple("0.10.0") >= MIN_PGVECTOR
    assert _version_tuple("0.8.1") < MIN_PGVECTOR and _version_tuple("0.6.0") < MIN_PGVECTOR


async def test_production_refuses_example_configuration_and_flags_placeholder_macs(db: Any, tmp_path: Path) -> None:
    """A production install must run on its own /etc/hermclaw/<name>.yaml files, not on the shipped examples; the
    example hosts file still carries placeholder MAC addresses, which would make Wake-on-LAN silently useless."""
    cfg = get_config()
    examples = tmp_path / "etc-examples"
    examples.mkdir()
    for f in (ROOT / "config").glob("*.example.yaml"):
        shutil.copy(f, examples / f.name)
    prod = settings(tmp_path).model_copy(update={"env": "production", "config_dir": examples})
    checks = by_name(await doctor(cfg, prod, db, Endpoints(cfg), offline=True).run())
    assert checks["config:files"].status == "fail" and "hosts.yaml" in checks["config:files"].detail
    assert checks["config:wake_on_lan"].status == "warn" and "exec-222" in checks["config:wake_on_lan"].detail
    real = tmp_path / "etc"
    real.mkdir()
    for f in (ROOT / "config").glob("*.example.yaml"):
        shutil.copy(f, real / f.name.replace(".example", ""))
    hosts = real / "hosts.yaml"
    hosts.write_text(hosts.read_text(encoding="utf-8").replace("00:00:00:00:02:22", "3c:7c:3f:12:34:56"), encoding="utf-8")
    cfg_real = load_config(real)
    ok = by_name(
        await doctor(
            cfg_real, settings(tmp_path).model_copy(update={"env": "production", "config_dir": real}), db, Endpoints(cfg_real), offline=True
        ).run()
    )
    assert ok["config:files"].status == "ok" and "(example)" not in ok["config:files"].detail
    assert "exec-222" not in ok["config:wake_on_lan"].detail and "model-224" in ok["config:wake_on_lan"].detail


async def test_pgvector_verdicts() -> None:
    from hermclaw.doctor import pgvector_status

    assert pgvector_status(None)[0] == "fail"
    assert pgvector_status("0.8.0") == ("fail", "pgvector 0.8.0 < 0.8.2 (CVE-2026-3172) – install postgresql-17-pgvector from PGDG")
    status, detail = pgvector_status("0.8.6")
    assert status == "warn" and "CVE-2026-103484" in detail
    assert pgvector_status("0.8.7") == ("ok", "pgvector 0.8.7") and pgvector_status("0.10.0")[0] == "ok"


async def test_llamacpp_server_is_checked_for_the_profiles_that_use_it(db: Any, tmp_path: Path) -> None:
    """D-015: chat roles on the resident llama-server; doctor checks it is healthy and serves their model."""
    cfg = get_config()
    profiles = [
        p.model_copy(update={"backend": "llamacpp", "model": "qwen3.6:35b", "deviation": "D-015", "gpu_layers": None})
        if p.kind == "chat"
        else p
        for p in cfg.models.profiles
    ]
    cfg = cfg.model_copy(update={"models": cfg.models.model_copy(update={"profiles": profiles})})
    ep = Endpoints(cfg)
    checks = by_name(await doctor(cfg, settings(tmp_path), db, ep).run())
    assert checks["llamacpp:model-224"].status == "ok" and "qwen3.6:35b" in checks["llamacpp:model-224"].detail
    ep.llama_served = ["other:1b"]
    checks = by_name(await doctor(cfg, settings(tmp_path), db, ep).run())
    assert checks["llamacpp:model-224"].status == "fail" and "--alias" in checks["llamacpp:model-224"].detail
    ep.llama_status = 503
    checks = by_name(await doctor(cfg, settings(tmp_path), db, ep).run())
    assert checks["llamacpp:model-224"].status == "fail" and "loading" in checks["llamacpp:model-224"].detail
    assert "llamacpp:model-224" not in by_name(await doctor(get_config(), settings(tmp_path), db, Endpoints(get_config())).run())
