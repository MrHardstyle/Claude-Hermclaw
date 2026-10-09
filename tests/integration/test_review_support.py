"""Test helpers for the heavy-review tests (no tests in here).

- ``ScriptedChatModel``: a fake ``ChatModel`` whose ``structured`` behaves like the gateway (validate the scripted raw
  answer with the requested schema, ≤ ``max_repairs`` repairs, then ``ModelOutputInvalid``); records every call.
- ``GitCliReader``: read-only ``GitReader`` over the real git CLI (committed + uncommitted + untracked vs base_sha).
- ``StaticRepoContext``: ``RepoContextProvider`` returning fixed hits.
- builders for steps, verifier reports, configs and the job/step rows a review run references.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.verification import VerificationCheck, VerificationReport
from hermclaw.core.config import HermclawConfig, ReviewPolicy, get_config
from hermclaw.core.errors import ModelOutputInvalid
from hermclaw.core.interfaces import GitStatusEntry, RepoHit, WorkspaceHandle
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.persistence.models import Event, Job, ReviewFindingRow, ReviewRun, Step

T = TypeVar("T", bound=BaseModel)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "GIT_CONFIG_NOSYSTEM": "1",
}

Answer = dict[str, Any] | str | BaseException | Callable[[], Any]


@dataclass
class StructuredCall:
    alias: str
    messages: list[ChatMessage]
    schema: type[BaseModel]
    ctx: CallContext
    max_repairs: int
    max_tokens: int | None
    temperature: float | None
    timeout_seconds: float | None


@dataclass
class ScriptedChatModel:
    """Scripted answers; each element is consumed by one model *attempt* (first answer or a repair)."""

    answers: list[Answer] = field(default_factory=list)
    delay_seconds: float = 0.0
    calls: list[StructuredCall] = field(default_factory=list)
    attempts: int = 0

    async def chat(
        self,
        alias: str,
        messages: list[ChatMessage],
        *,
        ctx: CallContext,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> ChatResult:
        raise AssertionError("the heavy reviewer must use structured()")

    async def structured(
        self,
        alias: str,
        messages: list[ChatMessage],
        schema: type[T],
        *,
        ctx: CallContext,
        max_repairs: int = 2,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
    ) -> StructuredResult[T]:
        self.calls.append(StructuredCall(alias, list(messages), schema, ctx, max_repairs, max_tokens, temperature, timeout_seconds))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        errors: list[str] = []
        for attempt in range(max_repairs + 1):
            if not self.answers:
                raise AssertionError("ScriptedChatModel ran out of answers")
            answer = self.answers.pop(0)
            self.attempts += 1
            if isinstance(answer, BaseException):
                raise answer
            if callable(answer):
                answer = answer()
                if asyncio.iscoroutine(answer):
                    answer = await answer
            content = answer if isinstance(answer, str) else json.dumps(answer)
            try:
                value = schema.model_validate(json.loads(content))
            except (json.JSONDecodeError, ValidationError) as exc:
                errors.append(str(exc)[:500])
                continue
            return StructuredResult(
                value=value, result=ChatResult(content=content, alias=alias, model="qwen3.8:27b"), repair_attempts=attempt
            )
        raise ModelOutputInvalid(
            f"'{alias}' returned no valid {schema.__name__} after {max_repairs} repair attempts",
            details={"alias": alias, "schema": schema.__name__, "attempts": max_repairs + 1, "errors": errors},
        )


# ----------------------------------------------------------------------------------------------- git / repo
def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, errors="surrogateescape", env=GIT_ENV
    ).stdout


def write(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class GitCliReader:
    """Read-only GitReader via the git CLI; untracked files appear in the diff via ``git add -N`` on a temp index."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def status(self, workspace: WorkspaceHandle) -> list[GitStatusEntry]:
        out = git(workspace.path, "status", "--porcelain=v1", "--untracked-files=all")
        return [GitStatusEntry(path=line[3:], status=line[:2]) for line in out.splitlines() if len(line) > 3]

    def _env_index(self, workspace: WorkspaceHandle) -> dict[str, str]:
        index = workspace.path / ".git" / "review-test-index"
        env = {**GIT_ENV, "GIT_INDEX_FILE": str(index)}
        subprocess.run(["git", "-C", str(workspace.path), "read-tree", "HEAD"], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", str(workspace.path), "add", "-A", "-N"], check=True, env=env, capture_output=True)
        return env

    async def diff(self, workspace: WorkspaceHandle, paths: list[str] | None = None, *, max_bytes: int = 200_000) -> str:
        self.calls.append("diff")
        if self.fail:
            raise OSError("git diff failed: repository is corrupt")
        env = self._env_index(workspace)
        args = ["git", "-C", str(workspace.path), "diff", "--no-renames", workspace.base_sha]
        if paths:
            args += ["--", *paths]
        out = subprocess.run(args, check=True, env=env, capture_output=True, text=True).stdout
        return out[:max_bytes]

    async def changed_files(self, workspace: WorkspaceHandle) -> list[str]:
        self.calls.append("changed_files")
        env = self._env_index(workspace)
        out = subprocess.run(
            ["git", "-C", str(workspace.path), "diff", "--no-renames", "--name-only", workspace.base_sha],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        ).stdout
        return sorted({p for p in out.splitlines() if p})


@dataclass
class StaticRepoContext:
    hits: list[RepoHit] = field(default_factory=list)
    error: BaseException | None = None
    queries: list[str] = field(default_factory=list)

    async def inventory_summary(self, workspace: WorkspaceHandle) -> dict[str, Any]:
        return {}

    async def search(self, workspace: WorkspaceHandle, query: str, *, k: int = 20) -> list[RepoHit]:
        return self.hits[:k]

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]:
        return []

    async def read(
        self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000
    ) -> str:
        return ""

    async def context_for(self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int = 24_000) -> list[RepoHit]:
        self.queries.append(goal)
        if self.error is not None:
            raise self.error
        return list(self.hits)


