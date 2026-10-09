"""Shared helpers for the P17 tool engine tests (no tests in this module).

- :class:`GitCliReader` – a real, read-only ``GitReader`` on top of the git CLI (the gitops implementation is a
  separate component; the engine is coded against the protocol);
- :class:`ScriptedRepo` – scripted ``RepoContextProvider`` fake;
- :class:`RecordingCallbacks` – scripted ``ToolCallbacks`` fake;
- DB helpers (job/step rows, tool_calls/command_runs/test_runs/events queries) and an engine factory.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.scope import ScopeContract, ScopeExpansionRequest
from hermclaw.contracts.tools import CoderAction, ToolName, ToolResult
from hermclaw.core.config import PoliciesConfig, load_config
from hermclaw.core.interfaces import CommandExecutor, GitStatusEntry, RepoHit, WorkspaceHandle
from hermclaw.core.settings import Settings
from hermclaw.persistence.models import CommandRun, Event, Job, Step, TestRun, ToolCallRow
from hermclaw.scope.guard import ScopeGuard
from hermclaw.tools.context import ScopeExpansionOutcome, ToolPermissions
from hermclaw.tools.engine import ToolEngine
from hermclaw.tools.executors import SubprocessExecutor

SM = async_sessionmaker[AsyncSession]
ROOT = Path(__file__).resolve().parents[2]
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout


def commit_all(repo: Path, message: str = "fixture") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def policies() -> PoliciesConfig:
    return load_config(ROOT / "config").policies


def dev_settings() -> Settings:
    return Settings(env="test")


class GitCliReader:
    """Read-only GitReader via the git CLI (test implementation of the protocol)."""

    async def status(self, workspace: WorkspaceHandle) -> list[GitStatusEntry]:
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
        args = ["diff", workspace.base_sha]
        if paths:
            args += ["--", *paths]
        return git(workspace.path, *args)[:max_bytes]

    async def changed_files(self, workspace: WorkspaceHandle) -> list[str]:
        tracked = git(workspace.path, "diff", "--name-only", workspace.base_sha).splitlines()
        untracked = git(workspace.path, "ls-files", "--others", "--exclude-standard").splitlines()
        return sorted({p for p in [*tracked, *untracked] if p})


class BrokenGitReader(GitCliReader):
    async def status(self, workspace: WorkspaceHandle) -> list[GitStatusEntry]:
        raise RuntimeError("git service down")

    async def changed_files(self, workspace: WorkspaceHandle) -> list[str]:
        raise RuntimeError("git service down")


@dataclass
class ScriptedRepo:
    hits: list[RepoHit] = field(default_factory=list)
    symbols: list[RepoHit] = field(default_factory=list)
    fail: bool = False
    queries: list[str] = field(default_factory=list)

    async def inventory_summary(self, workspace: WorkspaceHandle) -> dict[str, Any]:
        return {}

    async def search(self, workspace: WorkspaceHandle, query: str, *, k: int = 20) -> list[RepoHit]:
        self.queries.append(query)
        if self.fail:
            raise ConnectionError("index offline")
        return self.hits[:k]

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]:
        self.queries.append(name)
        if self.fail:
            raise ConnectionError("index offline")
        return [h for h in self.symbols if name in h.snippet][:k]

    async def read(self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000) -> str:
        return ""

    async def context_for(self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int = 24_000) -> list[RepoHit]:
        return []


@dataclass
class RecordingCallbacks:
    research_answer: str = "summary: use the documented API"
    expansion: ScopeExpansionOutcome | None = None
    fail: bool = False
    research: list[str] = field(default_factory=list)
    expansions: list[ScopeExpansionRequest] = field(default_factory=list)
    replans: list[str] = field(default_factory=list)

    async def on_research(self, question: str) -> str:
        self.research.append(question)
        if self.fail:
            raise ConnectionError("research backend down")
        return self.research_answer

    async def on_scope_expansion(self, request: ScopeExpansionRequest) -> ScopeExpansionOutcome:
        self.expansions.append(request)
        if self.fail:
            raise ConnectionError("scope engine down")
        return self.expansion or ScopeExpansionOutcome(False, "denied: semantic change, ask the replanner")

    async def on_replan(self, reason: str) -> None:
        self.replans.append(reason)
        if self.fail:
            raise ConnectionError("planner down")


async def make_step(sm: SM, **fields: Any) -> Step:
    defaults: dict[str, Any] = {
        "step_key": "S001",
        "title": "tool step",
        "kind": "implement",
        "capability": "coding",
        "goal": "change code",
    }
    defaults.update(fields)
    async with sm() as s:
        job = Job(title="tool test", prompt="p")
        s.add(job)
        await s.flush()
        step = Step(job_id=job.id, **defaults)
        s.add(step)
        await s.commit()
        return step


def workspace_for(job_id: uuid.UUID, repo: Path) -> WorkspaceHandle:
    return WorkspaceHandle(
        id=uuid.uuid4(),
        job_id=job_id,
        path=repo,
        branch="hermclaw/test",
        base_branch="main",
        base_sha=git(repo, "rev-parse", "HEAD").strip(),
        repository_key="demo",
    )


def scope(**fields: Any) -> ScopeContract:
    return ScopeContract(**fields)


@dataclass
class Harness:
    engine: ToolEngine
    step: Step
    workspace: WorkspaceHandle
    repo: Path
    sm: SM
    attempt_id: uuid.UUID
    turn: int = 0

    async def call(self, tool: ToolName | str, **args: Any) -> ToolResult:
        self.turn += 1
        return await self.engine.execute(
            CoderAction(tool=ToolName(tool), args=args, status=f"turn {self.turn}"),
            job_id=self.step.job_id,
            step_id=self.step.id,
            attempt_id=self.attempt_id,
            turn=self.turn,
        )


async def make_harness(
    sm: SM,
    repo: Path,
    *,
    contract: ScopeContract | None = None,
    permissions: ToolPermissions | None = None,
    callbacks: Any = None,
    executor: CommandExecutor | None = None,
    repo_provider: Any = None,
    git_reader: Any = None,
    policy: PoliciesConfig | None = None,
    step_fields: dict[str, Any] | None = None,
) -> Harness:
    pol = policy or policies()
    step = await make_step(sm, **(step_fields or {}))
    ws = workspace_for(step.job_id, repo)
    guard = ScopeGuard(contract, pol.scope) if contract is not None else None
    engine = ToolEngine(
        sm,
        pol,
        ws,
        guard,
        executor or SubprocessExecutor(pol.sandbox, settings=dev_settings()),
        git_reader or GitCliReader(),
        repo_provider or ScriptedRepo(),
        callbacks or RecordingCallbacks(),
        permissions=permissions,
    )
    return Harness(engine, step, ws, repo, sm, uuid.uuid4())


async def tool_calls(sm: SM, step_id: uuid.UUID) -> list[ToolCallRow]:
    async with sm() as s:
        res = await s.execute(select(ToolCallRow).where(ToolCallRow.step_id == step_id).order_by(ToolCallRow.started_at))
        return list(res.scalars())


async def command_runs(sm: SM, step_id: uuid.UUID) -> list[CommandRun]:
    async with sm() as s:
        res = await s.execute(select(CommandRun).where(CommandRun.step_id == step_id).order_by(CommandRun.created_at))
        return list(res.scalars())


async def fetch_test_runs(sm: SM, step_id: uuid.UUID) -> list[TestRun]:
    async with sm() as s:
        res = await s.execute(select(TestRun).where(TestRun.step_id == step_id).order_by(TestRun.created_at))
        return list(res.scalars())


async def events_for(sm: SM, step_id: uuid.UUID, event_type: str | None = None) -> list[Event]:
    async with sm() as s:
        stmt = select(Event).where(Event.step_id == step_id).order_by(Event.sequence)
        if event_type is not None:
            stmt = stmt.where(Event.event_type == event_type)
        return list((await s.execute(stmt)).scalars())


async def reload_step(sm: SM, step_id: uuid.UUID) -> Step:
    async with sm() as s:
        step = await s.get(Step, step_id)
        assert step is not None
        return step
