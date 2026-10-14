"""P28.2/P28.3 – compose validation with the real Compose CLIs and real sandbox runs (podman + podman-compose).

The build container has Docker Compose v5 (validation works without a daemon), Podman 4.9 and podman-compose 1.0.6;
the Docker daemon is not running – the runtime adapter falls back to Podman and a Docker-only preference is reported
as ``skipped``. Images used: docker.io/library/alpine:3.20 and docker.io/library/nginx:1.28 (pre-pulled).
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from hermclaw.core.config import HostConfig, HostsConfig
from hermclaw.infratools import InfraTools
from hermclaw.infratools.compose import SANDBOX_LABEL, ComposeEngines, ComposeValidator
from hermclaw.infratools.compose_sandbox import ComposeSandbox, SandboxOptions, cleanup_stale_sandboxes
from hermclaw.infratools.errors import INFRA_COMPOSE_REFUSED, INFRA_SANDBOX_UNAVAILABLE
from hermclaw.infratools.process import HostExecutor, LocalRunner

ALPINE = "docker.io/library/alpine:3.20"
NGINX = "docker.io/library/nginx:1.28"


def _have_image(image: str) -> bool:
    if shutil.which("podman") is None:
        return False
    return subprocess.run(["podman", "image", "exists", image], capture_output=True, check=False).returncode == 0


PODMAN_OK = shutil.which("podman-compose") is not None and _have_image(ALPINE) and _have_image(NGINX)
needs_podman = pytest.mark.skipif(not PODMAN_OK, reason="podman/podman-compose or the pre-pulled test images are missing")
needs_docker_compose = pytest.mark.skipif(
    subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode != 0 if shutil.which("docker") else True,
    reason="docker compose plugin missing",
)
pytestmark = [pytest.mark.integration, pytest.mark.slow]
FAST = SandboxOptions(settle_seconds=1.0, poll_interval=0.5, teardown_timeout=60, stop_timeout=1)


def _leftovers(sandbox_label: str = SANDBOX_LABEL) -> list[str]:
    out = subprocess.run(["podman", "ps", "-aq", "--filter", f"label={sandbox_label}"], capture_output=True, text=True, check=False)
    nets = subprocess.run(["podman", "network", "ls", "--format", "{{.Name}}"], capture_output=True, text=True, check=False)
    return out.stdout.split() + [n for n in nets.stdout.split() if n.startswith("hermclaw-sbx-")]


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "proj"
    (root / "site").mkdir(parents=True)
    (root / "site" / "index.html").write_text("<h1>sandbox</h1>\n")
    (root / "app.env").write_text("GREETING=hello-from-env\n")
    (root / "compose.yaml").write_text(
        f"""
services:
  web:
    image: {NGINX}
    container_name: fixed-web
    restart: always
    ports: ["8080:80"]
    volumes: ["./site:/usr/share/nginx/html:ro"]
    healthcheck:
      test: ["CMD-SHELL", "nginx -t || exit 1"]
      interval: 2s
      timeout: 3s
      retries: 3
  worker:
    image: {ALPINE}
    env_file: [app.env]
    command: ["sh", "-c", "echo $$GREETING; mkdir -p /out && echo done > /out/result.txt; sleep 60"]
    volumes: ["./out:/out"]
