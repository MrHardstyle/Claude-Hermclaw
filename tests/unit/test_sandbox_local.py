"""P18 LocalSandbox (no isolation, development/test only): real subprocesses, timeouts, process-group cleanup."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.worker import CommandRequest
from hermclaw.core.config import SandboxPolicy
from hermclaw.core.errors import PolicyViolation
from hermclaw.core.redaction import REDACTED
from worker.execution.sandbox import LocalSandbox, make_sandbox

POLICY = SandboxPolicy(engine="local", default_timeout_seconds=30, env_allowlist=["CI", "APP_*", "MY_API_TOKEN"])


def req(command: str, **kw: Any) -> CommandRequest:
    return CommandRequest(
        request_id=kw.pop("request_id", f"local-{uuid.uuid4().hex[:10]}"),
        job_id="job-l",
        step_id="step-l",
        workspace="ws",
        command=command,
        **kw,
    )


def local(**kw: Any) -> LocalSandbox:
    return LocalSandbox(POLICY, environment="test", max_output_bytes=kw.pop("max_output_bytes", 50_000), **kw)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    (d / "input.txt").write_text("7\n")
    return d


def _alive(pid: int) -> bool:
    """True while ``pid`` exists and is not a zombie (zombies are dead, just not reaped yet)."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    state = data.rsplit(")", 1)[1].split()[0]
    return state not in ("Z", "X")


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


async def test_runs_in_workspace_with_exit_code(ws: Path) -> None:
    r = await local().run(req("cat input.txt; pwd; echo err >&2; echo done > out.txt; exit 3"), ws)
    assert r.exit_code == 3 and not r.timed_out and r.error is None
    lines = r.stdout.split()
    assert lines[0] == "7" and Path(lines[1]).resolve() == ws.resolve()
    assert r.stderr.strip() == "err" and (ws / "out.txt").read_text() == "done\n"
    assert r.sandbox == "local" and r.container_name is None and r.duration_ms >= 0


async def test_only_allowlisted_env_and_no_host_secrets(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMCLAW_P18_HOST_SECRET", "host-only-value")
    r = await local().run(req("env", env={"APP_MODE": "x", "CI": "1"}), ws)
    env = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    assert env["APP_MODE"] == "x" and env["CI"] == "1"
    assert "HERMCLAW_P18_HOST_SECRET" not in env and "host-only-value" not in r.stdout
    assert set(env) <= {"APP_MODE", "CI", "PATH", "LANG", "HOME", "TMPDIR", "PWD", "SHLVL", "_", "OLDPWD"}
    denied = await local().run(req("env", env={"LD_PRELOAD": "/x.so"}), ws)
    assert denied.exit_code is None and denied.error and denied.error.startswith("SANDBOX_ENV_NOT_ALLOWED")


async def test_timeout_kills_whole_process_group(ws: Path) -> None:
    r = await local().run(req("sleep 30 & echo $! > child.pid; echo started; sleep 30", timeout_seconds=1), ws)
    assert r.timed_out and r.exit_code is None and r.error and "timeout" in r.error
    assert "started" in r.stdout and r.duration_ms < 10_000
    assert await _wait_dead(int((ws / "child.pid").read_text()))


async def test_background_children_killed_after_command_exits(ws: Path) -> None:
    r = await local().run(req("sleep 30 >/dev/null 2>&1 & echo $! > bg.pid; echo ok"), ws)
    assert r.exit_code == 0 and r.stdout.strip() == "ok"
    assert await _wait_dead(int((ws / "bg.pid").read_text()))


async def test_child_holding_pipe_does_not_hang(ws: Path) -> None:
    # the background child inherits stdout; without process-group cleanup run() would wait 30 s for EOF
    started = time.monotonic()
    r = await local(kill_grace_seconds=1.0).run(req("sleep 30 & echo $! > bg.pid; echo quick"), ws)
    assert r.exit_code == 0 and "quick" in r.stdout
    assert time.monotonic() - started < 10
    assert await _wait_dead(int((ws / "bg.pid").read_text()))


async def test_cancellation_kills_process_group(ws: Path) -> None:
    task = asyncio.create_task(local().run(req("echo $$ > sh.pid; sleep 30 & echo $! > child.pid; wait"), ws))
    for _ in range(100):
        if (ws / "child.pid").exists() and (ws / "child.pid").read_text().strip():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_dead(int((ws / "child.pid").read_text()))
    assert await _wait_dead(int((ws / "sh.pid").read_text()))


async def test_output_truncated_and_secrets_redacted(ws: Path) -> None:
    r = await local(max_output_bytes=2048).run(
        req("yes line | head -n 5000; echo password=hunter2-hunter2 >&2; echo tok=$MY_API_TOKEN", env={"MY_API_TOKEN": "zz-top-secret-1"}),
        ws,
    )
    assert r.exit_code == 0 and r.stdout_truncated and not r.stderr_truncated
    assert "bytes omitted" in r.stdout and len(r.stdout.encode()) < 2048 + 100
    assert "zz-top-secret-1" not in r.stdout and REDACTED in r.stdout
    assert "hunter2-hunter2" not in r.stderr


async def test_policy_default_timeout_applies(ws: Path) -> None:
    s = LocalSandbox(SandboxPolicy(engine="local", default_timeout_seconds=1), environment="test", max_output_bytes=10_000)
    r = await s.run(CommandRequest(request_id="t", job_id="j", step_id="s", workspace="w", command="sleep 20"), ws)
    assert r.timed_out


async def test_missing_workspace_and_nul_command(tmp_path: Path, ws: Path) -> None:
    missing = await local().run(req("true"), tmp_path / "nope")
    assert missing.exit_code is None and missing.error and missing.error.startswith("SANDBOX_WORKSPACE_MISSING")
    nul = await local().run(req("echo a\x00b"), ws)
    assert nul.error and nul.error.startswith("SANDBOX_COMMAND_INVALID")


async def test_missing_shell_reported(ws: Path) -> None:
    r = await local(shell=("/nonexistent/sh", "-c")).run(req("true"), ws)
    assert r.exit_code is None and r.error and "cannot execute" in r.error


def test_local_forbidden_outside_development_and_test() -> None:
    for env in ("production", "staging"):
        with pytest.raises(PolicyViolation) as exc:
            LocalSandbox(POLICY, environment=env)
        assert exc.value.code == "SANDBOX_LOCAL_FORBIDDEN"
    assert isinstance(make_sandbox(POLICY, environment="development", max_output_bytes=1000), LocalSandbox)


async def test_concurrent_local_runs(ws: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    s = local()
    r1, r2 = await asyncio.gather(s.run(req("sleep 0.3; echo a > f; ls"), ws), s.run(req("sleep 0.3; echo b > f; ls"), other))
    assert (ws / "f").read_text() == "a\n" and (other / "f").read_text() == "b\n"
    assert "input.txt" in r1.stdout and "input.txt" not in r2.stdout
    assert os.getpgrp() != 0  # the test process itself survived the process-group kills
