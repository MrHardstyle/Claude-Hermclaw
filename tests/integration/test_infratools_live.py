"""P28 live acceptance against the real hosts (BLOCKER-001: the LAN 192.168.178.0/24 is unreachable from the build
environment). Run on the orchestrator with ``pytest -m live tests/integration/test_infratools_live.py``."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import load_config
from hermclaw.infratools import InfraTools

pytestmark = [pytest.mark.live, pytest.mark.integration]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def tools(sessionmaker: async_sessionmaker[AsyncSession]) -> InfraTools:
    from hermclaw.sshtools.tools import SshTools

    config = load_config()
    ssh = SshTools.from_config(config, sessionmaker, compound_allowlist=config.policies.ssh.compound_allowlist)
    return InfraTools.create(hosts=config.hosts, remote=ssh.runner, sessionmaker=sessionmaker)


async def test_live_inventory_and_ports_on_execution_worker(tools: InfraTools) -> None:
    listed = await tools.container_list(host="exec-222", all_containers=True)
    assert listed.ok, listed.error
    report = await tools.port_check("[Socket]\nListenStream=8787\n", kind="systemd", host="exec-222", owner="execution-worker")
    assert report.live_checked, report.render()


async def test_live_nginx_config_test_on_webui(tools: InfraTools) -> None:
    """``sudo -n nginx -t`` as the runtime user. The bootstrap gives hermclaw-admin no sudo rule on the shared .223
    (R-038; the deployment runs ``nginx -t`` as root), and without sudo ``nginx -t`` cannot open the logs, the pid
    file or the TLS key - so the test only runs where an operator granted exactly that command."""
    probe = await tools.executor.run(["sudo", "-n", "-l", "/usr/sbin/nginx", "-t"], host="webui-223", timeout=30)
    if not probe.ok:
        pytest.skip(f"the runtime user may not run 'sudo -n /usr/sbin/nginx -t' on webui-223: {probe.describe(200)}")
    tested = await tools.nginx.test_host("webui-223", sudo=True)
    assert tested.ok, tested.render()


async def test_live_nginx_on_webui(tools: InfraTools) -> None:
    site = (ROOT / "infra/ansible/roles/nginx/templates/hermclaw-next.conf.j2").read_text()
    assert "events/stream" in site
    gate = await tools.predeploy_check("compose", "services:\n  a:\n    image: nginx:1.28\n    ports: ['80:80']\n", host="webui-223")
    assert not gate.ok  # port 80 belongs to the legacy portal (Apache) on the shared .223 (D-013)
    assert gate.ports is not None and any(c.owner in {"legacy-portal", "apache2"} for c in gate.ports.collisions), gate.render()
    # Hermclaw's own nginx on its port (hosts.yaml, hermclaw_webui_http_port) is the expected owner
    port = next(s.port for s in tools.hosts.by_id("webui-223").services if s.name == "nginx") if tools.hosts else 8080
    own = await tools.port_check(f"server {{ listen {port}; }}", kind="nginx", host="webui-223", owner="nginx")
    assert own.live_checked and not own.collisions, own.render()


async def test_live_sandbox_on_execution_worker(tools: InfraTools, tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  once:\n    image: docker.io/library/alpine:3.20\n    command: ['echo', 'live-ok']\n")
    report = await tools.compose_up_sandbox(compose, timeout=300, host="exec-222")
    assert report.ok, report.render()