def handle(repo: Path, job_id: uuid.UUID, base_sha: str) -> WorkspaceHandle:
    return WorkspaceHandle(
        id=uuid.uuid4(), job_id=job_id, path=repo, branch="hermclaw/test", base_branch="main", base_sha=base_sha, repository_key="demo"
    )


# ----------------------------------------------------------------------------------------------- contracts / config
def make_step(job_id: uuid.UUID, step_id: uuid.UUID, *, kind: str = "implement", **kw: Any) -> StepContract:
    data: dict[str, Any] = {
        "id": step_id,
        "job_id": job_id,
        "step_key": "S001",
        "title": "Add subtraction",
        "kind": kind,
        "capability": "code.implement",
        "goal": "Add a sub(a, b) function to app.py and test it.",
        "status": "running",
        "constraints": ["Keep the public API backwards compatible"],
        "acceptance": [{"type": "test", "command": "pytest -q", "description": "unit tests pass"}],
        "scope": ScopeContract(target_paths=["app.py"], allowed_new_paths=["tests/test_app.py"]),
    }
    data.update(kw)
    return StepContract.model_validate(data)


def report(*, passed: bool = True, failures: Sequence[VerificationCheck] = (), changed: Sequence[str] = ("app.py",)) -> VerificationReport:
    checks = [VerificationCheck(check_type="syntax", name="python", status="pass"), *failures]
    return VerificationReport(passed=passed, checks=checks, changed_files=list(changed), summary="ok" if passed else "failed")


SIMPLE_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,5 @@\n"
    " def add(a, b):\n"
    "     return a + b\n"
    "+\n"
    "+def sub(a, b):\n"
    "+    return a - b\n"
)


def config(*, timeout_seconds: int | None = None, required_for_kinds: list[str] | None = None, heavy_enabled: bool = True) -> HermclawConfig:
    cfg = get_config()
    review = cfg.policies.review
    updates: dict[str, Any] = {}
    if timeout_seconds is not None:
        updates["timeout_seconds"] = timeout_seconds
    if required_for_kinds is not None:
        updates["required_for_kinds"] = required_for_kinds
    policies = cfg.policies.model_copy(update={"review": ReviewPolicy.model_validate(review.model_dump() | updates)})
    models = cfg.models
    if not heavy_enabled:
        models = models.model_copy(
            update={"profiles": [p.model_copy(update={"enabled": False}) if p.role == "heavy" else p for p in models.profiles]}
        )
    return cfg.model_copy(update={"policies": policies, "models": models})


# ----------------------------------------------------------------------------------------------- database
async def make_job_step(sm: async_sessionmaker[AsyncSession], *, kind: str = "implement") -> tuple[uuid.UUID, uuid.UUID]:
    async with sm() as s:
        job = Job(title="Calculator", prompt="Extend the calculator module with subtraction.")
        s.add(job)
        await s.flush()
        step = Step(job_id=job.id, step_key="S001", title="Add subtraction", kind=kind, capability="code.implement", goal="add sub()")
        s.add(step)
        await s.flush()
        ids = (job.id, step.id)
        await s.commit()
        return ids


async def load_run(sm: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> tuple[ReviewRun, list[ReviewFindingRow]]:
    async with sm() as s:
        run = (await s.execute(select(ReviewRun).where(ReviewRun.id == run_id))).scalar_one()
        rows = list((await s.execute(select(ReviewFindingRow).where(ReviewFindingRow.review_run_id == run_id))).scalars())
        return run, rows


async def events_for(sm: async_sessionmaker[AsyncSession], step_id: uuid.UUID) -> list[Event]:
    async with sm() as s:
        return list((await s.execute(select(Event).where(Event.step_id == step_id).order_by(Event.sequence))).scalars())
