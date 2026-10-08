"""Service interfaces shared between orchestration components (wave 2 contract).

Concrete implementations:
- ``WorkspaceHandle``: created by ``hermclaw.gitops`` (Workspace row + path on the orchestrator).
- ``CommandExecutor``: ``hermclaw.tools.executors`` – ``RemoteSandboxExecutor`` (execution worker .222 via
  WorkerClient + workspace sync) and ``LocalSandboxExecutor`` (worker.execution.sandbox in-process, dev/test).
- ``GitReader``: ``hermclaw.gitops`` (read-only operations exposed to LLM tools).
- ``RepoContextProvider``: ``hermclaw.repo_intelligence.service.RepoIntelligence``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hermclaw.contracts.worker import CommandResult


@dataclass(frozen=True)
class WorkspaceHandle:
    id: uuid.UUID
    job_id: uuid.UUID
    path: Path
    branch: str
    base_branch: str
    base_sha: str
    repository_key: str  # stable key for indexes, e.g. repository name


@dataclass(frozen=True)
class ExecutionRequest:
    command: str
    timeout_seconds: int = 600
    network: bool = False
    env: dict[str, str] = field(default_factory=dict)
    image: str | None = None
    job_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    purpose: str = "tool"  # tool|test|verifier|lint


@runtime_checkable
class CommandExecutor(Protocol):
    """Runs a command for a workspace inside the sandbox and returns its result.

    Implementations must sync file changes made by the command back into ``workspace.path``
    (the orchestrator copy is the single source of truth for Git) and must never run commands
    on the orchestrator host without isolation in production.
    """

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult: ...


@dataclass(frozen=True)
class GitStatusEntry:
    path: str
    status: str  # porcelain XY code, e.g. " M", "??", "D "


@runtime_checkable
class GitReader(Protocol):
    async def status(self, workspace: WorkspaceHandle) -> list[GitStatusEntry]: ...

    async def diff(self, workspace: WorkspaceHandle, paths: list[str] | None = None, *, max_bytes: int = 200_000) -> str: ...

    async def changed_files(self, workspace: WorkspaceHandle) -> list[str]:
        """Paths changed relative to base_sha (committed + uncommitted + untracked, excluding ignored)."""
        ...


@dataclass
class RepoHit:
    path: str
    start_line: int = 1
    end_line: int = 1
    score: float = 0.0
    snippet: str = ""
    signals: dict[str, float] = field(default_factory=dict)


@runtime_checkable
class RepoContextProvider(Protocol):
    async def inventory_summary(self, workspace: WorkspaceHandle) -> dict[str, Any]: ...

    async def search(self, workspace: WorkspaceHandle, query: str, *, k: int = 20) -> list[RepoHit]: ...

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]: ...

    async def read(
        self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000
    ) -> str: ...

    async def context_for(self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int = 24_000) -> list[RepoHit]: ...
