"""P17 failure behaviour: sandbox/executor errors, broken services, cancellation, non-git workspaces, huge outputs."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeExpansionRequest
from hermclaw.contracts.tools import CoderAction, ToolName
from hermclaw.contracts.worker import CommandResult
from hermclaw.core.interfaces import ExecutionRequest, WorkspaceHandle
from hermclaw.scope.guard import ScopeGuard
from hermclaw.tools import errors as E
from hermclaw.tools.context import NullCallbacks
from hermclaw.tools.engine import ToolEngine
from hermclaw.tools.errors import ToolError
from hermclaw.tools.executors import SubprocessExecutor
from tests.integration.test_tools_support import (
    SM,
    BrokenGitReader,
    GitCliReader,
    RecordingCallbacks,
    ScriptedRepo,
    command_runs,
    dev_settings,
    events_for,
    git,
    make_harness,
    make_step,
    policies,
    scope,
    tool_calls,
    workspace_for,
)

SCOPE = {"target_paths": ["app.py"], "allowed_new_paths": ["gen/**"]}


class ExplodingExecutor:
    """Writes files (in and out of scope) and then fails like a crashed sandbox."""

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        (workspace.path / "gen").mkdir(exist_ok=True)
        (workspace.path / "gen" / "ok.txt").write_text("ok\n")
        (workspace.path / "stray.txt").write_text("stray\n")
        raise ConnectionError("execution worker lost")


class SlowExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        self.started.set()
        await asyncio.sleep(60)
        raise AssertionError("not reached")


async def test_executor_failure_still_audits_side_effects(sessionmaker: SM, tmp_repo: Path) -> None:
    h = await make_harness(sessionmaker, tmp_repo, contract=scope(**SCOPE), executor=ExplodingExecutor())
    res = await h.call("run_command", command="make all")
    assert not res.ok and res.error_code == E.COMMAND_SCOPE_VIOLATION  # violation wins over the sandbox error
    assert "sandbox error: ConnectionError" in res.output
    assert not (tmp_repo / "stray.txt").exists() and (tmp_repo / "gen" / "ok.txt").exists()
    runs = await command_runs(sessionmaker, h.step.id)
    assert runs[0].exit_code is None and "execution worker lost" in (runs[0].stderr_excerpt or "")
    h2 = await make_harness(sessionmaker, tmp_repo, contract=None, executor=ExplodingExecutor())
    res = await h2.call("run_test", command="pytest -q")
    assert res.error_code == E.COMMAND_SCOPE_VIOLATION and res.data["status"] == "error"


async def test_executor_failure_without_side_effects_is_sandbox_error(sessionmaker: SM, tmp_repo: Path) -> None:
    class Down:
        async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
            raise ConnectionError("no route to host")

    h = await make_harness(sessionmaker, tmp_repo, executor=Down())
    res = await h.call("run_command", command="ls")
    assert res.error_code == E.SANDBOX_ERROR and (await tool_calls(sessionmaker, h.step.id))[0].status == "failed"
    res = await h.call("run_test", command="pytest")
    assert res.error_code == E.TEST_ERROR and res.data["status"] == "error"


async def test_cancellation_is_recorded(sessionmaker: SM, tmp_repo: Path) -> None:
    slow = SlowExecutor()
    h = await make_harness(sessionmaker, tmp_repo, executor=slow)
    task = asyncio.create_task(h.call("run_command", command="sleep 60"))
    await asyncio.wait_for(slow.started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await tool_calls(sessionmaker, h.step.id)
    assert rows[0].status == "cancelled" and rows[0].error_code == "CANCELLED"
    # the engine stays usable after a cancelled call
    assert (await h.call("read_file", path="app.py")).ok


async def test_callback_failures_are_contained(sessionmaker: SM, tmp_repo: Path) -> None:
    h = await make_harness(sessionmaker, tmp_repo, contract=scope(**SCOPE), callbacks=RecordingCallbacks(fail=True))
    assert (await h.call("request_research", question="what is the API?")).error_code == E.RESEARCH_FAILED
    res = await h.call("request_scope_expansion", paths=["README.md"], justification="needs the readme updated")
    assert res.error_code == E.SCOPE_EXPANSION_FAILED
    res = await h.call("request_replan", reason="cannot continue with this plan")
    assert res.error_code == E.REPLAN_FAILED and not res.terminal and not h.engine.finished


async def test_null_callbacks_refuse(sessionmaker: SM, tmp_repo: Path) -> None:
    cb = NullCallbacks()
    with pytest.raises(ToolError):
        await cb.on_research("q")
    assert not (await cb.on_scope_expansion(ScopeExpansionRequest(paths=["x"], justification="because reasons"))).granted
    step = await make_step(sessionmaker)
    engine = ToolEngine(
        sessionmaker,
        policies(),
        workspace_for(step.job_id, tmp_repo),
        None,
        SubprocessExecutor(settings=dev_settings()),
        GitCliReader(),
        ScriptedRepo(),
    )
    res = await engine.execute(CoderAction(tool=ToolName.request_replan, args={"reason": "plan is wrong here"}), step_id=step.id, turn=1)
    assert res.error_code == E.REPLAN_FAILED and not res.terminal


async def test_broken_git_reader(sessionmaker: SM, tmp_repo: Path) -> None:
    h = await make_harness(sessionmaker, tmp_repo, git_reader=BrokenGitReader())
    assert (await h.call("git_status")).error_code == E.GIT_UNAVAILABLE
    res = await h.call("complete_step", summary="done with the change")
    assert res.ok and res.terminal and res.data["actual_changed_files"] is None


async def test_internal_handler_error_is_contained(sessionmaker: SM, tmp_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = await make_harness(sessionmaker, tmp_repo)

    async def boom(*_args: Any) -> Any:
        raise KeyError("secret-internal-detail")

    monkeypatch.setitem(h.engine._handlers, ToolName.list_files, boom)
    res = await h.call("list_files")
    assert res.error_code == E.TOOL_INTERNAL_ERROR and "secret-internal-detail" not in res.output and "KeyError" in res.output
    rows = await tool_calls(sessionmaker, h.step.id)
    assert rows[0].status == "failed"
    finished = await events_for(sessionmaker, h.step.id, EventType.TOOL_CALL_FINISHED)
    assert finished[0].payload["ok"] is False and finished[0].severity == "warning"


async def test_checkpoint_for_unknown_step(sessionmaker: SM, tmp_repo: Path) -> None:
    h = await make_harness(sessionmaker, tmp_repo)
    res = await h.engine.execute(CoderAction(tool=ToolName.checkpoint, args={"notes": "n"}), step_id=uuid.uuid4(), turn=1)
    assert res.error_code == E.STEP_NOT_FOUND


async def test_non_git_workspace(sessionmaker: SM, tmp_path: Path) -> None:
    ws = tmp_path / "plain"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("A = 1\n")
    (ws / "keep.txt").write_text("keep\n")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "x.js").write_text("x")
    step = await make_step(sessionmaker)
    pol = policies()
    handle = WorkspaceHandle(uuid.uuid4(), step.job_id, ws, "b", "main", "", "plain")
    engine = ToolEngine(
        sessionmaker,
        pol,
        handle,
        ScopeGuard(scope(target_paths=["src/a.py"], allowed_new_paths=["src/new_*.py"]), pol.scope),
        SubprocessExecutor(settings=dev_settings()),
        GitCliReader(),
        ScriptedRepo(),
    )
    act = CoderAction(tool=ToolName.list_files)
    res = await engine.execute(act, step_id=step.id, turn=1)
    assert res.ok and set(res.output.splitlines()) == {"keep.txt", "src/a.py"}
    assert (
        await engine.execute(
            CoderAction(tool=ToolName.write_file, args={"path": "src/new_b.py", "content": "B\n"}), step_id=step.id, turn=2
        )
    ).ok
    edit = CoderAction(tool=ToolName.replace_text, args={"path": "src/new_b.py", "old": "B", "new": "C"})
    assert (await engine.execute(edit, step_id=step.id, turn=3)).ok  # created by this attempt -> still a create
    cmd = CoderAction(tool=ToolName.run_command, args={"command": "echo x >> keep.txt; echo y > stray.txt; echo 'A = 2' > src/a.py"})
    res = await engine.execute(cmd, step_id=step.id, turn=4)
    assert res.error_code == E.COMMAND_SCOPE_VIOLATION and res.mutated_paths == ["src/a.py"]
    assert (ws / "keep.txt").read_text() == "keep\n" and not (ws / "stray.txt").exists() and (ws / "src" / "a.py").read_text() == "A = 2\n"


async def test_huge_command_output_is_budgeted(sessionmaker: SM, tmp_repo: Path) -> None:
    h = await make_harness(sessionmaker, tmp_repo)
    res = await h.call("run_command", command="seq 1 300000; echo END >&2")
    assert res.ok and res.truncated and len(res.output) <= h.engine.output_limit + 400
    assert "END" in res.output and "omitted" in res.output
    run = (await command_runs(sessionmaker, h.step.id))[0]
    assert len(run.stdout_excerpt or "") <= 8200
    assert git(tmp_repo, "status", "--porcelain") == ""


async def test_sandbox_reported_error_without_exit_code(sessionmaker: SM, tmp_repo: Path) -> None:
    class ImagePullFails:
        async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
            return CommandResult(request_id="r", exit_code=None, error="image pull failed: python:3.12", sandbox="podman")

    h = await make_harness(sessionmaker, tmp_repo, executor=ImagePullFails())
    res = await h.call("run_command", command="ls")
    assert res.error_code == E.SANDBOX_ERROR and "image pull failed" in res.output
    res = await h.call("run_test", command="pytest")
    assert res.error_code == E.TEST_ERROR and "image pull failed" in res.output
    runs = await command_runs(sessionmaker, h.step.id)
    assert all(r.exit_code is None and "[sandbox] image pull failed" in (r.stderr_excerpt or "") for r in runs)
