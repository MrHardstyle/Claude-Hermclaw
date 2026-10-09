"""Remote execution on the execution worker ``.222`` (DECISIONS D-005).

The orchestrator workspace (``.225``) stays the single source of truth for Git. Before every command the executor
syncs the workspace content (never ``.git``) to the worker by content-hash manifest (only changed files travel,
deletions are mirrored), runs the command in the worker's rootless sandbox and syncs the files the command changed
back. ``.git`` paths are refused in both directions, extraction goes through ``extract_tar_safely`` (no escapes,
no device files), local deletions never follow symlinked parents.
"""

from __future__ import annotations

import asyncio
import os
import stat
import uuid
from pathlib import Path

from hermclaw.contracts.worker import CommandRequest, CommandResult
from hermclaw.core.config import SandboxPolicy
from hermclaw.core.errors import PolicyViolation
from hermclaw.core.interfaces import ExecutionRequest, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.workers.client import ExecutionWorkerClient
from hermclaw.workers.errors import WorkerRemoteError
from worker.execution.workspaces import build_tar, diff_manifest, extract_tar_safely, manifest

log = get_logger(__name__)


def _is_git(path: str) -> bool:
    first = path.replace("\\", "/").split("/", 1)[0]
    return first == ".git"


def _safe_unlink(root: Path, rel: str) -> bool:
    """Delete ``root/rel`` (file or symlink) without following symlinks in any parent component."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or rel.startswith("/") or _is_git(rel):
        raise PolicyViolation(f"refusing to delete unsafe path {rel!r}", code="SYNC_PATH_UNSAFE")
    cur = root
    for part in parts[:-1]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise PolicyViolation(f"refusing to delete below non-directory {cur.relative_to(root)!s}", code="SYNC_PATH_UNSAFE")
    target = cur / parts[-1]
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        return False  # directories are left alone; only files/symlinks are mirrored
    os.unlink(target)
    return True


class RemoteSandboxExecutor:
    """``CommandExecutor`` that runs commands on an execution worker through :class:`ExecutionWorkerClient`."""

    def __init__(self, client: ExecutionWorkerClient, policy: SandboxPolicy, *, upload_timeout_seconds: float = 600.0) -> None:
        self.client = client
        self.policy = policy
        self.upload_timeout_seconds = upload_timeout_seconds
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    @staticmethod
    def remote_id(workspace: WorkspaceHandle) -> str:
        return workspace.id.hex

    async def _remote_manifest(self, rid: str) -> dict[str, str] | None:
        try:
            return dict((await self.client.workspace_manifest(rid)).files)
        except WorkerRemoteError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def sync_up(self, workspace: WorkspaceHandle) -> dict[str, str]:
        """Bring the worker copy to the orchestrator state; returns the local manifest used as baseline."""
        root, rid = Path(workspace.path), self.remote_id(workspace)
        local = await asyncio.to_thread(manifest, root)
        remote = await self._remote_manifest(rid)
        if remote is None:
            tar = await asyncio.to_thread(build_tar, root)
            await self.client.upload_workspace(rid, tar, mode="replace", timeout_seconds=self.upload_timeout_seconds)
            return local
        changed, deleted = diff_manifest(remote, local)
        if changed:
            tar = await asyncio.to_thread(build_tar, root, changed)
            await self.client.upload_workspace(rid, tar, mode="merge", timeout_seconds=self.upload_timeout_seconds)
        if deleted:
            await self.client.delete_paths(rid, deleted)
        return local

    async def sync_down(self, workspace: WorkspaceHandle, baseline: dict[str, str]) -> tuple[list[str], list[str]]:
        """Copy back what the command changed on the worker (relative to ``baseline``)."""
        root, rid = Path(workspace.path), self.remote_id(workspace)
        after = await self._remote_manifest(rid) or {}
        changed, deleted = diff_manifest(baseline, after)
        bad = [p for p in changed + deleted if _is_git(p)]
        if bad:
            log.warning("worker reported .git paths; ignored", extra={"paths": bad[:10]})
        changed = [p for p in changed if not _is_git(p)]
        deleted = [p for p in deleted if not _is_git(p)]
        if changed:
            data = await self.client.download_workspace(rid, paths=changed, timeout_seconds=self.upload_timeout_seconds)
            await asyncio.to_thread(extract_tar_safely, data, root)
        removed = [p for p in deleted if await asyncio.to_thread(_safe_unlink, root, p)]
        return changed, removed

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        lock = self._locks.setdefault(workspace.id, asyncio.Lock())
        async with lock:  # one sync+run+sync cycle per workspace at a time
            baseline = await self.sync_up(workspace)
            request = CommandRequest(
                request_id=f"r-{uuid.uuid4().hex}",
                job_id=str(req.job_id or workspace.job_id),
                step_id=str(req.step_id or ""),
                workspace=self.remote_id(workspace),
                command=req.command,
                image=req.image,
                timeout_seconds=min(max(req.timeout_seconds, 1), 7200),
                network=req.network,
                env=dict(req.env),
                cpus=self.policy.cpus,
                memory=self.policy.memory,
            )
            result = await self.client.run_command(request)
            await self.sync_down(workspace, baseline)
            return result

    async def drop(self, workspace: WorkspaceHandle) -> None:
        """Remove the worker copy (job finished / workspace archived)."""
        try:
            await self.client.delete_workspace(self.remote_id(workspace))
        except WorkerRemoteError as exc:
            if exc.status_code != 404:
                raise
        self._locks.pop(workspace.id, None)
