"""RemoteSandboxExecutor against the real execution worker daemon app (signed requests over ASGI transport,
real tar sync). The command runner inside the daemon is the local test runner (no podman needed)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from hermclaw.core.config import SandboxPolicy
from hermclaw.core.errors import PolicyViolation
from hermclaw.core.interfaces import ExecutionRequest, WorkspaceHandle
from hermclaw.tools.remote import RemoteSandboxExecutor, _safe_unlink
from tests.integration.test_workers_execution_daemon import _app, _client
from tests.integration.test_workers_support import lifespan

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
async def remote(tmp_path: Path) -> AsyncIterator[tuple[RemoteSandboxExecutor, WorkspaceHandle, Any]]:
    (tmp_path / "worker").mkdir()
    app = _app(tmp_path / "worker")
    ws_dir = tmp_path / "orchestrator" / "ws"
    (ws_dir / "src").mkdir(parents=True)
    (ws_dir / ".git").mkdir()
    (ws_dir / ".git" / "config").write_text("[core]\n")
    (ws_dir / "src" / "a.py").write_text("A = 1\n")
    (ws_dir / "README.md").write_text("# demo\n")
    handle = WorkspaceHandle(
        id=uuid.uuid4(), job_id=uuid.uuid4(), path=ws_dir, branch="hermclaw/x", base_branch="main", base_sha="0" * 40, repository_key="demo"
    )
    async with lifespan(app), _client(app) as c:
        yield RemoteSandboxExecutor(c, SandboxPolicy(engine="local")), handle, app


def _worker_dir(app: Any, handle: WorkspaceHandle) -> Path:
    return Path(app.state.execution.root) / handle.id.hex


async def test_first_run_uploads_without_git_and_syncs_changes_back(remote: Any) -> None:
    ex, ws, app = remote
    res = await ex.run(
        ws, ExecutionRequest(command="cat src/a.py && echo 'B = 2' > src/b.py && echo changed >> README.md", timeout_seconds=30)
    )
    assert res.exit_code == 0 and "A = 1" in res.stdout
    wdir = _worker_dir(app, ws)
    assert (wdir / "src" / "a.py").read_text() == "A = 1\n" and not (wdir / ".git").exists(), ".git must never reach the worker"
    assert (ws.path / "src" / "b.py").read_text() == "B = 2\n", "created file synced back"
    assert (ws.path / "README.md").read_text().endswith("changed\n"), "modified file synced back"
    assert (ws.path / ".git" / "config").read_text() == "[core]\n"


async def test_incremental_sync_mirrors_local_edits_and_deletions(remote: Any) -> None:
    ex, ws, app = remote
    await ex.run(ws, ExecutionRequest(command="true"))
    wdir = _worker_dir(app, ws)
    (ws.path / "src" / "a.py").write_text("A = 42\n")
    (ws.path / "README.md").unlink()
    res = await ex.run(ws, ExecutionRequest(command="cat src/a.py; test ! -e README.md && echo gone"))
    assert "A = 42" in res.stdout and "gone" in res.stdout
    assert not (wdir / "README.md").exists()


async def test_remote_deletion_is_mirrored_locally_and_git_writes_are_ignored(remote: Any) -> None:
    ex, ws, app = remote
    res = await ex.run(ws, ExecutionRequest(command="rm src/a.py && mkdir -p .git && echo evil > .git/hooks_pre && echo ok"))
    assert res.exit_code == 0
    assert not (ws.path / "src" / "a.py").exists(), "deletion by the command mirrored to the orchestrator"
    assert not (ws.path / ".git" / "hooks_pre").exists(), "worker-side .git writes never come back"


async def test_failed_command_result_is_returned_and_drop_removes_remote_copy(remote: Any) -> None:
    ex, ws, app = remote
    res = await ex.run(ws, ExecutionRequest(command="echo boom >&2; exit 3"))
    assert res.exit_code == 3 and "boom" in res.stderr
    assert _worker_dir(app, ws).exists()
    await ex.drop(ws)
    assert not _worker_dir(app, ws).exists()
    await ex.drop(ws)  # idempotent


async def test_safe_unlink_never_follows_symlinked_parents(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("keep")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside, root / "link")
    with pytest.raises(PolicyViolation):
        _safe_unlink(root, "link/victim.txt")
    assert (outside / "victim.txt").exists()
    for bad in ("../x", "/etc/passwd", ".git/config", ""):
        with pytest.raises(PolicyViolation):
            _safe_unlink(root, bad)
    (root / "f.txt").write_text("x")
    assert _safe_unlink(root, "f.txt") is True and not (root / "f.txt").exists()
    assert _safe_unlink(root, "missing.txt") is False
