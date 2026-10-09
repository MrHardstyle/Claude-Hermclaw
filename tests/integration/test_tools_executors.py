"""P17 17.5: command executors – subprocess fallback (dev/test only) and sandbox delegation."""

from __future__ import annotations

import asyncio
import importlib
import importlib.machinery
import os
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

from hermclaw.contracts.worker import CommandRequest, CommandResult
from hermclaw.core.config import SandboxPolicy
from hermclaw.core.interfaces import CommandExecutor, ExecutionRequest, WorkspaceHandle
from hermclaw.core.settings import Settings
from hermclaw.tools import executors as X
from hermclaw.tools.executors import LocalSandboxExecutor, SandboxUnavailable, SubprocessExecutor


def _ws(path: Path) -> WorkspaceHandle:
    return WorkspaceHandle(uuid.uuid4(), uuid.uuid4(), path, "b", "main", "0" * 40, "demo")


DEV = Settings(env="development")
PROD = Settings(env="production")


async def test_subprocess_executor_runs_in_workspace_with_env_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMCLAW_TEST_SECRET", "should-not-leak")
    ex = SubprocessExecutor(SandboxPolicy(), settings=DEV)
    assert isinstance(ex, CommandExecutor)
    res = await ex.run(_ws(tmp_path), ExecutionRequest(command='pwd; echo "s=${HERMCLAW_TEST_SECRET:-unset} e=$EXTRA"', env={"EXTRA": "1"}))
    assert res.exit_code == 0 and res.sandbox == "local" and not res.timed_out
    lines = res.stdout.splitlines()
    assert lines[0] == str(tmp_path) and lines[1] == "s=unset e=1"
    res = await ex.run(_ws(tmp_path), ExecutionRequest(command="echo err >&2; exit 4"))
    assert res.exit_code == 4 and res.stderr.strip() == "err"


async def test_subprocess_executor_timeout_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "survivor"
    ex = SubprocessExecutor(SandboxPolicy(), settings=DEV)
    started = time.monotonic()
    res = await ex.run(_ws(tmp_path), ExecutionRequest(command=f"(sleep 3; touch {marker}) & sleep 30", timeout_seconds=1))
    assert res.timed_out and res.exit_code is None and time.monotonic() - started < 10
    await asyncio.sleep(3.5)
    assert not marker.exists()  # the background child was killed with the group


async def test_subprocess_executor_background_children_do_not_block(tmp_path: Path) -> None:
    ex = SubprocessExecutor(SandboxPolicy(), settings=DEV)
    started = time.monotonic()
    res = await ex.run(_ws(tmp_path), ExecutionRequest(command="sleep 30 & echo done", timeout_seconds=20))
    assert res.exit_code == 0 and "done" in res.stdout and time.monotonic() - started < 10


async def test_subprocess_executor_caps_output_keeping_head_and_tail(tmp_path: Path) -> None:
    ex = SubprocessExecutor(SandboxPolicy(), settings=DEV, max_output_bytes=4000)
    res = await ex.run(_ws(tmp_path), ExecutionRequest(command="echo FIRST; seq 1 100000; echo LAST"))
    assert res.stdout_truncated and res.stdout.startswith("FIRST") and res.stdout.rstrip().endswith("LAST")
    assert len(res.stdout) < 4200 and "bytes omitted" in res.stdout


async def test_subprocess_executor_refuses_production(tmp_path: Path) -> None:
    with pytest.raises(SandboxUnavailable):
        await SubprocessExecutor(SandboxPolicy(), settings=PROD).run(_ws(tmp_path), ExecutionRequest(command="true"))
    with pytest.raises(SandboxUnavailable):
        await SubprocessExecutor(SandboxPolicy(), settings=DEV).run(_ws(tmp_path / "missing"), ExecutionRequest(command="true"))


async def test_local_sandbox_executor_fallback_only_outside_production(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(X, "sandbox_module_available", lambda: False)
    dev = LocalSandboxExecutor(SandboxPolicy(), settings=DEV)
    assert dev.mode == "subprocess"
    assert (await dev.run(_ws(tmp_path), ExecutionRequest(command="echo hi"))).stdout.strip() == "hi"
    prod = LocalSandboxExecutor(SandboxPolicy(), settings=PROD)
    with pytest.raises(SandboxUnavailable):
        await prod.run(_ws(tmp_path), ExecutionRequest(command="echo hi"))


async def test_local_sandbox_executor_delegates_to_sandbox_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SandboxPolicy, CommandRequest, Path]] = []

    class FakeRunner:
        def __init__(self, policy: SandboxPolicy) -> None:
            self.policy = policy

        async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult:
            calls.append((self.policy, req, workspace_dir))
            return CommandResult(request_id=req.request_id, exit_code=0, stdout="from sandbox", sandbox="podman")

    module = types.ModuleType(X.SANDBOX_MODULE)
    module.__spec__ = importlib.machinery.ModuleSpec(X.SANDBOX_MODULE, None)
    module.make_sandbox = FakeRunner  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, X.SANDBOX_MODULE, module)
    policy = SandboxPolicy(engine="podman", image="img:1")
    ex = LocalSandboxExecutor(policy, settings=PROD)
    ws = _ws(tmp_path)
    step_id = uuid.uuid4()
    res = await ex.run(
        ws, ExecutionRequest(command="pytest -q", timeout_seconds=99, network=True, image="python", step_id=step_id, env={"A": "1"})
    )
    assert res.stdout == "from sandbox" and ex.mode == "sandbox"
    pol, req, path = calls[0]
    assert pol is policy and path == tmp_path
    assert (req.command, req.timeout_seconds, req.network, req.image, req.env) == ("pytest -q", 99, True, "python", {"A": "1"})
    assert req.workspace == str(ws.id) and req.job_id == str(ws.job_id) and req.step_id == str(step_id)


async def test_sandbox_module_import_errors_surface(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(X, "sandbox_module_available", lambda: True)

    def broken(name: str) -> object:
        raise ImportError("broken sandbox build")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(ImportError):
        await LocalSandboxExecutor(SandboxPolicy(), settings=DEV).run(_ws(tmp_path), ExecutionRequest(command="true"))
    assert os.path.isdir(tmp_path)
