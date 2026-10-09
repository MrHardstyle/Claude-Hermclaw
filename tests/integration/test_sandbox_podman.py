"""P18 sandbox against the real podman engine of the build host (alpine image, no network pulls).

Skipped with a clear reason when podman or the image is unavailable. The build host runs podman 4.9.3 as
root with cgroups v1 (rootful), so limits are enforced here; on ``.222`` (rootless, cgroups v2) the same
tests are the ``live`` verification (BLOCKER-001)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.worker import CommandRequest
from hermclaw.core.config import SandboxPolicy
from hermclaw.core.redaction import REDACTED
from worker.execution.sandbox import DockerSandbox, PodmanSandbox, make_sandbox, podman_info

IMAGE = "docker.io/library/alpine:3.20"


def _podman_ready() -> str | None:
    if shutil.which("podman") is None:
        return "podman not installed"
    rc = subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True, timeout=60, check=False).returncode
    return None if rc == 0 else f"image {IMAGE} not present locally (pull it through the proxy first)"


_SKIP = _podman_ready()
pytestmark = [pytest.mark.integration, pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")]


def policy(**kw: Any) -> SandboxPolicy:
    base: dict[str, Any] = {
        "engine": "podman",
        "image": IMAGE,
        "cpus": 1.0,
        "memory": "256m",
        "pids_limit": 64,
        "tmpfs_size": "16m",
        "default_timeout_seconds": 60,
        "env_allowlist": ["CI", "APP_*", "MY_API_TOKEN"],
    }
    base.update(kw)
    return SandboxPolicy(**base)


def sandbox(**kw: Any) -> PodmanSandbox:
    label = kw.pop("managed_label", "hermclaw.p18test=true")
    max_out = kw.pop("max_output_bytes", 100_000)
    return PodmanSandbox(policy(**kw.pop("policy_kw", {})), pull="never", managed_label=label, max_output_bytes=max_out, **kw)


def req(command: str, **kw: Any) -> CommandRequest:
    rid = kw.pop("request_id", f"p18-{uuid.uuid4().hex[:12]}")
    return CommandRequest(request_id=rid, job_id="job-p18", step_id="step-1", workspace="ws", command=command, **kw)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    (d / "input.txt").write_text("42\n")
    return d


def _exists(name: str) -> bool:
    return subprocess.run(["podman", "container", "exists", name], capture_output=True, check=False).returncode == 0


async def test_workspace_rw_rootfs_ro_tmp_rw(ws: Path) -> None:
    r = await sandbox().run(
        req(
            "cat input.txt; echo out > result.txt && echo WS_OK; "
            "touch /etc/hermclaw 2>/dev/null && echo ROOTFS_WRITABLE || echo ROOTFS_RO; "
            "echo t > /tmp/t && echo TMP_OK; pwd"
        ),
        ws,
    )
    assert r.exit_code == 0, r
    lines = r.stdout.split()
    assert lines[0] == "42" and "WS_OK" in lines and "ROOTFS_RO" in lines and "TMP_OK" in lines
    assert "ROOTFS_WRITABLE" not in lines and lines[-1] == "/workspace"
    assert (ws / "result.txt").read_text() == "out\n"  # written through the bind mount
    assert r.sandbox == "podman" and r.container_name and r.duration_ms > 0
    assert not _exists(r.container_name)  # --rm


async def test_network_off_by_default(ws: Path) -> None:
    r = await sandbox().run(
        req("cat /proc/net/route | wc -l; ls /sys/class/net; wget -q -T 3 -O /dev/null http://1.1.1.1/ && echo NET_OK || echo NET_FAIL"), ws
    )
    out = r.stdout.split()
    assert out[0] == "1"  # only the header line: no routes at all
    assert out[1:-1] == ["lo"]  # loopback only
    assert out[-1] == "NET_FAIL"


async def test_network_allowed_mode_has_route(ws: Path) -> None:
    r = await sandbox(allowed_network="slirp4netns").run(req("ip route | grep -c '^default'; ls /sys/class/net | grep -vc '^lo$'", network=True), ws)
    assert r.exit_code == 0, r
    assert r.stdout.split() == ["1", "1"]


async def test_timeout_kills_and_removes_container(ws: Path) -> None:
    s = sandbox()
    r = await s.run(req("echo started; sleep 60", timeout_seconds=2), ws)
    assert r.timed_out and r.exit_code is None and r.error and "timeout" in r.error
    assert r.duration_ms < 20_000
    assert "started" in r.stdout
    assert r.container_name and not _exists(r.container_name)
    assert s.active_containers() == {}


async def test_pids_limit_enforced(ws: Path) -> None:
    info = await podman_info()
    r = await sandbox(policy_kw={"pids_limit": 16}).run(req("for i in $(seq 1 40); do sleep 5 & done; wait; echo done"), ws)
    if not info.limits_effective:
        pytest.skip(f"cgroup limits not effective on this host: {info.warnings}")
    assert "can't fork" in r.stderr or "Resource temporarily unavailable" in r.stderr, r


async def test_memory_limit_enforced(ws: Path) -> None:
    info = await podman_info()
    r = await sandbox().run(req("head -c 200000000 /dev/zero | tail > /dev/null", memory="32m"), ws)
    if not info.limits_effective:
        pytest.skip(f"cgroup limits not effective on this host: {info.warnings}")
    assert r.exit_code == 137 and r.error and "out of memory" in r.error, r


async def test_capabilities_dropped_and_no_new_privileges(ws: Path) -> None:
    r = await sandbox().run(req("grep -E '^(CapEff|CapBnd|NoNewPrivs)' /proc/self/status"), ws)
    fields = dict(line.split(":\t") for line in r.stdout.strip().splitlines())
    assert fields["CapEff"] == "0000000000000000" and fields["CapBnd"] == "0000000000000000"
    assert fields["NoNewPrivs"] == "1"


async def test_only_allowlisted_env_reaches_container(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMCLAW_P18_HOST_SECRET", "host-only-value")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:proxy-password@proxy.invalid:3128")
    r = await sandbox().run(req("env", env={"APP_MODE": "test", "CI": "1"}), ws)
    assert r.exit_code == 0
    env = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
    assert env["APP_MODE"] == "test" and env["CI"] == "1" and env["HOME"] == "/tmp" and env["TMPDIR"] == "/tmp"
    assert "HERMCLAW_P18_HOST_SECRET" not in env
    assert not any("proxy" in k.lower() for k in env), sorted(env)  # --http-proxy=false
    assert "proxy-password" not in r.stdout
    denied = await sandbox().run(req("env", env={"LD_PRELOAD": "/x.so"}), ws)
    assert denied.exit_code is None and denied.error and denied.error.startswith("SANDBOX_ENV_NOT_ALLOWED")


async def test_output_limits_and_redaction(ws: Path) -> None:
    s = sandbox(max_output_bytes=4096)
    r = await s.run(req("head -c 100000 /dev/zero | tr '\\0' a; echo; echo END; echo token=supersecretvalue123 >&2"), ws)
    assert r.exit_code == 0 and r.stdout_truncated and not r.stderr_truncated
    assert "bytes omitted" in r.stdout and r.stdout.rstrip().endswith("END") and len(r.stdout) < 4096 + 100
    assert "supersecretvalue123" not in r.stderr and REDACTED in r.stderr
    secret = await sandbox().run(req("echo value=$MY_API_TOKEN", env={"MY_API_TOKEN": "abcd-1234-efgh"}), ws)
    assert "abcd-1234-efgh" not in secret.stdout and REDACTED in secret.stdout


async def test_exit_code_and_stderr(ws: Path) -> None:
    r = await sandbox().run(req("echo oops >&2; exit 7"), ws)
    assert r.exit_code == 7 and r.stderr.strip() == "oops" and r.error is None and not r.timed_out


async def test_cancellation_kills_and_removes_container(ws: Path) -> None:
    s = sandbox()
    request = req("sleep 60")
    task = asyncio.create_task(s.run(request, ws))
    name = f"hermclaw-{request.request_id}"
    for _ in range(100):
        if _exists(name):
            break
        await asyncio.sleep(0.1)
    assert _exists(name) and name in s.active_containers()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _exists(name) and s.active_containers() == {}


async def test_stale_leftover_with_same_name_is_replaced(ws: Path) -> None:
    label = "hermclaw.p18test=true"
    rid = f"p18-stale-{uuid.uuid4().hex[:8]}"
    name = f"hermclaw-{rid}"
    subprocess.run(["podman", "create", "--name", name, "--label", label, IMAGE, "true"], check=True, capture_output=True)
    try:
        r = await sandbox(managed_label=label).run(req("echo fresh", request_id=rid), ws)
        assert r.exit_code == 0 and r.stdout.strip() == "fresh", r
    finally:
        subprocess.run(["podman", "rm", "-f", "-i", name], capture_output=True, check=False)


async def test_foreign_container_with_same_name_is_not_touched(ws: Path) -> None:
    rid = f"p18-foreign-{uuid.uuid4().hex[:8]}"
    name = f"hermclaw-{rid}"
    subprocess.run(["podman", "create", "--name", name, "--label", "other.owner=1", IMAGE, "true"], check=True, capture_output=True)
    try:
        r = await sandbox().run(req("echo hi", request_id=rid), ws)
        assert r.exit_code == 125 and r.error and r.error.startswith("sandbox engine error"), r
        assert _exists(name)  # not ours -> left alone
    finally:
        subprocess.run(["podman", "rm", "-f", "-i", name], capture_output=True, check=False)


async def test_duplicate_running_request_rejected(ws: Path) -> None:
    s = sandbox()
    request = req("sleep 3")
    first = asyncio.create_task(s.run(request, ws))
    for _ in range(100):
        if s.active_containers():
            break
        await asyncio.sleep(0.05)
    second = await s.run(request, ws)
    assert second.exit_code is None and second.error and second.error.startswith("SANDBOX_REQUEST_ACTIVE")
    assert (await first).exit_code == 0


async def test_concurrent_commands_are_isolated(ws: Path, tmp_path: Path) -> None:
    s = sandbox()
    other = tmp_path / "other"
    other.mkdir()
    r1, r2 = await asyncio.gather(s.run(req("echo one > f.txt; ls"), ws), s.run(req("echo two > f.txt; ls"), other))
    assert r1.exit_code == r2.exit_code == 0
    assert (ws / "f.txt").read_text() == "one\n" and (other / "f.txt").read_text() == "two\n"
    assert "input.txt" in r1.stdout and "input.txt" not in r2.stdout  # each sees only its own workspace


async def test_docker_adapter_flags_run_on_podman_cli(ws: Path) -> None:
    """The Docker adapter's flag set is docker-CLI compatible; podman's CLI accepts it, so it is run for real."""
    s = DockerSandbox(policy(), executable="podman", pull="never", managed_label="hermclaw.p18test=true", max_output_bytes=10_000)
    r = await s.run(req("echo d > docker.txt; grep NoNewPrivs /proc/self/status; touch /usr/x 2>/dev/null || echo RO"), ws)
    assert r.exit_code == 0, r
    assert "NoNewPrivs:\t1" in r.stdout and "RO" in r.stdout and r.sandbox == "docker"
    assert (ws / "docker.txt").read_text() == "d\n"


async def test_ensure_images_and_health() -> None:
    s = sandbox(extra_images=["docker.io/library/alpine:0.0-hermclaw-missing"])
    report = await s.ensure_images()
    assert report[IMAGE] == "present"
    assert report["docker.io/library/alpine:0.0-hermclaw-missing"].startswith("error")
    info = await s.health()
    assert info.available and info.version and info.cgroup_version in ("v1", "v2")
    assert isinstance(info.limits_effective, bool) and info.as_dict()["engine"] == "podman"


async def test_make_sandbox_default_runner_end_to_end(ws: Path) -> None:
    runner = make_sandbox(policy(), environment="test", pull="never", managed_label="hermclaw.p18test=true", max_output_bytes=10_000)
    r = await runner.run(req("cat input.txt"), ws)
    assert r.exit_code == 0 and r.stdout.strip() == "42"
