"""P07 7.6 execution worker daemon (``.222``) driven through the real :class:`ExecutionWorkerClient`
(signed requests, in-process ASGI transport); container recovery against real podman."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.contracts.worker import CommandRequest
from hermclaw.core.config import SandboxPolicy
from hermclaw.workers.client import ExecutionWorkerClient, probe_health
from hermclaw.workers.errors import WorkerAuthFailed, WorkerBusy, WorkerRemoteError
from tests.integration.test_workers_support import (
    TOKEN,
    FailingRunner,
    LocalTestRunner,
    lifespan,
    make_settings,
    make_tar,
    read_tar,
    workspace_ops,
)
from worker.execution.app import RWLock, create_app

pytestmark = pytest.mark.integration
WORKER = "exec-test-222"


def _app(tmp_path: Path, runner: Any = None, **settings_kw: Any) -> FastAPI:
    settings_kw.setdefault("sandbox", SandboxPolicy(engine="local"))
    settings = make_settings(tmp_path, WorkerKind.execution, WORKER, **settings_kw)
    return create_app(settings, runner=runner or LocalTestRunner(), workspace_ops=workspace_ops(), heartbeat=False)


def _client(app: FastAPI, token: str = TOKEN) -> ExecutionWorkerClient:
    return ExecutionWorkerClient("http://exec", worker_id=WORKER, token=token, transport=httpx.ASGITransport(app=app), get_retries=0)


@pytest.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, ExecutionWorkerClient]]:
    app = _app(tmp_path)
    async with lifespan(app), _client(app) as c:
        yield app, c


def cmd(command: str, **kw: Any) -> CommandRequest:
    values: dict[str, Any] = {
        "request_id": f"r-{uuid.uuid4().hex}",
        "job_id": "job-1",
        "step_id": "step-1",
        "workspace": "ws1",
        "command": command,
        "timeout_seconds": 30,
    }
    values.update(kw)
    return CommandRequest(**values)


async def test_health_is_unauthenticated(daemon: tuple[FastAPI, ExecutionWorkerClient]) -> None:
    app, c = daemon
    h = await c.health()
    assert h.worker_id == WORKER and h.kind == WorkerKind.execution and h.state == WorkerState.ready
    assert h.checks == {"workspaces_writable": True, "sandbox_engine": True} and h.status == "ok"
    probed = await probe_health("http://exec", transport=httpx.ASGITransport(app=app))
    assert probed.worker_id == WORKER


async def test_workspace_lifecycle(daemon: tuple[FastAPI, ExecutionWorkerClient]) -> None:
    _app_, c = daemon
    tar = make_tar({"README.md": b"# demo\n", "src/app.py": b"def add(a, b):\n    return a + b\n"})
    info = await c.upload_workspace("ws1", tar)
    assert info.exists and info.files == 2 and info.mode == "replace"
    manifest = await c.workspace_manifest("ws1")
    assert set(manifest.files) == {"README.md", "src/app.py"} and len(manifest.files["README.md"]) == 64
    await c.upload_workspace("ws1", make_tar({"src/new.py": b"x = 1\n"}), mode="merge")
    assert (await c.workspace_info("ws1")).files == 3
    archive = read_tar(await c.download_workspace("ws1"))
    assert archive["src/app.py"].startswith(b"def add") and archive["src/new.py"] == b"x = 1\n"
    only = read_tar(await c.download_workspace("ws1", paths=["src/new.py"]))
    assert set(only) == {"src/new.py"}
    res = await c.delete_paths("ws1", ["src/new.py", "missing.txt"])
    assert res.deleted == ["src/new.py"] and res.missing == ["missing.txt"]
    # replace drops files that are not in the new archive
    await c.upload_workspace("ws1", make_tar({"only.txt": b"1"}))
    assert set((await c.workspace_manifest("ws1")).files) == {"only.txt"}
    gone = await c.delete_workspace("ws1")
    assert not gone.exists
    with pytest.raises(WorkerRemoteError) as exc:
        await c.workspace_info("ws1")
    assert exc.value.status_code == 404 and exc.value.remote_code == "WORKSPACE_NOT_FOUND"


async def test_workspace_path_safety(daemon: tuple[FastAPI, ExecutionWorkerClient], tmp_path: Path) -> None:
    _app_, c = daemon
    await c.upload_workspace("ws1", make_tar({"a.txt": b"a"}))
    for bad in ("../outside", "/etc/passwd", ".", "a/../../x"):
        with pytest.raises(WorkerRemoteError) as exc:
            await c.delete_paths("ws1", [bad])
        assert exc.value.status_code == 400 and exc.value.remote_code == "WORKSPACE_PATH_INVALID"
    with pytest.raises(WorkerRemoteError) as exc:
        await c.download_workspace("ws1", paths=["../../etc"])
    assert exc.value.status_code == 400
    # a symlinked directory inside the workspace must not let deletes escape
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    ws_dir = tmp_path / "data" / "workspaces" / "ws1"
    (ws_dir / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkerRemoteError):
        await c.delete_paths("ws1", ["link/keep.txt"])
    assert (outside / "keep.txt").exists()
    res = await c.delete_paths("ws1", ["link"])  # removes the link itself, never the target
    assert res.deleted == ["link"] and (outside / "keep.txt").exists()
    # invalid workspace ids are rejected by the router
    with pytest.raises(WorkerRemoteError) as exc:
        await c.upload_workspace("..", make_tar({"a": b"1"}))
    assert exc.value.status_code in (404, 422)
    with pytest.raises(WorkerRemoteError) as exc:
        await c.upload_workspace("ws2", b"this is not a tar archive")
    assert exc.value.status_code == 400
    assert not (tmp_path / "data" / "workspaces" / "ws2").exists()


async def test_run_command_and_idempotency(daemon: tuple[FastAPI, ExecutionWorkerClient]) -> None:
    app, c = daemon
    await c.upload_workspace("ws1", make_tar({"data.txt": b"hello\n"}))
    req = cmd("cat data.txt; echo err >&2; exit 3")
    result = await c.run_command(req)
    # NOTE: the shared Contract base strips surrounding whitespace (str_strip_whitespace) - see shared change request
    assert result.exit_code == 3 and result.stdout.strip() == "hello" and result.stderr.strip() == "err"
    assert result.request_id == req.request_id
    runner = app.state.execution.runner
    assert isinstance(runner, LocalTestRunner) and len(runner.calls) == 1
    again = await c.run_command(req)  # same request_id + same payload -> cached result, no re-execution
    assert again == result and len(runner.calls) == 1
    status = await c.command_status(req.request_id)
    assert status is not None and status.status == "finished" and status.result == result
    assert await c.command_status("never-ran") is None
    with pytest.raises(WorkerRemoteError) as exc:
        await c.run_command(req.model_copy(update={"command": "rm -rf /"}))
    assert exc.value.status_code == 409 and exc.value.remote_code == "REQUEST_ID_CONFLICT"
    with pytest.raises(WorkerRemoteError) as exc:
        await c.run_command(cmd("true", workspace="nope"))
    assert exc.value.remote_code == "WORKSPACE_NOT_FOUND"


async def test_secrets_are_redacted_from_command_output(daemon: tuple[FastAPI, ExecutionWorkerClient]) -> None:
    _app_, c = daemon
    await c.upload_workspace("ws1", make_tar({"x": b""}))
    result = await c.run_command(cmd(f"echo {TOKEN}"))
    assert TOKEN not in result.stdout and result.exit_code == 0


async def test_busy_when_all_slots_used_and_heartbeat_state(tmp_path: Path) -> None:
    app = _app(tmp_path, max_concurrent_commands=1)
    async with lifespan(app), _client(app) as c:
        await c.upload_workspace("ws1", make_tar({"x": b""}))
        slow = asyncio.create_task(c.run_command(cmd("sleep 1; echo done", job_id="job-slow", step_id="step-slow")))
        state = app.state.daemon.state
        for _ in range(100):
            if state.state == WorkerState.busy:
                break
            await asyncio.sleep(0.01)
        assert state.state == WorkerState.busy and state.active_job == "job-slow" and state.active_step == "step-slow"
        assert (await c.health()).state == WorkerState.busy
        with pytest.raises(WorkerBusy) as exc:
            await c.run_command(cmd("true"))
        assert exc.value.remote_code == "WORKER_BUSY"
        # an upload waits for the running command (exclusive workspace lock) instead of yanking the tree
        upload = asyncio.create_task(c.upload_workspace("ws1", make_tar({"y": b""})))
        await asyncio.sleep(0.2)
        assert not upload.done()
        assert (await slow).stdout.strip() == "done"
        await upload
        assert state.state == WorkerState.ready


async def test_runner_failure_becomes_result_error(tmp_path: Path) -> None:
    app = _app(tmp_path, runner=FailingRunner())
    async with lifespan(app), _client(app) as c:
        await c.upload_workspace("ws1", make_tar({"x": b""}))
        result = await c.run_command(cmd("true"))
    assert result.exit_code is None and "engine exploded" in (result.error or "") and result.sandbox == "local"


async def test_wrong_credentials_rejected(daemon: tuple[FastAPI, ExecutionWorkerClient]) -> None:
    app, _c = daemon
    async with _client(app, token="q" * 64) as bad:
        with pytest.raises(WorkerAuthFailed) as exc:
            await bad.workspace_info("ws1")
    assert exc.value.details["remote_code"] in ("WORKER_AUTH_BAD_TOKEN", "WORKER_AUTH_BAD_SIGNATURE")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://exec") as raw:
        r = await raw.post("/v1/commands", json=cmd("true").model_dump())
    assert r.status_code == 401


async def test_selftest_local_engine(daemon: tuple[FastAPI, ExecutionWorkerClient]) -> None:
    _app_, c = daemon
    result = await c.selftest()
    names = [ch.name for ch in result.checks]
    assert names == ["workspaces_writable", "disk_free", "sandbox_engine", "sandbox_run"]
    assert result.ok, result.checks


async def test_draining_rejects_new_work(daemon: tuple[FastAPI, ExecutionWorkerClient]) -> None:
    app, c = daemon
    await c.upload_workspace("ws1", make_tar({"x": b""}))
    app.state.daemon.state.draining = True
    with pytest.raises(WorkerRemoteError) as exc:
        await c.run_command(cmd("true"))
    assert exc.value.status_code == 503 and exc.value.remote_code == "WORKER_DRAINING"
    assert (await c.health()).state == WorkerState.draining


async def test_rwlock_writer_preference() -> None:
    lock = RWLock()
    order: list[str] = []

    async def reader(name: str, delay: float) -> None:
        async with lock.read():
            order.append(f"{name}+")
            await asyncio.sleep(delay)
            order.append(f"{name}-")

    async def writer() -> None:
        async with lock.write():
            order.append("w")

    r1 = asyncio.create_task(reader("r1", 0.1))
    await asyncio.sleep(0.01)
    w = asyncio.create_task(writer())
    await asyncio.sleep(0.01)
    r2 = asyncio.create_task(reader("r2", 0))
    await asyncio.gather(r1, w, r2)
    assert order == ["r1+", "r1-", "w", "r2+", "r2-"]
    assert lock.idle


# ------------------------------------------------------------------------------------------- podman
def _podman_usable() -> bool:
    if shutil.which("podman") is None:
        return False
    try:
        return subprocess.run(["podman", "image", "exists", "docker.io/library/alpine:3.20"], timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _podman_usable(), reason="podman with docker.io/library/alpine:3.20 not available")
async def test_container_recovery_with_real_podman(tmp_path: Path) -> None:
    label = f"hermclaw.p07test={uuid.uuid4().hex[:12]}"
    names = [f"hermclaw-p07-{uuid.uuid4().hex[:8]}" for _ in range(2)]
    unrelated = f"hermclaw-p07-unrelated-{uuid.uuid4().hex[:8]}"
    try:
        for name in names:
            subprocess.run(
                ["podman", "create", "--name", name, "--label", label, "docker.io/library/alpine:3.20", "true"], check=True, timeout=60
            )
        subprocess.run(["podman", "create", "--name", unrelated, "docker.io/library/alpine:3.20", "true"], check=True, timeout=60)
        app = _app(tmp_path, sandbox=SandboxPolicy(engine="podman"), container_label=label)
        async with _client(app) as c:
            # the daemon probes the engine version
            await app.state.execution.probe_engine()
            assert app.state.execution.engine_version
            result = await c.recover_containers()
            assert sorted(result.removed) == sorted(names) and result.errors == [] and result.engine == "podman"
            again = await c.recover_containers()
            assert again.removed == []
        exists = subprocess.run(["podman", "container", "exists", unrelated], check=False, timeout=30)
        assert exists.returncode == 0  # containers without the Hermclaw label are never touched
    finally:
        subprocess.run(["podman", "rm", "-f", *names, unrelated], check=False, timeout=60, capture_output=True)


@pytest.mark.skipif(not _podman_usable(), reason="podman not available")
async def test_startup_recovery_removes_leftovers(tmp_path: Path) -> None:
    label = f"hermclaw.p07test={uuid.uuid4().hex[:12]}"
    name = f"hermclaw-p07-{uuid.uuid4().hex[:8]}"
    leftover = tmp_path / "data" / "workspaces" / ".incoming-ws1-deadbeef"
    leftover.mkdir(parents=True)
    try:
        subprocess.run(
            ["podman", "create", "--name", name, "--label", label, "docker.io/library/alpine:3.20", "true"], check=True, timeout=60
        )
        app = _app(tmp_path, sandbox=SandboxPolicy(engine="podman"), container_label=label)
        async with lifespan(app):
            pass
        assert subprocess.run(["podman", "container", "exists", name], check=False, timeout=30).returncode == 1
        assert not leftover.exists()
    finally:
        subprocess.run(["podman", "rm", "-f", name], check=False, timeout=60, capture_output=True)


async def test_recovery_refuses_while_commands_run_and_engine_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, sandbox=SandboxPolicy(engine="podman"), container_label="hermclaw.p07test=none")
    svc = app.state.execution
    async with _client(app) as c:
        await c.upload_workspace("ws1", make_tar({"x": b""}))
        task = asyncio.create_task(c.run_command(cmd("sleep 0.5")))
        await asyncio.sleep(0.1)
        with pytest.raises(WorkerRemoteError) as exc:
            await c.recover_containers()
        assert exc.value.status_code == 409 and exc.value.remote_code == "COMMANDS_RUNNING"
        await task

        original = shutil.which
        monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: None if name == "podman" else original(name, *a, **kw))
        with pytest.raises(WorkerRemoteError) as exc:
            await c.recover_containers()
        assert exc.value.status_code == 503 and exc.value.remote_code == "SANDBOX_ENGINE_MISSING"
        extras = await svc.extras()
        assert svc.engine_error is not None and extras.readiness_error and "not found" in extras.readiness_error
        health = await c.health()
        assert health.status == "degraded" and health.checks["sandbox_engine"] is False


def _sandbox_module_available() -> bool:
    try:
        import worker.execution.sandbox  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not (_sandbox_module_available() and _podman_usable()), reason="sandbox component or podman not available")
async def test_real_sandbox_runner_through_daemon(tmp_path: Path) -> None:
    """With the sandbox component present the daemon's default runner executes inside podman."""
    settings = make_settings(
        tmp_path, WorkerKind.execution, WORKER, sandbox=SandboxPolicy(engine="podman", image="docker.io/library/alpine:3.20")
    )
    app = create_app(settings, heartbeat=False)
    async with lifespan(app), _client(app) as c:
        await c.upload_workspace("ws1", make_tar({"in.txt": b"42\n"}))
        result = await c.run_command(cmd("cat in.txt"))
    assert result.exit_code == 0 and result.stdout.strip() == "42" and result.sandbox == "podman"
