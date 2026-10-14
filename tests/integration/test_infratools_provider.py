"""P28 – the container/proxy tools behind the tool engine's operations protocol (``hermclaw.infratools.provider``)."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.tools import CONTAINER_TOOLS, ToolName
from hermclaw.core.config import load_config
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.core.settings import Settings
from hermclaw.infratools.errors import INFRA_PATH_INVALID
from hermclaw.infratools.provider import CAPABILITY_REFUSED, InfraOpsProvider, build_provider, default_execution_host
from hermclaw.tools.ops import OpsCall
from hermclaw.tools.registry import ComposeArgs, ConfigSourceArgs, ContainerInspectArgs, ContainerQueryArgs

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
ALPINE = "docker.io/library/alpine:3.20"
HAVE_PODMAN = (
    shutil.which("podman-compose") is not None
    and subprocess.run(["podman", "image", "exists", ALPINE], capture_output=True, check=False).returncode == 0
)


@pytest.fixture
async def provider(sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path) -> InfraOpsProvider:
    settings = Settings(env="test", data_dir=tmp_path / "data", execution_mode="local")
    return build_provider(load_config(ROOT / "config"), sessionmaker, settings)


def workspace(tmp_path: Path) -> WorkspaceHandle:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    return WorkspaceHandle(uuid.uuid4(), uuid.uuid4(), root, "hermclaw/x", "main", "0" * 40, "repo")


def call(tool: ToolName, args: BaseModel, ws: WorkspaceHandle, capability: str | None = "docker") -> OpsCall:
    return OpsCall(tool, args, ws, capability, None, None, None, 1)


async def test_build_provider_and_default_host(provider: InfraOpsProvider) -> None:
    assert all(provider.provides(t) for t in CONTAINER_TOOLS) and not provider.provides(ToolName.ssh_command)
    assert provider.default_host is None
    config = load_config(ROOT / "config")
    prod = Settings(env="production", execution_mode="auto")
    assert default_execution_host(config.hosts, prod, remote_available=True) == "exec-222"
    assert default_execution_host(config.hosts, prod, remote_available=False) is None
    assert default_execution_host(config.hosts, Settings(env="test", execution_mode="auto"), remote_available=True) is None
    assert callable(provider.tools.cleanup_orphan_sandboxes)  # type: ignore[attr-defined]


async def test_capability_gate_is_enforced_again(provider: InfraOpsProvider, tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    for capability in ("ssh", "database", None, "code"):
        res = await provider.call(call(ToolName.container_list, ContainerQueryArgs(), ws, capability))
        assert not res.ok and res.error_code == CAPABILITY_REFUSED


async def test_compose_validate_inside_workspace_only(provider: InfraOpsProvider, tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws.path / "deploy").mkdir()
    (ws.path / "deploy" / "compose.yaml").write_text(f"services:\n  a:\n    image: {ALPINE}\n    ports: ['8081:80']\n")
    res = await provider.call(call(ToolName.compose_validate, ComposeArgs(path="deploy/compose.yaml"), ws, "deploy"))
    assert res.ok, res.output
    assert "compose_validate" in res.output and res.data["ports"][0]["published"] == 8081
    (tmp_path / "secret.yaml").write_text("services: {}\n")
    os.symlink(tmp_path / "secret.yaml", ws.path / "link.yaml")
    for bad in ("../secret.yaml", "/etc/hostname", "link.yaml", "missing.yaml"):
        res = await provider.call(call(ToolName.compose_validate, ComposeArgs(path=bad), ws))
        assert not res.ok and res.error_code == INFRA_PATH_INVALID, bad


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx not installed")
async def test_nginx_tools_from_text_and_file(provider: InfraOpsProvider, tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws.path / "nginx.conf").write_text("server {\n  listen 80;\n  location /api/events/stream { proxy_pass http://127.0.0.1:8000; }\n}\n")
    tested = await provider.call(call(ToolName.nginx_test, ConfigSourceArgs(path="nginx.conf"), ws))
    assert tested.ok and "nginx_test nginx.conf" in tested.output
    proxy = await provider.call(call(ToolName.proxy_config_validate, ConfigSourceArgs(path="nginx.conf"), ws))
    assert not proxy.ok and "SSE_BUFFERING" in proxy.output and proxy.error_code == "INFRA_PROXY_INVALID"
    bad = await provider.call(call(ToolName.nginx_test, ConfigSourceArgs(config="server { listen 80; bogus_directive on; }"), ws))
    assert not bad.ok and "bogus_directive" in bad.output


@pytest.mark.skipif(not HAVE_PODMAN, reason="podman-compose or the alpine test image missing")
async def test_container_tools_and_sandbox(provider: InfraOpsProvider, tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    listed = await provider.call(call(ToolName.container_list, ContainerQueryArgs(engine="podman", all=True), ws))
    assert listed.ok and listed.output.startswith("containers on local")
    missing = await provider.call(call(ToolName.container_inspect, ContainerInspectArgs(name="hermclaw-no-such-container"), ws))
    assert not missing.ok and missing.error_code == "INFRA_CONTAINER_NOT_FOUND"
    (ws.path / "compose.yaml").write_text(f"services:\n  once:\n    image: {ALPINE}\n    command: ['echo', 'sandbox-ok']\n")
    res = await provider.call(call(ToolName.compose_up_sandbox, ComposeArgs(path="compose.yaml", timeout_seconds=60), ws))
    assert res.ok, res.output
    assert "sandbox-ok" in res.output and res.data["teardown"]["ok"]
