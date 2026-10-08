"""Shared helpers for the P15 scope tests: real git workspace, scripted fake RepoContextProvider, DB rows.

This module holds no tests; it is imported by ``test_scope_*`` modules (unit/integration/failure).
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

from hermclaw.core.config import HermclawConfig, ScopePolicy, load_config
from hermclaw.core.interfaces import RepoHit, WorkspaceHandle
from hermclaw.persistence.models import Event, Job, ScopeContractRow, Step

SM = async_sessionmaker[AsyncSession]  # fixture type of ``sessionmaker``
ROOT = Path(__file__).resolve().parents[2]
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}

REPO_FILES: dict[str, str] = {
    "src/app/__init__.py": "",
    "src/app/core.py": "from app.helpers import slug\nfrom . import models\n\n\ndef run():\n    return slug('X')\n",
    "src/app/helpers.py": "def slug(value):\n    return value.lower()\n",
    "src/app/models.py": "class User:\n    name = ''\n",
    "src/lib/util.py": "def unrelated():\n    return 1\n",
    "src/web/index.ts": "import { fmt } from './fmt';\nexport const x = fmt(1);\n",
    "src/web/fmt.ts": "export function fmt(n: number): string {\n  return String(n);\n}\n",
    "tests/test_core.py": "from app.core import run\n\n\ndef test_run():\n    assert run() == 'x'\n",
    "tests/test_misc.py": "def test_misc():\n    assert True\n",
    "docs/guide.md": "# Guide\n",
    "legacy/old_module.py": "OLD = 1\n",
    "legacy/older_module.py": "OLDER = 1\n",
    "config/settings.yaml": "a: 1\n",
    ".gitignore": "build/\n*.log\n",
    ".env": "SECRET_VALUE=1\n",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout


def write(repo: Path, rel: str, content: str = "x\n") -> Path:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def build_repo(repo: Path) -> Path:
    """Extend the ``tmp_repo`` fixture with a small multi-language layout, ignored files and a secret."""
    for rel, content in REPO_FILES.items():
        write(repo, rel, content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "layout")
    write(repo, "build/out.js", "ignored\n")
    write(repo, "debug.log", "ignored\n")
    write(repo, "notes/untracked.md", "untracked\n")
    return repo


def make_config(**scope_overrides: Any) -> HermclawConfig:
    cfg = load_config(ROOT / "config")
    if scope_overrides:
        cfg.policies.scope = ScopePolicy(**{**cfg.policies.scope.model_dump(), **scope_overrides})
    return cfg


def hit(path: str, score: float, **signals: float) -> RepoHit:
    return RepoHit(path=path, start_line=1, end_line=3, score=score, snippet="...", signals=dict(signals))


@dataclass
class FakeRepo:
    """Scripted RepoContextProvider: symbol/text hits from dicts, ``read`` from the real workspace."""

    symbols: dict[str, list[RepoHit]] = field(default_factory=dict)
    texts: dict[str, list[RepoHit]] = field(default_factory=dict)
    fail: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def inventory_summary(self, workspace: WorkspaceHandle) -> dict[str, Any]:
        return {}

    async def search(self, workspace: WorkspaceHandle, query: str, *, k: int = 20) -> list[RepoHit]:
        self.calls.append(("search", query))
        if self.fail:
            raise RuntimeError("index offline")
        return list(self.texts.get(query, []))[:k]

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]:
        self.calls.append(("find_symbol", name))
        if self.fail:
            raise RuntimeError("index offline")
        return list(self.symbols.get(name, []))[:k]

    async def read(self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000) -> str:
        self.calls.append(("read", path))
        lines = (workspace.path / path).read_text(encoding="utf-8").splitlines(keepends=True)
        return "".join(lines[start - 1 : end])[:max_chars]

    async def context_for(self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int = 24_000) -> list[RepoHit]:
        return []


async def make_step(sm: async_sessionmaker[AsyncSession], **fields: Any) -> Step:
    defaults: dict[str, Any] = {
        "step_key": "S001",
        "title": "change things",
        "kind": "implement",
        "capability": "coding",
        "goal": "implement the change",
    }
    defaults.update(fields)
    async with sm() as s:
        job = Job(title="scope test", prompt="p")
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


async def scope_rows(sm: async_sessionmaker[AsyncSession], step_id: uuid.UUID) -> list[ScopeContractRow]:
    async with sm() as s:
        res = await s.execute(select(ScopeContractRow).where(ScopeContractRow.step_id == step_id).order_by(ScopeContractRow.version))
        return list(res.scalars())


async def events_for(sm: async_sessionmaker[AsyncSession], step_id: uuid.UUID, event_type: str | None = None) -> list[Event]:
    async with sm() as s:
        stmt = select(Event).where(Event.step_id == step_id).order_by(Event.sequence)
        if event_type is not None:
            stmt = stmt.where(Event.event_type == event_type)
        return list((await s.execute(stmt)).scalars())


async def reload_step(sm: async_sessionmaker[AsyncSession], step_id: uuid.UUID) -> Step:
    async with sm() as s:
        step = await s.get(Step, step_id)
        assert step is not None
        return step
