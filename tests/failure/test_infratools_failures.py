"""P28 failure paths: timeouts, transport errors, injection attempts, audit robustness, missing engines."""

from __future__ import annotations

import asyncio
import os
import stat
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import HostConfig, HostsConfig
from hermclaw.core.errors import HermclawError
from hermclaw.infratools import InfraTools
from hermclaw.infratools.compose import ComposeEngines
from hermclaw.infratools.compose_sandbox import ComposeSandbox
from hermclaw.infratools.errors import (
    INFRA_COMMAND_FAILED,
    INFRA_ENGINE_UNAVAILABLE,
    INFRA_INVALID_ARGUMENT,
    INFRA_REMOTE_UNAVAILABLE,
    INFRA_TIMEOUT,
    InfraToolError,
)
from hermclaw.infratools.nginx import NginxTester, ProxyConfigValidator
from hermclaw.infratools.ports import PortChecker
from hermclaw.infratools.process import CommandAudit, HostExecutor, InfraContext, LocalRunner, run_local
from hermclaw.persistence.models import CommandRun

HOSTS = HostsConfig(hosts=[HostConfig(id="exec-222", address="127.0.0.1", role="execution_worker")])


class RaisingRunner:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def run(self, host_id: str, command: str, timeout: float) -> tuple[int, str, str]:
        raise self.exc


class SshTimeout(HermclawError):
    code = "SSH_TIMEOUT"


async def test_local_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    res = await run_local(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], timeout=1)
    assert res.timed_out and res.exit_code is None and res.error_code == INFRA_TIMEOUT and not res.ok
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("background child survived the timeout")
    assert "timed out" in res.describe()


async def test_output_cap_missing_binary_and_bad_timeout() -> None:
    res = await run_local(["sh", "-c", "head -c 300000 /dev/zero"], timeout=10, max_output_bytes=1000)
    assert res.ok and res.truncated and len(res.stdout) == 1000
    missing = await run_local(["definitely-not-installed-xyz"], timeout=5)
    assert missing.missing_binary and missing.error_code == INFRA_ENGINE_UNAVAILABLE and "not installed" in missing.describe()
    with pytest.raises(InfraToolError):
        await run_local(["true"], timeout=0)
    with pytest.raises(InfraToolError):
        await run_local([], timeout=1)


@pytest.mark.parametrize(
    ("exc", "timed_out"),
    [(TimeoutError(), True), (SshTimeout("ssh timed out"), True), (OSError("connection reset"), False), (HermclawError("refused"), False)],
)
async def test_remote_transport_errors_become_results(exc: BaseException, timed_out: bool) -> None:
    executor = HostExecutor(remote=RaisingRunner(exc), hosts=HOSTS)
    res = await executor.run(["podman", "ps"], host="exec-222", timeout=5)
    assert not res.ok and res.timed_out is timed_out and res.error
    assert res.error_code == (INFRA_TIMEOUT if timed_out else INFRA_COMMAND_FAILED)


async def test_remote_unavailable_is_raised() -> None:
    executor = HostExecutor(remote=RaisingRunner(InfraToolError("down", code=INFRA_REMOTE_UNAVAILABLE)), hosts=HOSTS)
    with pytest.raises(InfraToolError) as err:
        await executor.run(["true"], host="exec-222")
    assert err.value.code == INFRA_REMOTE_UNAVAILABLE


async def test_remote_command_quoting_prevents_injection(tmp_path: Path) -> None:
    runner = LocalRunner()
    executor = HostExecutor(remote=runner, hosts=HOSTS)
    marker = tmp_path / "pwned"
    res = await executor.run(["printf", "%s|", "a b", "c'd", f"$(touch {marker})", "; touch x", "`id`"], host="exec-222")
    assert res.ok and res.stdout == f"a b|c'd|$(touch {marker})|; touch x|`id`|"
    assert not marker.exists()
    with pytest.raises(InfraToolError):
        await executor.run(["echo", "two\nlines"], host="exec-222")
    with pytest.raises(InfraToolError):
        await executor.run(["true"], host="exec-222", cwd=tmp_path)
    with pytest.raises(InfraToolError):
        await executor.run(["true"], host="exec-222", env={"A": "1"})
    no_stdin = HostExecutor(remote=RaisingRunner(OSError()), hosts=HOSTS)
    with pytest.raises(InfraToolError) as err:
        await no_stdin.run(["cat"], host="exec-222", stdin=b"x")
    assert err.value.code == INFRA_REMOTE_UNAVAILABLE


