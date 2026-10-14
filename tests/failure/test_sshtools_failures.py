"""P26 failure modes of the SSH/admin tools against a REAL local sshd: dead/unreachable hosts, missing client,
insecure keys, missing host keys, connections dropped by the host, audit failures – and that approvals survive
pre-flight failures (an approval is only consumed when the command is actually started)."""

from __future__ import annotations

import os
import shutil
import socket
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from hermclaw.core.config import HostConfig, SshConfig
from hermclaw.sshtools import tools as tools_module
from hermclaw.sshtools.approvals import approval_state, grant_approval
from hermclaw.sshtools.policy import approval_token
from hermclaw.sshtools.tools import SshOutcome
from tests.integration.test_sshtools_sshd import SshdServer, _ctx, _events, _job, _runs, build_tools, start_sshd

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (shutil.which("sshd") or Path("/usr/sbin/sshd").exists()), reason="OpenSSH server not installed"),
]


@pytest.fixture(scope="module")
def sshd(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SshdServer]:
    server = start_sshd(tmp_path_factory.mktemp("sshd-failures"))
    yield server
    server.stop()


def _dead_port_host(server: SshdServer) -> HostConfig:
    """A host entry whose port has no listener (the host is 'down'); the host key is pinned for that port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    pinned = server.root / f"known_hosts_{port}"
    pub = (server.root / "host_key.pub").read_text().split()
    pinned.write_text(f"[127.0.0.1]:{port} {pub[0]} {pub[1]}\n", encoding="utf-8")
    os.chmod(pinned, 0o644)
    return server.host("down", port=port, known_hosts=pinned)


async def test_host_down_is_a_transport_error_with_audit(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path, extra_hosts=(_dead_port_host(sshd),), allowed=("lab", "gated", "down"))
    ctx = _ctx(await _job(sessionmaker))
    res = await tools.ssh_read("down", "uptime", ctx=ctx)
    assert res.outcome == SshOutcome.transport_error and res.code == "SSH_TRANSPORT_ERROR" and res.exit_code == 255
    assert "refused" in res.stderr.lower()
    (run,) = await _runs(sessionmaker, ctx.step_id)
    assert run.exit_code == 255 and run.target == "ssh"
    (event,) = await _events(sessionmaker, ctx.step_id)
    assert event.payload["outcome"] == "transport_error" and event.severity == "error"


async def test_unroutable_host_respects_connect_timeout(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    """TEST-NET-1 (RFC 5737) is never routed: ssh gives up after ConnectTimeout (or at once if there is no route)."""
    pinned = tmp_path / "known_hosts_testnet"
    pub = (sshd.root / "host_key.pub").read_text().split()
    pinned.write_text(f"192.0.2.1 {pub[0]} {pub[1]}\n", encoding="utf-8")
    os.chmod(pinned, 0o644)
    blackhole = HostConfig(
        id="blackhole",
        address="192.0.2.1",
        role="webui",
        ssh=SshConfig(user=sshd.user, port=22, key_ref=f"file:{sshd.client_key}", known_hosts=str(pinned)),
    )
    tools = build_tools(sshd, sessionmaker, tmp_path, extra_hosts=(blackhole,), allowed=("lab", "blackhole"))
    tools.runner.connect_timeout = 2
    started = time.monotonic()
    res = await tools.runner.execute("blackhole", "uptime", timeout=30)
    assert res.exit_code == 255 and res.transport_error and not res.timed_out
    assert time.monotonic() - started < 15


async def test_missing_ssh_client_is_reported_and_audited(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path, ssh_binary=str(tmp_path / "no-such-ssh"))
    ctx = _ctx(await _job(sessionmaker))
    res = await tools.ssh_read("lab", "uptime", ctx=ctx)
    assert res.outcome == SshOutcome.transport_error and res.code == "SSH_CLIENT_MISSING"
    (run,) = await _runs(sessionmaker, ctx.step_id)
    assert run.exit_code is None and "not found" in (run.stderr_excerpt or "")  # the started row is completed, not left dangling


async def test_preflight_failures_do_not_consume_the_approval(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    loose_key = tmp_path / "loose_key"
    shutil.copy(sshd.client_key, loose_key)
    os.chmod(loose_key, 0o644)
    loose = sshd.host("loose", key=loose_key)
    no_known = sshd.host("noknown", known_hosts=tmp_path / "absent_known_hosts")
    tools = build_tools(sshd, sessionmaker, tmp_path, extra_hosts=(loose, no_known), allowed=("lab", "loose", "noknown"))
    job_id = await _job(sessionmaker)
    ctx = _ctx(job_id)
    target = tmp_path / "never-created"
    for host, code in (("loose", "SSH_KEY_PERMISSIONS"), ("noknown", "SSH_KNOWN_HOSTS_MISSING")):
        token = approval_token(host, f"touch {target}", ctx.step_id)
        async with sessionmaker() as s, s.begin():  # type: ignore[operator]
            await grant_approval(s, job_id, token, approved_by="op")
        res = await tools.ssh_command(host, f"touch {target}", approval_token=token, ctx=ctx)
        assert res.outcome == SshOutcome.transport_error and res.code == code, host
        async with sessionmaker() as s:  # type: ignore[operator]
            assert await approval_state(s, job_id, token) == "approved", host  # still usable once the config is fixed
    assert not target.exists()
    assert await _runs(sessionmaker, ctx.step_id) == []  # nothing started


async def test_connection_dropped_by_the_host(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    """The remote session process dies mid-command (host reboot, sshd restart): ssh reports 255, output so far is kept."""
    tools = build_tools(sshd, sessionmaker, tmp_path, require_approval=False)
    res = await tools.runner.execute("lab", "echo partial; sleep 0.3; kill -9 $PPID; sleep 5", timeout=20)
    assert res.exit_code == 255 and res.transport_error and res.stdout.startswith("partial")


async def test_audit_failure_after_execution_is_flagged_not_hidden(
    sshd: SshdServer, sessionmaker: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = build_tools(sshd, sessionmaker, tmp_path)
    ctx = _ctx(await _job(sessionmaker))

    async def broken_append_event(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(tools_module, "append_event", broken_append_event)
    res = await tools.ssh_read("lab", "uptime", ctx=ctx)
    # the command ran: the caller gets its real result and the audit gap is flagged ...
    assert res.outcome == SshOutcome.ok and res.data["audit_incomplete"] is True
    # ... and the 'started' command_runs row written before execution remains as evidence
    (run,) = await _runs(sessionmaker, ctx.step_id)
    assert run.exit_code is None and run.classification == "read"


async def test_unknown_step_job_cannot_use_approvals(sshd: SshdServer, sessionmaker: object, tmp_path: Path) -> None:
    """Approvals are per job: without job context a mutation can never run, whatever token is passed."""
    tools = build_tools(sshd, sessionmaker, tmp_path)
    ctx = _ctx(None)
    target = tmp_path / "no-job"
    token = approval_token("lab", f"touch {target}", ctx.step_id)
    res = await tools.ssh_command("lab", f"touch {target}", approval_token=token, ctx=ctx)
    assert res.outcome == SshOutcome.approval_required and res.code == "SSH_APPROVAL_PENDING" and not target.exists()
    res = await tools.ssh_command("lab", f"touch {target}", approval_token="ssh-" + uuid.uuid4().hex, ctx=ctx)
    assert res.code == "SSH_APPROVAL_INVALID"
