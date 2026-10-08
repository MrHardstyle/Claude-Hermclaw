"""P07 daemon building blocks: settings, /proc metrics, nvidia-smi telemetry, state, signed-request middleware."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.types import Message, Receive, Scope, Send

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.core.errors import ConfigError
from hermclaw.workers.auth import ReplayCache, WorkerRequestSigner, sign_headers
from worker.common.auth import SignedRequestMiddleware
from worker.common.gpu import NVIDIA_SMI_ARGS, parse_nvidia_smi_csv, query_gpus
from worker.common.settings import BASE_CAPABILITIES, WorkerDaemonSettings, capabilities_from_file, load_sandbox_policy, parse_bind
from worker.common.state import DaemonState
from worker.common.system import SystemSampler, cpu_percent_between, read_cpu_times, read_loadavg, read_meminfo, read_uptime

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "k" * 48 + "-daemon-token"


# ------------------------------------------------------------------------------------------- settings
def test_settings_from_env_defaults(tmp_path: Path) -> None:
    s = WorkerDaemonSettings.from_env({"WORKER_ID": "exec-222", "WORKER_TOKEN_FILE": str(tmp_path / "t")}, default_kind=WorkerKind.execution)
    assert s.kind == WorkerKind.execution and s.bind == "127.0.0.1:8787" and s.orchestrator_url is None
    assert s.heartbeat_seconds == 15.0 and s.max_skew_seconds == 120.0
    assert s.all_capabilities() == sorted(BASE_CAPABILITIES[WorkerKind.execution])
    assert s.sandbox.engine == "podman"


def test_settings_from_env_full(tmp_path: Path) -> None:
    policies = tmp_path / "policies.yaml"
    policies.write_text("sandbox:\n  engine: docker\n  cpus: 1.5\n", encoding="utf-8")
    env = {
        "WORKER_ID": "model-224",
        "WORKER_KIND": "model",
        "CREDENTIALS_DIRECTORY": str(tmp_path),
        "ORCHESTRATOR_URL": "http://192.168.178.225:8000/",
        "WORKER_BIND": "0.0.0.0:9000",
        "WORKER_HEARTBEAT_SECONDS": "5",
        "WORKER_CAPABILITIES": "embedding, chat ,",
        "WORKER_LOG_JSON": "false",
        "WORKER_MAX_BODY_MB": "64",
        "WORKER_POLICIES_FILE": str(policies),
        "OLLAMA_URL": "http://127.0.0.1:11434/",
        "WORKER_DATA_DIR": str(tmp_path / "data"),
    }
    s = WorkerDaemonSettings.from_env(env)
    assert s.token_file == tmp_path / "worker-token"
    assert s.orchestrator_url == "http://192.168.178.225:8000" and s.ollama_url == "http://127.0.0.1:11434"
    assert (s.bind_host, s.bind_port) == ("0.0.0.0", 9000)
    assert s.capabilities == ("embedding", "chat") and "ollama" in s.all_capabilities()
    assert not s.log_json and s.max_body_bytes == 64 * 1024 * 1024 and s.heartbeat_seconds == 5.0
    assert s.sandbox.engine == "docker" and s.sandbox.cpus == 1.5
    assert s.workspaces_dir == tmp_path / "data" / "workspaces"


def test_settings_capabilities_from_config_file() -> None:
    caps = capabilities_from_file(ROOT / "config" / "capabilities.example.yaml", WorkerKind.execution)
    assert caps and "testing" in caps
    s = WorkerDaemonSettings.from_env(
        {"WORKER_ID": "e", "WORKER_KIND": "execution", "WORKER_CAPABILITIES_FILE": str(ROOT / "config" / "capabilities.example.yaml")}
    )
    assert set(caps) <= set(s.all_capabilities())
    assert load_sandbox_policy(ROOT / "config" / "policies.example.yaml").engine in ("podman", "docker", "local")


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"WORKER_ID": "x"},
        {"WORKER_ID": "x", "WORKER_KIND": "gpu"},
        {"WORKER_ID": "bad id!", "WORKER_KIND": "model"},
        {"WORKER_ID": "x", "WORKER_KIND": "model", "WORKER_BIND": "8787"},
        {"WORKER_ID": "x", "WORKER_KIND": "model", "WORKER_HEARTBEAT_SECONDS": "fast"},
        {"WORKER_ID": "x", "WORKER_KIND": "model", "WORKER_HEARTBEAT_SECONDS": "0.1"},
        {"WORKER_ID": "x", "WORKER_KIND": "model", "WORKER_LOG_JSON": "maybe"},
        {"WORKER_ID": "x", "WORKER_KIND": "model", "WORKER_POLICIES_FILE": "/nonexistent/policies.yaml"},
    ],
)
def test_settings_invalid(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        WorkerDaemonSettings.from_env(env)


def test_settings_kind_mismatch_with_daemon() -> None:
    with pytest.raises(ConfigError):
        WorkerDaemonSettings.from_env({"WORKER_ID": "x", "WORKER_KIND": "model"}, default_kind=WorkerKind.execution)


def test_parse_bind() -> None:
    assert parse_bind("[::1]:8787") == ("::1", 8787)
    with pytest.raises(ConfigError):
        parse_bind("host:70000")


# ------------------------------------------------------------------------------------------- metrics
def _proc(tmp_path: Path, stat_line: str) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    (proc / "stat").write_text(f"{stat_line}\ncpu0 1 2 3 4\n", encoding="ascii")
    (proc / "meminfo").write_text("MemTotal:       32768000 kB\nMemFree:  1000 kB\nMemAvailable:   16384000 kB\n", encoding="ascii")
    (proc / "loadavg").write_text("0.50 1.25 2.00 1/123 4567\n", encoding="ascii")
    (proc / "uptime").write_text("12345.67 54321.00\n", encoding="ascii")
    return proc


def test_proc_parsers(tmp_path: Path) -> None:
    proc = _proc(tmp_path, "cpu  100 0 100 700 100 0 0 0 0 0")
    t1 = read_cpu_times(proc)
    assert t1 is not None and t1.idle == 800 and t1.total == 1000
    (proc / "stat").write_text("cpu  200 0 200 1300 300 0 0 0 0 0\n", encoding="ascii")
    t2 = read_cpu_times(proc)
    # delta total 1000, idle delta (1300+300)-(800)=800 -> 20 % busy
    assert cpu_percent_between(t1, t2) == 20.0
    assert cpu_percent_between(None, t2) == 0.0 and cpu_percent_between(t2, t2) == 0.0
    assert read_meminfo(proc) == (32000, 16000)
    assert read_loadavg(proc) == [0.5, 1.25, 2.0]
    assert read_uptime(proc) == 12345


def test_proc_parsers_degrade_gracefully(tmp_path: Path) -> None:
    empty = tmp_path / "noproc"
    empty.mkdir()
    assert read_cpu_times(empty) is None
    assert read_meminfo(empty) == (0, 0)
    assert read_uptime(empty) == 0
    assert isinstance(read_loadavg(empty), list)


def test_system_sampler_real_host(tmp_path: Path) -> None:
    sampler = SystemSampler(tmp_path / "does" / "not" / "exist")
    m = sampler.sample()
    assert m.ram_total_mb > 0 and m.disk_free_mb > 0 and 0.0 <= m.cpu_percent <= 100.0 and len(m.load_avg) == 3


# ------------------------------------------------------------------------------------------- gpu
NVIDIA_CSV = "0, NVIDIA GeForce GTX 1080, 8192, 1536, 37, 54, 550.163.01\n1, Some GPU, 4096, [N/A], [Not Supported], [N/A], [N/A]\ngarbage\n"


def test_parse_nvidia_smi_csv() -> None:
    gpus = parse_nvidia_smi_csv(NVIDIA_CSV)
    assert len(gpus) == 2
    g = gpus[0]
    assert (g.index, g.name, g.memory_total_mb, g.memory_used_mb, g.utilization_percent, g.temperature_c, g.driver) == (
        0,
        "NVIDIA GeForce GTX 1080",
        8192,
        1536,
        37,
        54,
        "550.163.01",
    )
    assert gpus[1].memory_used_mb == 0 and gpus[1].temperature_c is None and gpus[1].driver == ""


def write_fake_nvidia_smi(directory: Path, output: str = NVIDIA_CSV, exit_code: int = 0) -> Path:
    """Test-only stand-in for nvidia-smi: checks the exact query arguments, prints CSV."""
    script = directory / "nvidia-smi"
    expected = " ".join(NVIDIA_SMI_ARGS)
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$*" != "{expected}" ]; then echo "unexpected args: $*" >&2; exit 9; fi\n'
        f"cat <<'EOF'\n{output}EOF\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


async def test_query_gpus_with_executable(tmp_path: Path) -> None:
    script = write_fake_nvidia_smi(tmp_path)
    res = await query_gpus(str(script))
    assert res.available and res.gpus[0].name == "NVIDIA GeForce GTX 1080" and res.driver == "550.163.01"
    failing = tmp_path / "f"
    failing.mkdir()
    res = await query_gpus(str(write_fake_nvidia_smi(failing, "NVIDIA-SMI has failed\n", exit_code=6)))
    assert not res.available and "exit 6" in (res.error or "")
    res = await query_gpus(str(tmp_path / "missing-nvidia-smi"))
    assert not res.available and res.error == "nvidia-smi not found"


# ------------------------------------------------------------------------------------------- state
def test_daemon_state_effective_state() -> None:
    st = DaemonState("w", WorkerKind.execution)
    assert st.state == WorkerState.starting
    st.starting = False
    assert st.state == WorkerState.ready
    with st.work("r1", kind="command", job_id="j1", step_id="s1"):
        assert st.state == WorkerState.busy and st.active_job == "j1" and st.active_step == "s1"
    assert st.state == WorkerState.ready and st.active_job is None
    st.draining = True
    assert st.state == WorkerState.draining and not st.accepting_work
    st.readiness_error = "ollama down"
    assert st.state == WorkerState.error
    st.shutting_down = True
    assert st.state == WorkerState.offline


# ------------------------------------------------------------------------------------------- middleware
async def _echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    auth = scope.get("state", {}).get("worker_auth")
    payload = json.dumps({"len": len(body), "sha": __import__("hashlib").sha256(body).hexdigest(), "worker": getattr(auth, "worker_id", None)})
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": payload.encode()})


def _mw(max_body: int = 10 * 1024 * 1024) -> SignedRequestMiddleware:
    return SignedRequestMiddleware(_echo_app, worker_id="exec-1", tokens=lambda: [TOKEN], max_body_bytes=max_body, replay_cache=ReplayCache())


async def test_middleware_health_exempt_and_rejects_unsigned() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_mw()), base_url="http://w") as c:
        assert (await c.get("/health")).status_code == 200
        r = await c.post("/v1/commands", content=b"{}")
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_MISSING"


async def test_middleware_accepts_signed_and_passes_body() -> None:
    body = bytes(range(256)) * 4096  # 1 MiB
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_mw()), base_url="http://w", auth=WorkerRequestSigner("exec-1", TOKEN)
    ) as c:
        r = await c.put("/v1/workspaces/ws1", content=body, params={"mode": "merge"})
    assert r.status_code == 200
    data = r.json()
    assert data["len"] == len(body) and data["sha"] == __import__("hashlib").sha256(body).hexdigest() and data["worker"] == "exec-1"


async def test_middleware_rejects_other_worker_wrong_token_and_replay() -> None:
    mw = _mw()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mw), base_url="http://w", auth=WorkerRequestSigner("model-1", TOKEN)) as c:
        r = await c.post("/x", content=b"1")
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_WRONG_WORKER"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mw), base_url="http://w", auth=WorkerRequestSigner("exec-1", "z" * 64, include_bearer=False)
    ) as c:
        r = await c.post("/x", content=b"1")
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_BAD_SIGNATURE"
    headers = sign_headers(worker_id="exec-1", token=TOKEN, method="POST", path="/x", body=b"1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mw), base_url="http://w") as c:
        assert (await c.post("/x", content=b"1", headers=headers)).status_code == 200
        r = await c.post("/x", content=b"1", headers=headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_REPLAY"
        # body swapped under a valid signature
        headers2 = sign_headers(worker_id="exec-1", token=TOKEN, method="POST", path="/x", body=b"1")
        r = await c.post("/x", content=b"2", headers=headers2)
        assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_BAD_SIGNATURE"


async def test_middleware_body_limit_declared_and_streamed() -> None:
    mw = _mw(max_body=1000)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mw), base_url="http://w", auth=WorkerRequestSigner("exec-1", TOKEN)
    ) as c:
        r = await c.put("/v1/workspaces/a", content=b"x" * 1001)
        assert r.status_code == 413 and r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    # chunked body without content-length, streamed in many ASGI messages
    body = b"y" * 3000
    headers = sign_headers(worker_id="exec-1", token=TOKEN, method="PUT", path="/v1/workspaces/a", body=body)
    sent: list[Message] = []
    chunks = [body[i : i + 500] for i in range(0, len(body), 500)]

    async def receive() -> Message:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(msg: Message) -> None:
        sent.append(msg)

    scope: dict[str, Any] = {
        "type": "http",
        "method": "PUT",
        "path": "/v1/workspaces/a",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    await mw(scope, receive, send)
    assert sent[0]["status"] == 413


async def test_middleware_replays_multichunk_body_to_app() -> None:
    mw = SignedRequestMiddleware(_echo_app, worker_id="exec-1", tokens=lambda: [TOKEN])
    body = b"abc" * (4 * 1024 * 1024)  # 12 MiB: rolls the spool over to disk and replays in 1 MiB chunks
    headers = sign_headers(worker_id="exec-1", token=TOKEN, method="PUT", path="/v1/workspaces/a", body=body)
    chunks = [body[i : i + 65536] for i in range(0, len(body), 65536)]
    sent: list[Message] = []

    async def receive() -> Message:
        if not chunks:
            return {"type": "http.disconnect"}
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(msg: Message) -> None:
        sent.append(msg)

    scope: dict[str, Any] = {
        "type": "http",
        "method": "PUT",
        "path": "/v1/workspaces/a",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    await mw(scope, receive, send)
    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"])["len"] == len(body)


async def test_middleware_token_unavailable_rejects() -> None:
    from hermclaw.workers.errors import WorkerAuthError

    def broken() -> list[str]:
        raise WorkerAuthError("gone", code="WORKER_TOKEN_MISSING")

    mw = SignedRequestMiddleware(_echo_app, worker_id="exec-1", tokens=broken)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mw), base_url="http://w", auth=WorkerRequestSigner("exec-1", TOKEN)
    ) as c:
        r = await c.post("/x", content=b"1")
    assert r.status_code == 401 and r.json()["error"]["code"] == "WORKER_AUTH_UNKNOWN"
