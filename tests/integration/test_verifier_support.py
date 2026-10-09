"""Test helpers for the verifier tests (no tests in here).

- ``GitCliReader``: read-only GitReader over the git CLI (test implementation of the protocol);
- ``WorkspaceShellExecutor``: a LocalSandbox-like CommandExecutor that really runs commands with ``bash -c`` inside
  the workspace (subprocess, own process group, timeout) – used to exercise command/test evidence for real;
- DB helpers that create the job/step rows a verification run references.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.worker import CommandResult
from hermclaw.core.config import PoliciesConfig
from hermclaw.core.interfaces import ExecutionRequest, GitStatusEntry, WorkspaceHandle
from hermclaw.persistence.models import Job, Step
from hermclaw.verifier import VerificationStep, Verifier

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, errors="surrogateescape", env=GIT_ENV
    ).stdout


def commit_all(repo: Path, message: str = "fixture") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def write(repo: Path, rel: str, content: str | bytes) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


def init_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=GIT_ENV)
    for rel, content in (files or {"README.md": "# demo\n"}).items():
        write(path, rel, content)
    commit_all(path, "init")
    return path


def handle(repo: Path, job_id: uuid.UUID, base_sha: str | None = None) -> WorkspaceHandle:
    return WorkspaceHandle(
        id=uuid.uuid4(),
        job_id=job_id,
        path=repo,
        branch="hermclaw/test",
        base_branch="main",
        base_sha=base_sha or git(repo, "rev-parse", "HEAD").strip(),
        repository_key="demo",
    )


class GitCliReader:
    """Read-only GitReader via the git CLI (committed + staged + unstaged + untracked relative to base_sha)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def status(self, workspace: WorkspaceHandle) -> list[GitStatusEntry]:
        self.calls.append("status")
        out = git(workspace.path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries: list[GitStatusEntry] = []
        parts = out.split("\0")
        i = 0
        while i < len(parts):
            rec = parts[i]
            if len(rec) < 4:
                i += 1
                continue
            code, path = rec[:2], rec[3:]
            if code[0] in "RC":
                entries.append(GitStatusEntry(path=path, status=code, orig_path=parts[i + 1]))
                i += 2
                continue
            entries.append(GitStatusEntry(path=path, status=code))
            i += 1
        return entries

    async def diff(self, workspace: WorkspaceHandle, paths: list[str] | None = None, *, max_bytes: int = 200_000) -> str:
        self.calls.append("diff")
        args = ["diff", "--no-renames", workspace.base_sha]
        if paths:
            args += ["--", *paths]
        return git(workspace.path, *args)[:max_bytes]

    async def changed_files(self, workspace: WorkspaceHandle) -> list[str]:
        self.calls.append("changed_files")
        tracked = git(workspace.path, "diff", "--no-renames", "--name-only", "-z", workspace.base_sha).split("\0")
        untracked = git(workspace.path, "ls-files", "-z", "--others", "--exclude-standard").split("\0")
        return sorted({p for p in [*tracked, *untracked] if p})


class BrokenGitReader(GitCliReader):
    async def changed_files(self, workspace: WorkspaceHandle) -> list[str]:
        raise RuntimeError("git service down")


@dataclass
class WorkspaceShellExecutor:
    """Runs ``bash -c <command>`` in the workspace directory (like the local sandbox, without isolation)."""

    requests: list[ExecutionRequest] = field(default_factory=list)
    active: int = 0
    max_active: int = 0
    delay: float = 0.0

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        self.requests.append(req)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            path = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "/usr/bin:/bin")])
            env = {"PATH": path, "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8", **req.env}
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                req.command,
                cwd=str(workspace.path),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), req.timeout_seconds)
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await asyncio.shield(proc.wait())
                raise
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
                return CommandResult(request_id=uuid.uuid4().hex, exit_code=None, timed_out=True, sandbox="local", error="timed out")
            return CommandResult(
                request_id=uuid.uuid4().hex,
                exit_code=proc.returncode,
                stdout=out.decode("utf-8", errors="replace"),
                stderr=err.decode("utf-8", errors="replace"),
                duration_ms=int((time.monotonic() - started) * 1000),
                sandbox="local",
            )
        finally:
            self.active -= 1


class RaisingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        self.calls += 1
        raise ConnectionError("execution worker unreachable token=supersecretvalue123")


class HangingExecutor:
    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


async def make_job_step(sm: async_sessionmaker[AsyncSession], *, kind: str = "implement") -> tuple[uuid.UUID, uuid.UUID]:
    async with sm() as s:
        job = Job(title="verifier test", prompt="verify")
        s.add(job)
        await s.flush()
        step = Step(job_id=job.id, step_key="S001", title="Implement it", kind=kind, capability="code.implement", goal="change the code")
        s.add(step)
        await s.flush()
        ids = (job.id, step.id)
        await s.commit()
        return ids


def scope(**kw: Any) -> ScopeContract:
    return ScopeContract(**kw)


def step(
    kind: str = "implement",
    *,
    acceptance: Sequence[Any] | None = None,
    scope_contract: ScopeContract | None = None,
    network: bool = False,
) -> VerificationStep:
    return VerificationStep.build(key="S001", kind=kind, acceptance=list(acceptance or []), scope=scope_contract, network=network)


def make_verifier(sm: async_sessionmaker[AsyncSession], executor: Any = None, git_reader: Any = None, **policy: Any) -> Verifier:
    policies = PoliciesConfig.model_validate({"verifier": policy}) if policy else PoliciesConfig()
    return Verifier(sm, policies, executor or WorkspaceShellExecutor(), git_reader or GitCliReader())


def by_name(report: Any) -> dict[str, Any]:
    return {c.name: c for c in report.checks}


def of_type(report: Any, check_type: str) -> list[Any]:
    return [c for c in report.checks if c.check_type == check_type]