async def test_local_runner_timeout_raises() -> None:
    with pytest.raises(InfraToolError) as err:
        await LocalRunner().run("exec-222", "sleep 5", 0.5)
    assert err.value.code == INFRA_TIMEOUT


@pytest.mark.integration
async def test_command_audit_is_redacted_and_never_stores_raw_output(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    executor = HostExecutor(audit=CommandAudit(sessionmaker))
    job_marker = uuid.uuid4()
    ctx = InfraContext(job_id=None, step_id=job_marker)
    await executor.run(["echo", "password=supersecretvalue1"], ctx=ctx, classification="read")
    await executor.run(["echo", "raw-file-content-xyz"], ctx=ctx, raw_stdout=True)
    async with sessionmaker() as s:
        rows = list((await s.execute(select(CommandRun).where(CommandRun.step_id == job_marker))).scalars())
    assert len(rows) == 2
    secret_row = next(r for r in rows if "password" in r.command)
    raw_row = next(r for r in rows if "raw-file-content" in r.command)
    assert "supersecretvalue1" not in secret_row.command and "supersecretvalue1" not in (secret_row.stdout_excerpt or "")
    assert secret_row.stdout_excerpt and "REDACTED" in secret_row.stdout_excerpt
    assert raw_row.stdout_excerpt is None  # unredacted file/JSON content is never stored
    assert all(r.target == "local" and r.exit_code == 0 for r in rows)


async def test_broken_audit_does_not_break_the_tool() -> None:
    def broken() -> object:
        raise RuntimeError("db down")

    executor = HostExecutor(audit=CommandAudit(broken))  # type: ignore[arg-type]
    assert (await executor.run(["true"])).ok


async def test_sandbox_argument_and_engine_failures(tmp_path: Path) -> None:
    executor = HostExecutor(env={"PATH": "/nonexistent"})
    sandbox = ComposeSandbox(executor, ComposeEngines(executor))
    with pytest.raises(InfraToolError) as err:
        await sandbox.run(tmp_path / "compose.yaml", timeout=0)
    assert err.value.code == INFRA_INVALID_ARGUMENT
    (tmp_path / "compose.yaml").write_text("services:\n  a: {image: docker.io/library/alpine:3.20}\n")
    report = await sandbox.run(tmp_path / "compose.yaml", timeout=30)
    assert report.status == "skipped" and "no compose implementation installed" in (report.reason or "")
    missing = await sandbox.run(tmp_path / "missing.yaml", timeout=30)
    assert missing.status == "refused"
    (tmp_path / "bad.yaml").write_text("services: [")
    assert (await sandbox.run(tmp_path / "bad.yaml", timeout=30)).status == "refused"


async def test_nginx_test_timeout_with_hanging_binary(tmp_path: Path) -> None:
    fake = tmp_path / "nginx"
    fake.write_text('#!/bin/sh\nif [ "$1" = "-v" ]; then echo "nginx version: nginx/9.9.9" >&2; exit 0; fi\nsleep 30\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "t").mkdir()
    report = await NginxTester(HostExecutor(), binary=str(fake), timeout=1, tmp_root=tmp_path / "t").test("server { listen 1; }")
    assert report.status == "failed" and "timed out" in (report.reason or "")
    assert report.nginx == "nginx 9.9.9" and list((tmp_path / "t").iterdir()) == []


async def test_port_check_without_ss_reports_live_error() -> None:
    checker = PortChecker(HostExecutor(env={"PATH": "/nonexistent"}))
    report = await checker.check("server { listen 18080; }")
    assert not report.live_checked and report.live_error and not report.ok


async def test_default_resolver_and_unparsable_predeploy() -> None:
    report = await ProxyConfigValidator().validate("server { listen 80; location / { proxy_pass http://localhost:8000; } }")
    assert report.ok, report.render()  # resolves to loopback
    tools = InfraTools(HostExecutor())
    gate = await tools.predeploy_check("compose", "services: [", live_ports=False, inventory=False)
    assert not gate.ok and any(f.code == "PORT_CHECK_FAILED" for f in gate.findings)
    with pytest.raises(InfraToolError):
        await tools.predeploy_check("kubernetes", "x")  # type: ignore[arg-type]
