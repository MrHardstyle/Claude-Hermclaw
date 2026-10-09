"""Test doubles for the context builder (fakes live only in tests): scripted RepoContextProvider / GitReader."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermclaw.context_builder import (
    ContextBuilder,
    ContextBuilderConfig,
    CorrectionItem,
    StepBrief,
    ToolPromptSpec,
    TurnContextInput,
    TurnRecord,
)
from hermclaw.contracts.acceptance import TestEvidence
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.errors import NotFoundError
from hermclaw.core.interfaces import GitReader, GitStatusEntry, RepoContextProvider, RepoHit, WorkspaceHandle

WS = WorkspaceHandle(
    id=uuid.UUID(int=1),
    job_id=uuid.UUID(int=2),
    path=Path("/srv/hermclaw/workspaces/job-1/repo"),
    branch="hermclaw/abc-demo",
    base_branch="main",
    base_sha="0123456789abcdef0123456789abcdef01234567",
    repository_key="demo",
)


@dataclass
class FakeRepo:
    files: dict[str, str] = field(default_factory=dict)
    context_hits: list[RepoHit] = field(default_factory=list)
    search_hits: dict[str, list[RepoHit]] = field(default_factory=dict)
    inventory: dict[str, Any] = field(default_factory=lambda: {"languages": ["python"], "files": 3})
    fail: set[str] = field(default_factory=set)  # method names that raise
    delay: dict[str, float] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)

    async def _maybe(self, name: str) -> None:
        if name in self.delay:
            await asyncio.sleep(self.delay[name])
        if name in self.fail:
            raise RuntimeError(f"{name} exploded password=hunter2hunter2")

    async def inventory_summary(self, workspace: WorkspaceHandle) -> dict[str, Any]:
        self.calls.append(("inventory_summary", None))
        await self._maybe("inventory_summary")
        return dict(self.inventory)

    async def search(self, workspace: WorkspaceHandle, query: str, *, k: int = 20) -> list[RepoHit]:
        self.calls.append(("search", query))
        await self._maybe("search")
        return list(self.search_hits.get(query, []))[:k]

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]:
        self.calls.append(("find_symbol", name))
        return []

    async def read(self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000) -> str:
        self.calls.append(("read", (path, start, end)))
        await self._maybe("read")
        if path not in self.files:
            raise NotFoundError(f"no such file {path}")
        lines = self.files[path].splitlines(keepends=True)
        chunk = "".join(lines[start - 1 : end if end is not None else len(lines)])
        return chunk[:max_chars]

    async def context_for(self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int = 24_000) -> list[RepoHit]:
        self.calls.append(("context_for", goal))
        await self._maybe("context_for")
        return list(self.context_hits)


@dataclass
class FakeGit:
    diff_text: str = ""
    status_entries: list[GitStatusEntry] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)

    async def status(self, workspace: WorkspaceHandle) -> list[GitStatusEntry]:
        if "status" in self.fail:
            raise RuntimeError("status failed")
        return list(self.status_entries)

    async def diff(self, workspace: WorkspaceHandle, paths: list[str] | None = None, *, max_bytes: int = 200_000) -> str:
        if "diff" in self.fail:
            raise RuntimeError("diff failed")
        return self.diff_text[:max_bytes]

    async def changed_files(self, workspace: WorkspaceHandle) -> list[str]:
        if "changed_files" in self.fail:
            raise RuntimeError("changed failed")
        return list(self.changed)


def numbered(n: int, prefix: str = "line") -> str:
    return "".join(f"{prefix} {i}\n" for i in range(1, n + 1))


APP = "".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(1, 41))  # 120 lines
TEST_APP = "import app\n\n" + "".join(f"def test_f{i}():\n    assert app.f{i}(1) == {i + 1}\n\n" for i in range(1, 11))

TOOLS = [
    ToolPromptSpec(
        "read_range",
        "Read lines start..end of a file.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
            "required": ["path", "start", "end"],
        },
    ),
    ToolPromptSpec(
        "replace_text",
        "Replace exact text in a file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "count": {"type": "integer", "default": 1},
            },
            "required": ["path", "old", "new"],
        },
    ),
    ToolPromptSpec(
        "run_test", "Run a test command.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    ),
    ToolPromptSpec(
        "complete_step",
        "Finish the step.",
        {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "changed_files": {"type": "array", "items": {"type": "string"}}},
            "required": ["summary"],
        },
    ),
    ToolPromptSpec("block_step", "Stop the step.", {"type": "object", "properties": {"reason_code": {"enum": ["other", "test_conflict"]}}}),
]


def default_repo() -> FakeRepo:
    return FakeRepo(
        files={"app.py": APP, "tests/test_app.py": TEST_APP, "util.py": numbered(30, "util")},
        context_hits=[RepoHit("util.py", 5, 9, 0.8, "".join(f"util {i}\n" for i in range(5, 10)))],
        search_hits={"app": [RepoHit("tests/test_app.py", 3, 5, 0.9, ""), RepoHit("app.py", 1, 2, 0.5, "")]},
    )


def make_step(**overrides: Any) -> StepBrief:
    data: dict[str, Any] = {
        "goal": "Fix the off-by-one error in f3 of app.py so that test_f3 passes.",
        "kind": "implement",
        "title": "Fix f3",
        "step_key": "S002",
        "constraints": ["Do not change the public API.", "Keep the style of the module."],
        "acceptance": [TestEvidence(command="pytest -q tests/test_app.py::test_f3", framework="pytest")],
        "scope": ScopeContract(target_paths=["app.py"], allowed_new_paths=["tests/test_extra.py"], forbidden_paths=["vendor/**"]),
        "repo_hints": ["app.py"],
    }
    data.update(overrides)
    return StepBrief(**data)


def make_input(**overrides: Any) -> TurnContextInput:
    data: dict[str, Any] = {
        "step": make_step(),
        "workspace": WS,
        "turn": 3,
        "max_turns": 20,
        "tools": TOOLS,
        "completion_contract": "Call complete_step with summary, changed_files and tests_run when test_f3 passes.",
        "history": [
            TurnRecord(1, "read_file", '{"path": "app.py"}', True, "120 lines"),
            TurnRecord(2, "run_test", '{"command": "pytest -q"}', False, "1 failed, 9 passed", "test_failed"),
        ],
        "latest_failure": None,
        "correction": [],
    }
    data.update(overrides)
    return TurnContextInput(**data)


def make_builder(
    repo: RepoContextProvider | None = None, git: GitReader | None = None, **cfg: Any
) -> tuple[ContextBuilder, FakeRepo | RepoContextProvider, FakeGit | GitReader]:
    r = repo or default_repo()
    g = git or FakeGit()
    return ContextBuilder(r, g, ContextBuilderConfig(**cfg)), r, g


def correction_items() -> list[CorrectionItem]:
    return [CorrectionItem("verifier", "test:pytest", "tests/test_app.py::test_f3 failed: assert 3 == 4", path="tests/test_app.py")]


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(default_repo(), RepoContextProvider)
    assert isinstance(FakeGit(), GitReader)
