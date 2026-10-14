"""P26 live checks against the real LAN hosts of ``config/hosts.yaml`` (BLOCKER-001: not reachable from the build
container). Run on the orchestrator with ``pytest -m live tests/integration/test_sshtools_live.py`` after the admin key,
``known_hosts`` and (optionally) the ``hermclaw-ssh-gate`` forced command are installed (docs/architecture/sshtools.md)."""

from __future__ import annotations

import uuid

import pytest

from hermclaw.core.config import load_config
from hermclaw.core.settings import get_settings
from hermclaw.sshtools.registry import HostRegistry
from hermclaw.sshtools.separation import sandbox_isolation_issues
from hermclaw.sshtools.tools import SshCallContext, SshOutcome, SshTools

pytestmark = [pytest.mark.live, pytest.mark.integration]


def _allowed() -> list[str]:
    try:
        return HostRegistry.from_config(load_config()).ids()
    except Exception:  # no deployable config in this checkout
        return []


@pytest.mark.parametrize("host_id", _allowed() or ["<no allow-listed host>"])
async def test_live_read_and_service_status(host_id: str, sessionmaker: object) -> None:
    if host_id.startswith("<"):
        pytest.skip("policies.ssh.allowed_hosts is empty")
    config = load_config()
    tools = SshTools.from_config(config, sessionmaker)  # type: ignore[arg-type]
    ctx = SshCallContext(step_id=uuid.uuid4(), capability="ssh", actor="runtime")
    res = await tools.ssh_read(host_id, "uptime", ctx=ctx, timeout=30)
    assert res.outcome == SshOutcome.ok, res.render()
    status = await tools.ssh_service_status(host_id, "ssh", ctx=ctx, timeout=30)
    assert status.outcome in (SshOutcome.ok, SshOutcome.failed), status.render()


def test_live_config_keeps_ssh_material_out_of_the_sandbox() -> None:
    config = load_config()
    assert sandbox_isolation_issues(config, workspaces_dir=get_settings().workspaces_dir) == []