"""
    )
    yield root
    subprocess.run(["podman", "rm", "-f", "fixed-web"], capture_output=True, check=False)


def _write(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "compose.yaml"
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------------------------- validation
@needs_docker_compose
async def test_engine_detection_and_schema_validation(project: Path, tmp_path: Path) -> None:
    executor = HostExecutor()
    engines = ComposeEngines(executor)
    found = await engines.detect()
    labels = [e.label for e in found]
    assert any(label.startswith("docker compose") for label in labels)
    validator = ComposeValidator(executor, engines)
    good = await validator.validate(project / "compose.yaml")
    assert good.ok and good.engine_check.status == "passed" and good.engine_check.engine and "docker compose" in good.engine_check.engine
    assert [p.published for p in good.ports] == [8080]
    # an unknown service property passes our structural rules but fails the Compose-Specification schema
    bad = await validator.validate(_write(tmp_path / "bad", f"services:\n  web:\n    image: {ALPINE}\n    imagee: typo\n"))
    assert bad.engine_check.status == "failed" and "imagee" in (bad.engine_check.reason or "") and not bad.ok
    assert "FAILED" in bad.render()


async def test_engine_check_skipped_without_compose_cli(project: Path) -> None:
    executor = HostExecutor(env={"PATH": "/nonexistent"})
    report = await ComposeValidator(executor, ComposeEngines(executor)).validate(project / "compose.yaml")
    assert report.engine_check.status == "skipped" and "no compose implementation" in (report.engine_check.reason or "")
    assert report.ok  # the structural check still ran and found nothing blocking


# ---------------------------------------------------------------------------------------------- sandbox
@needs_podman
async def test_sandbox_up_healthy_remapped_ports_logs_and_teardown(project: Path) -> None:
    tools = InfraTools(HostExecutor(), sandbox_options=FAST)
    report = await tools.compose_up_sandbox(project / "compose.yaml", timeout=120)
    assert report.status == "passed", report.render()
    assert report.ok and report.engine and report.engine.startswith("podman-compose")
    assert report.project.startswith("hermclaw-sbx-")
    web = next(s for s in report.services if s.service == "web")
    worker = next(s for s in report.services if s.service == "worker")
    assert web.health == "healthy" and web.container != "fixed-web"  # container_name dropped
    assert "hello-from-env" in worker.logs
    (port,) = report.ports
    assert port.original == "8080:80" and port.host_port != 8080 and port.reachable is True
    assert report.teardown is not None and report.teardown.ok and not report.teardown.leftovers
    assert not (project / "out").exists()  # the staged copy was used, the workspace is untouched
    assert _leftovers() == []
    with socket.socket() as probe:  # nothing listens on the remapped port any more
        assert probe.connect_ex(("127.0.0.1", port.host_port)) != 0


@needs_podman
async def test_sandbox_failing_container_reports_logs(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "fail", f"services:\n  broken:\n    image: {ALPINE}\n    command: ['sh', '-c', 'echo boom-on-stderr >&2; exit 3']\n"
    )
    report = await ComposeSandbox(HostExecutor(), ComposeEngines(HostExecutor()), options=FAST).run(path, timeout=60)
    assert report.status == "failed" and "exited with code 3" in (report.reason or ""), report.render()
    assert any("boom-on-stderr" in s.logs for s in report.services)
    assert report.teardown is not None and report.teardown.ok and _leftovers() == []


@needs_podman
async def test_sandbox_unhealthy_and_timeout(tmp_path: Path) -> None:
    sandbox = ComposeSandbox(HostExecutor(), ComposeEngines(HostExecutor()), options=FAST)
    unhealthy = _write(
        tmp_path / "unhealthy",
        f"services:\n  sick:\n    image: {ALPINE}\n    command: ['sleep', '60']\n"
        "    healthcheck: {test: ['CMD', 'false'], interval: 1s, timeout: 2s, retries: 1}\n",
    )
    report = await sandbox.run(unhealthy, timeout=60)
    assert report.status == "failed" and "unhealthy" in (report.reason or ""), report.render()
    slow = _write(
        tmp_path / "slow",
        f"services:\n  slow:\n    image: {ALPINE}\n    command: ['sleep', '60']\n"
        "    healthcheck: {test: ['CMD', 'false'], interval: 1s, timeout: 2s, retries: 1000}\n",
    )
    report = await sandbox.run(slow, timeout=12)
    assert report.status == "timeout" and "starting" in (report.reason or ""), report.render()
    assert report.teardown is not None and report.teardown.ok and _leftovers() == []


@needs_podman
async def test_sandbox_refuses_before_starting_anything(tmp_path: Path) -> None:
    sandbox = ComposeSandbox(HostExecutor(), ComposeEngines(HostExecutor()), options=FAST)
    for text in (
        f"services:\n  a:\n    image: {ALPINE}\n    privileged: true\n",
        f"services:\n  a:\n    image: {ALPINE}\n    network_mode: host\n",
        f"services:\n  a:\n    image: {ALPINE}\n    volumes: ['/var/run/docker.sock:/s']\n",
        f"services:\n  a:\n    image: {ALPINE}\n    volumes: ['/srv:/srv']\n",
        f"services:\n  a:\n    image: {ALPINE}\n    env_file: /etc/hostname\n",
        f"services:\n  a:\n    image: {ALPINE}\n    profiles: [never]\n",
    ):
        report = await sandbox.run(_write(tmp_path / "refused", text), timeout=30)
        assert report.status == "refused" and report.code == INFRA_COMPOSE_REFUSED, (text, report.render())
        assert not report.services and report.up_output == ""
    assert _leftovers() == []


@needs_podman
async def test_sandbox_docker_only_preference_is_skipped(project: Path) -> None:
    options = SandboxOptions(engine_preference=("docker",))
    report = await ComposeSandbox(HostExecutor(), ComposeEngines(HostExecutor()), options=options).run(project / "compose.yaml", timeout=30)
    if report.status != "skipped":  # a running Docker daemon would make this a real run
        pytest.skip("docker daemon available")
    assert report.code == INFRA_SANDBOX_UNAVAILABLE and "docker" in (report.reason or "").lower()


@needs_podman
async def test_sandbox_on_registered_host_via_remote_runner(project: Path) -> None:
    """Remote code path (tar staging over stdin, mktemp, ss port allocation, shlex-quoted commands) against this machine."""
    hosts = HostsConfig(hosts=[HostConfig(id="exec-222", address="127.0.0.1", role="execution_worker")])
    runner = LocalRunner()
    executor = HostExecutor(remote=runner, hosts=hosts)
    report = await ComposeSandbox(executor, ComposeEngines(executor), options=FAST).run(
        project / "compose.yaml", timeout=120, host="exec-222"
    )
    assert report.status == "passed", report.render()
    assert report.host == "exec-222" and 20000 <= report.ports[0].host_port < 32000
    assert any(c.startswith("mktemp -d /tmp/hermclaw-sbx-") for c in runner.commands)
    assert any(c.startswith("tar -xzf - --no-same-owner -C /tmp/hermclaw-sbx-") for c in runner.commands)
    assert any(c.startswith("rm -rf -- /tmp/hermclaw-sbx-") for c in runner.commands)
    assert not list(Path("/tmp").glob(f"hermclaw-sbx-{report.project.removeprefix('hermclaw-sbx-')}-*"))
    assert report.teardown is not None and report.teardown.ok and _leftovers() == []


@needs_podman
async def test_cancelled_sandbox_still_tears_down(project: Path) -> None:
    sandbox = ComposeSandbox(HostExecutor(), ComposeEngines(HostExecutor()), options=SandboxOptions(settle_seconds=30, poll_interval=0.5))
    task = asyncio.create_task(sandbox.run(project / "compose.yaml", timeout=120))
    for _ in range(240):
        await asyncio.sleep(0.25)
        if _leftovers():
            break
    assert _leftovers(), "sandbox containers never appeared"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _leftovers() == []


@needs_podman
async def test_cleanup_stale_sandboxes_removes_labelled_containers() -> None:
    name = "hermclaw-stale-probe"
    subprocess.run(["podman", "rm", "-f", name], capture_output=True, check=False)
    subprocess.run(
        ["podman", "create", "--name", name, "--label", f"{SANDBOX_LABEL}=stale", ALPINE, "true"], capture_output=True, check=True
    )
    removed = await cleanup_stale_sandboxes(HostExecutor(), engines=("podman",))
    assert removed and _leftovers() == []
