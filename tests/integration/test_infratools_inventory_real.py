"""P28.1/P28.4 – real container inventory (podman) and live port collisions on this machine (``ss``)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator

import pytest

from hermclaw.core.config import HostConfig, HostsConfig
from hermclaw.infratools import InfraTools
from hermclaw.infratools.errors import INFRA_CONTAINER_NOT_FOUND, InfraToolError
from hermclaw.infratools.ports import PortChecker, PortPlan
from hermclaw.infratools.process import HostExecutor, LocalRunner

pytestmark = pytest.mark.integration
ALPINE = "docker.io/library/alpine:3.20"
HAVE_PODMAN = shutil.which("podman") is not None and (
    subprocess.run(["podman", "image", "exists", ALPINE], capture_output=True, check=False).returncode == 0
)
HOSTS = HostsConfig(hosts=[HostConfig(id="exec-222", address="127.0.0.1", role="execution_worker")])


@pytest.fixture
def container() -> Iterator[tuple[str, int]]:
    if not HAVE_PODMAN:
        pytest.skip("podman or the alpine test image missing")
    name = f"hermclaw-inv-{uuid.uuid4().hex[:8]}"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run(
        ["podman", "run", "-d", "--name", name, "--label", "app=inventory-test", "--label", "secret_token=abcdefgh12345",
         "-e", "DB_PASSWORD=hunter2hunter2", "-p", f"127.0.0.1:{port}:80", ALPINE, "sleep", "120"],
        capture_output=True, check=True,
    )  # fmt: skip
    yield name, port
    subprocess.run(["podman", "rm", "-f", "-t", "0", name], capture_output=True, check=False)


@pytest.fixture
def listener() -> Iterator[int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        yield s.getsockname()[1]


async def test_container_list_and_inspect_with_podman(container: tuple[str, int]) -> None:
    name, port = container
    tools = InfraTools(HostExecutor())
    listed = await tools.container_list(engine="auto", filters=("label=app=inventory-test",))
    assert listed.ok, listed.error
    (info,) = [c for c in listed.containers if c.name == name]
    assert info.engine == "podman" and info.state == "running" and info.image.endswith("alpine:3.20")
    assert any(p.host_port == port and p.host_ip == "127.0.0.1" for p in info.ports)
    assert info.labels["app"] == "inventory-test" and info.labels["secret_token"] != "abcdefgh12345"
    docker = next(s for s in listed.engines if s.engine == "docker")
    assert docker.available is False or docker.error is None  # no daemon in the build container → reported, not fatal
    detail = await tools.container_inspect(name)
    assert detail.env_keys and "DB_PASSWORD" in detail.env_keys
    assert "hunter2hunter2" not in detail.model_dump_json()
    assert detail.command == "sleep 120" and detail.network_mode
    with pytest.raises(InfraToolError) as err:
        await tools.container_inspect("hermclaw-does-not-exist-xyz")
    assert err.value.code == INFRA_CONTAINER_NOT_FOUND


async def test_container_inventory_through_remote_runner(container: tuple[str, int]) -> None:
    """The remote path (SSH runner contract, redacting transport) against this machine."""
    name, _ = container
    tools = InfraTools(HostExecutor(remote=LocalRunner(), hosts=HOSTS))
    listed = await tools.container_list(host="exec-222", engine="podman", filters=(f"name={name}",))
    assert listed.ok and [c.name for c in listed.containers] == [name] and listed.host == "exec-222"
    detail = await tools.container_inspect(name, host="exec-222", engine="podman")
    assert detail.name == name and detail.host == "exec-222"


async def test_live_port_collision_with_owner_info(listener: int) -> None:
    checker = PortChecker(HostExecutor(), plan=PortPlan(()))
    text = f"services:\n  web:\n    image: nginx:1.28\n    ports: ['127.0.0.1:{listener}:80']\n"
    report = await checker.check(text)
    assert report.live_checked and not report.ok
    (collision,) = report.collisions
    assert collision.kind == "live" and collision.port == listener
    assert collision.owner.startswith("python") and collision.pid  # ss -p as root shows the owning process
    owned = await checker.check(text, owner=collision.owner)
    assert owned.ok, owned.render()
    free = await checker.check("services:\n  web:\n    image: nginx:1.28\n    ports: ['127.0.0.1:1:80']\n")
    assert free.ok


async def test_live_ports_through_remote_runner(listener: int) -> None:
    checker = PortChecker(HostExecutor(remote=LocalRunner(), hosts=HOSTS), hosts=HOSTS, plan=PortPlan(()))
    report = await checker.check(f"[Socket]\nListenStream=127.0.0.1:{listener}\n", host="exec-222")
    assert report.address == "127.0.0.1" and [c.port for c in report.collisions] == [listener]


async def test_predeploy_finds_ports_published_by_containers(container: tuple[str, int]) -> None:
    """Rootful podman publishes through netavark DNAT rules – no socket shows up in ss; the inventory catches it."""
    name, port = container
    tools = InfraTools(HostExecutor(), port_plan=PortPlan(()))
    text = f"services:\n  other:\n    image: docker.io/library/nginx:1.28\n    ports: ['127.0.0.1:{port}:80']\n"
    report = await tools.predeploy_check("compose", text)
    assert any(f.code == "PORT_USED_BY_CONTAINER" and name in f.message for f in report.findings), report.render()
    assert not report.ok
    owned = await tools.predeploy_check("compose", text, owner=name)
    assert not any(f.code == "PORT_USED_BY_CONTAINER" for f in owned.findings)
