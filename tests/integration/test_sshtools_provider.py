"""P26 – SSH tools behind the tool engine's operations-tool protocol (``SshOpsProvider``) against a real sshd."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from hermclaw.contracts.tools import ToolName
from hermclaw.core.config import load_config
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.sshtools.approvals import grant_approval
from hermclaw.sshtools.errors import SshPolicyError
from hermclaw.sshtools.provider import SshOpsProvider, resolve_workspace_file
from hermclaw.tools.ops import OpsCall
from hermclaw.tools.registry import SshCommandArgs, SshReadArgs, SshServiceStatusArgs, SshUploadArgs
from tests.integration.test_sshtools_sshd import SshdServer, _events, _job, _runs, build_tools, start_sshd

pytestmark = pytest.mark.integration

FORBIDDEN = load_config().policies.scope.always_forbidden


@pytest.fixture(scope="module")
def sshd(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SshdServer]:
    server = start_sshd(tmp_path_factory.mktemp("sshd-provider"))
    yield server
    server.stop()


def _workspace(root: Path, job_id: uuid.UUID) -> WorkspaceHandle:
    return WorkspaceHandle(
        id=uuid.uuid4(), job_id=job_id, path=root, branch="b", base_branch="main", base_sha="0" * 40, repository_key="repo"
    )


def _call(tool: ToolName, args: object, ws: WorkspaceHandle, *, capability: str = "deploy", step_id: uuid.UUID | None = None) -> OpsCall:
    return OpsCall(tool, args, ws, capability, ws.job_id, step_id or uuid.uuid4(), uuid.uuid4(), 1)  # type: ignore[arg-type]


async def test_provider_dispatches_all_ssh_tools(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    provider = SshOpsProvider(build_tools(sshd, sessionmaker, tmp_path), forbidden_upload_globs=FORBIDDEN)
    assert all(provider.provides(t) for t in (ToolName.ssh_read, ToolName.ssh_command, ToolName.ssh_upload, ToolName.ssh_service_status))
    assert not provider.provides(ToolName.db_introspect)
    job_id = await _job(sessionmaker)
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    ws = _workspace(ws_root, job_id)

    read = await provider.call(_call(ToolName.ssh_read, SshReadArgs(host="lab", command="ls -d /"), ws))
    assert read.ok and "--- stdout ---\n/" in read.output and read.data["outcome"] == "ok"

    step = uuid.uuid4()
    target = tmp_path / "provider-touch"
    cmd = SshCommandArgs(host="lab", command=f"touch {target}")
    first = await provider.call(_call(ToolName.ssh_command, cmd, ws, step_id=step))
    assert not first.ok and first.error_code == "SSH_APPROVAL_REQUIRED" and first.data["approval"]
    async with sessionmaker() as s, s.begin():  # type: ignore[operator]
        await grant_approval(s, job_id, first.data["approval"], approved_by="op")
    cmd = SshCommandArgs(host="lab", command=f"touch {target}", approval_token=first.data["approval"])
    second = await provider.call(_call(ToolName.ssh_command, cmd, ws, step_id=step))
    assert second.ok and target.exists()

    status = await provider.call(_call(ToolName.ssh_service_status, SshServiceStatusArgs(host="lab", unit="nginx"), ws))
    assert status.ok and status.data["active_state"] == "active" and "active_state: active" in status.output

    denied = await provider.call(_call(ToolName.ssh_read, SshReadArgs(host="lab", command="uptime"), ws, capability="implement"))
    assert not denied.ok and denied.error_code == "SSH_CAPABILITY_DENIED"


async def test_provider_upload_from_workspace(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    provider = SshOpsProvider(build_tools(sshd, sessionmaker, tmp_path, require_approval=False), forbidden_upload_globs=FORBIDDEN)
    job_id = await _job(sessionmaker)
    ws_root = tmp_path / "ws"
    (ws_root / "deploy").mkdir(parents=True)
    (ws_root / "deploy" / "site.conf").write_text("server {}\n")
    (ws_root / ".env").write_text("DB_PASSWORD=wsenv-Secret-4711\n")
    (ws_root / "deploy" / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (ws_root / "deploy" / "escape").symlink_to(outside)
    (ws_root / "deploy" / "envlink").symlink_to(ws_root / ".env")
    (ws_root / ".git").mkdir()
    (ws_root / ".git" / "config").write_text("[core]\n")
    ws = _workspace(ws_root, job_id)
    remote = tmp_path / "remote"
    remote.mkdir()

    step = uuid.uuid4()
    ok = await provider.call(
        _call(
            ToolName.ssh_upload,
            SshUploadArgs(host="lab", path="deploy/site.conf", remote_path=str(remote / "site.conf"), mode="0600"),
            ws,
            step_id=step,
        )
    )
    assert ok.ok, ok.output
    assert (remote / "site.conf").read_text() == "server {}\n" and (os.stat(remote / "site.conf").st_mode & 0o777) == 0o600

    for path in (".env", "deploy/key.pem", "deploy/escape", "deploy/envlink", ".git/config", "../outside.txt", "deploy/missing"):
        res = await provider.call(
            _call(ToolName.ssh_upload, SshUploadArgs(host="lab", path=path, remote_path=str(remote / "x")), ws, step_id=step)
        )
        assert not res.ok and res.error_code == "SSH_UPLOAD_INVALID", path
    assert not (remote / "x").exists()
    assert len(await _runs(sessionmaker, step)) == 1
    refusals = [e for e in await _events(sessionmaker, step) if e.payload["outcome"] == "denied"]
    assert len(refusals) == 7 and all("wsenv-Secret-4711" not in str(e.payload) for e in refusals)
    assert all("PRIVATE KEY" not in str(e.payload) for e in refusals)  # file contents never reach the audit


def test_resolve_workspace_file_rules(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.txt").write_text("x")
    assert resolve_workspace_file(tmp_path, "a/f.txt") == (tmp_path / "a" / "f.txt").resolve()
    assert resolve_workspace_file(tmp_path, "./a/f.txt") == (tmp_path / "a" / "f.txt").resolve()
    for bad in ("", "./", "/etc/passwd", "a/../../x", "a//f.txt", "a/./f.txt", ".git/HEAD", "a/.git/x", "a\x00b", "..\\x"):
        with pytest.raises(SshPolicyError):
            resolve_workspace_file(tmp_path, bad)
    with pytest.raises(SshPolicyError):
        resolve_workspace_file(tmp_path, "a/f.txt", forbidden_globs=["**/*.txt"])
