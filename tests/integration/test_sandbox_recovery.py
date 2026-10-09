"""P18 18.8/18.9 against the real podman engine: leftovers of a crashed worker are found by label and removed,
running commands of this process and foreign containers are never touched.

Each test uses its own random managed label, so containers of other test suites sharing the engine are never
seen (and never removed)."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.worker import CommandRequest
from hermclaw.core.config import SandboxPolicy
from worker.execution.recovery import list_managed_containers, recover_abandoned, recover_for_sandbox, run_periodic_recovery
from worker.execution.sandbox import PodmanSandbox

IMAGE = "docker.io/library/alpine:3.20"


def _podman_ready() -> str | None:
    if shutil.which("podman") is None:
        return "podman not installed"
    rc = subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True, timeout=60, check=False).returncode
    return None if rc == 0 else f"image {IMAGE} not present locally (pull it through the proxy first)"


_SKIP = _podman_ready()
pytestmark = [pytest.mark.integration, pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")]


@pytest.fixture
def label() -> Any:
    value = f"hermclaw.p18rec={uuid.uuid4().hex[:10]}"
    yield value
    ids = subprocess.run(
        ["podman", "ps", "-a", "-q", "--filter", f"label={value}"], capture_output=True, text=True, check=False
    ).stdout.split()
    if ids:
        subprocess.run(["podman", "rm", "-f", "-t", "0", *ids], capture_output=True, check=False)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


def sandbox(label: str) -> PodmanSandbox:
    policy = SandboxPolicy(engine="podman", image=IMAGE, cpus=1.0, memory="128m", pids_limit=64, tmpfs_size="8m")
    return PodmanSandbox(policy, pull="never", managed_label=label, max_output_bytes=10_000)


def req(command: str, **kw: Any) -> CommandRequest:
    return CommandRequest(
        request_id=kw.pop("request_id", f"p18rec-{uuid.uuid4().hex[:10]}"),
        job_id="job-r",
        step_id="step-r",
        workspace="ws",
        command=command,
        **kw,
    )


def _exists(name: str) -> bool:
    return subprocess.run(["podman", "container", "exists", name], capture_output=True, check=False).returncode == 0


def _state(name: str) -> str:
    return subprocess.run(
        ["podman", "container", "inspect", "--format", "{{.State.Status}}", name], capture_output=True, text=True, check=False
    ).stdout.strip()


async def _wait_for(predicate: Any, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return bool(predicate())


async def _crashed_worker_container(label: str, ws: Path) -> str:
    """Start a sandbox container exactly like :class:`PodmanSandbox` would and SIGKILL the podman client –
    what a crashing/OOM-killed worker leaves behind: a running, labelled container nobody waits for."""
    inv = sandbox(label).build_invocation(req("sleep 300"), ws)
    proc = await asyncio.create_subprocess_exec(*inv.argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    assert await _wait_for(lambda: _state(inv.container_name) == "running")
    os.kill(proc.pid, signal.SIGKILL)
    await proc.wait()
    await asyncio.sleep(0.5)
    assert _state(inv.container_name) == "running"  # survives the client (conmon keeps it alive)
    return inv.container_name


async def test_crashed_worker_leftover_is_recovered(label: str, ws: Path) -> None:
    name = await _crashed_worker_container(label, ws)
    records = await list_managed_containers("podman", label=label)
    assert [r.name for r in records] == [name]
    assert records[0].labels["hermclaw.job"] == "job-r" and records[0].labels["hermclaw.step"] == "step-r"
    assert records[0].request_id == name.removeprefix("hermclaw-") and records[0].created_at is not None

    fresh = sandbox(label)  # the restarted worker: tracks nothing yet
    young = await recover_for_sandbox(fresh, older_than_seconds=3600)
    assert young.removed == [] and young.kept == {name: "younger than 3600s"} and _exists(name)
    dry = await recover_for_sandbox(fresh, older_than_seconds=0, dry_run=True)
    assert dry.removed == [name] and _exists(name)
    report = await recover_for_sandbox(fresh, older_than_seconds=0)
    assert report.removed == [name] and report.errors == [] and report.scanned == 1
    assert not _exists(name)


async def test_running_command_of_this_process_is_kept(label: str, ws: Path) -> None:
    s = sandbox(label)
    task = asyncio.create_task(s.run(req("sleep 2; echo survived"), ws))
    assert await _wait_for(lambda: bool(s.active_containers()) and _state(next(iter(s.active_containers()))) == "running")
    name = next(iter(s.active_containers()))
    report = await recover_for_sandbox(s, older_than_seconds=0)
    assert report.kept == {name: "tracked"} and report.removed == []
    result = await task
    assert result.exit_code == 0 and result.stdout.strip() == "survived"


async def test_created_and_exited_leftovers_removed_foreign_untouched(label: str) -> None:
    suffix = uuid.uuid4().hex[:8]
    created, exited, foreign = f"hermclaw-c-{suffix}", f"hermclaw-e-{suffix}", f"hermclaw-f-{suffix}"
    subprocess.run(["podman", "create", "--name", created, "--label", label, IMAGE, "true"], check=True, capture_output=True)
    subprocess.run(["podman", "run", "--name", exited, "--label", label, IMAGE, "true"], check=True, capture_output=True)
    other_label = f"hermclaw.p18rec={uuid.uuid4().hex[:10]}"
    subprocess.run(["podman", "create", "--name", foreign, "--label", other_label, IMAGE, "true"], check=True, capture_output=True)
    try:
        report = await recover_abandoned(label=label, older_than_seconds=0)
        assert sorted(report.removed) == sorted([created, exited]) and report.errors == []
        assert not _exists(created) and not _exists(exited)
        assert _exists(foreign)
    finally:
        subprocess.run(["podman", "rm", "-f", foreign], capture_output=True, check=False)


async def test_periodic_recovery_loop(label: str, ws: Path, tmp_path: Path) -> None:
    name = await _crashed_worker_container(label, ws)
    workspaces = tmp_path / "workspaces"
    (workspaces / "ws-stale").mkdir(parents=True)
    (workspaces / "ws-busy").mkdir()
    old = time.time() - 7200
    for p in (workspaces / "ws-stale", workspaces / "ws-busy"):
        os.utime(p, (old, old))
    stop = asyncio.Event()
    loop_task = asyncio.create_task(
        run_periodic_recovery(
            sandbox(label),
            interval_seconds=0.2,
            stop=stop,
            older_than_seconds=0,
            workspaces_root=workspaces,
            workspace_max_age_seconds=3600,
            busy_workspaces=lambda: {"ws-busy"},
        )
    )
    try:
        assert await _wait_for(lambda: not _exists(name) and not (workspaces / "ws-stale").exists())
        assert (workspaces / "ws-busy").exists()
    finally:
        stop.set()
        await asyncio.wait_for(loop_task, 30)


async def test_timeout_leaves_nothing_for_recovery(label: str, ws: Path) -> None:
    s = sandbox(label)
    r = await s.run(req("sleep 60", timeout_seconds=1), ws)
    assert r.timed_out
    assert await list_managed_containers("podman", label=label) == []  # 18.8: killed and removed right away
