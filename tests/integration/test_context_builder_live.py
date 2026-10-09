"""Context builder against a real git work tree (hermclaw.gitops WorkspaceGitReader), real files and real PostgreSQL.

The repository provider is a small file-backed test double (repo intelligence is a separate component); git
status/diff/changed files come from the real read-only GitReader; the telemetry payload is stored as a real
event and read back.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from hermclaw.context_builder import ContextBuilder, ContextBuilderConfig, SectionName, TurnRecord, record_context_report
from hermclaw.contracts.acceptance import TestEvidence
from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import load_config
from hermclaw.core.interfaces import GitReader, RepoContextProvider, RepoHit, WorkspaceHandle
from hermclaw.gitops.reader import WorkspaceGitReader
from hermclaw.gitops.runner import GitRunner
from hermclaw.gitops.scope_guard import gitattributes_lines
from hermclaw.persistence.models import Event
from tests.unit.test_context_builder_support import TOOLS, make_input, make_step

pytestmark = pytest.mark.integration


class FileRepo:
    """RepoContextProvider double over a real directory (lexical search only)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _files(self) -> list[str]:
        out = subprocess.run(
            ["git", "-C", str(self.root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
        )
        return sorted(line for line in out.stdout.splitlines() if line)

    async def inventory_summary(self, workspace: WorkspaceHandle) -> dict[str, Any]:
        files = self._files()
        return {"files": len(files), "languages": sorted({Path(f).suffix.lstrip(".") for f in files if Path(f).suffix})}

    async def search(self, workspace: WorkspaceHandle, query: str, *, k: int = 20) -> list[RepoHit]:
        hits: list[RepoHit] = []
        for f in self._files():
            for i, line in enumerate((self.root / f).read_text(encoding="utf-8").splitlines(), 1):
                if query in line:
                    hits.append(RepoHit(f, i, i, 1.0, line + "\n"))
        return hits[:k]

    async def find_symbol(self, workspace: WorkspaceHandle, name: str, *, k: int = 20) -> list[RepoHit]:
        return await self.search(workspace, f"def {name}", k=k)

    async def read(self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000) -> str:
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise PermissionError(path)
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        return "".join(lines[start - 1 : end])[:max_chars]

    async def context_for(self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int = 24_000) -> list[RepoHit]:
        words = [w.strip(".,()") for w in goal.split() if len(w) > 3]
        hits: list[RepoHit] = []
        for w in words:
            hits.extend(await self.search(workspace, w, k=3))
        return hits


def git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()


@pytest.fixture
def workspace(tmp_repo: Path) -> WorkspaceHandle:
    (tmp_repo / "tests").mkdir()
    (tmp_repo / "tests" / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
    )
    git(tmp_repo, "add", "-A")
    git(tmp_repo, "commit", "-q", "-m", "tests")
    base = git(tmp_repo, "rev-parse", "HEAD")
    git(tmp_repo, "checkout", "-q", "-b", "hermclaw/job-ctx")
    return WorkspaceHandle(
        id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        path=tmp_repo,
        branch="hermclaw/job-ctx",
        base_branch="main",
        base_sha=base,
        repository_key="demo",
    )


async def test_real_git_diff_status_and_files(workspace: WorkspaceHandle) -> None:
    root = workspace.path
    (root / "app.py").write_text("def add(a, b):\n    return a + b + 0\n\n\ndef sub(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "notes.md").write_text("untracked notes\n", encoding="utf-8")
    (root / ".env").write_text("SECRET_TOKEN=abcdef0123456789\n", encoding="utf-8")
    forbidden = list(load_config().policies.scope.always_forbidden)
    # what gitops does at workspace creation: hide always_forbidden files from diffs
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "attributes").write_text("\n".join(gitattributes_lines(forbidden)) + "\n", encoding="utf-8")
    reader: GitReader = WorkspaceGitReader(GitRunner(author_name="t", author_email="t@x"), forbidden_globs=forbidden)
    repo: RepoContextProvider = FileRepo(root)
    assert isinstance(repo, RepoContextProvider)
    cfg = ContextBuilderConfig.from_config(load_config())
    builder = ContextBuilder(repo, reader, cfg)
    step = make_step(
        goal="Add a sub function to app.py next to add.",
        acceptance=[TestEvidence(command="pytest -q tests/test_app.py", framework="pytest")],
        scope=ScopeContract(target_paths=["app.py"], allowed_new_paths=["tests/test_sub.py"]),
    )
    failure = f'Traceback (most recent call last):\n  File "{root}/tests/test_app.py", line 5, in test_add\nAssertionError\n'
    inp = make_input(step=step, workspace=workspace, latest_failure=failure, tools=TOOLS)
    built = await builder.build(inp)
    user = built.messages[1].content
    assert not built.report.warnings, built.report.warnings
    # current diff from real git, incl. untracked file; .env never shown
    assert "diff --git a/app.py b/app.py" in user and "+def sub(a, b):" in user
    assert "notes.md" in user
    assert "abcdef0123456789" not in user
    # repo facts from real status / changed files
    facts = user.split("## CURRENT REPO FACTS\n", 1)[1].split("\n## ", 1)[0]
    assert "app.py" in facts and "notes.md" in facts and ".env" not in facts
    # code: the real target file content; tests: acceptance + failure reference to the real test file
    assert "### app.py lines 1-6 [target, context]" in user and "return a + b + 0" in user
    diff_section = user.split("## CURRENT DIFF\n", 1)[1].split("\n## ", 1)[0]
    assert ".env" not in diff_section and "[changes to 1 excluded path(s) not shown]" in diff_section
    assert "### tests/test_app.py lines 1-5 [failure, acceptance" in user
    assert built.report.estimated_prompt_tokens <= builder.plan.total_tokens
    # determinism against the real tree
    again = await builder.build(inp)
    assert again.report.fingerprint == built.report.fingerprint


async def test_report_is_persisted_as_event_payload(workspace: WorkspaceHandle, session: Any) -> None:
    reader = WorkspaceGitReader(GitRunner(author_name="t", author_email="t@x"))
    builder = ContextBuilder(FileRepo(workspace.path), reader, ContextBuilderConfig())
    history = [TurnRecord(1, "read_file", '{"path": "app.py"}', True, "2 lines")]
    built = await builder.build(make_input(workspace=workspace, history=history, latest_failure="E   boom\n" * 5000))
    attempt = uuid.uuid4()
    await record_context_report(
        session, built.report, event_type=EventType.MODEL_INVOCATION_STARTED, attempt_id=attempt, source_id="coder-main"
    )
    await session.flush()
    row = (await session.execute(select(Event).where(Event.attempt_id == attempt))).scalar_one()
    assert row.source_type == "context_builder" and row.event_type == EventType.MODEL_INVOCATION_STARTED
    ctx = row.payload["context"]
    assert ctx["kind"] == "context_report" and ctx["fingerprint"] == built.report.fingerprint
    assert ctx["budget"]["total"] == builder.plan.total_tokens  # counters survive the event-store redaction
    assert ctx["estimated_prompt"] == built.report.estimated_prompt_tokens
    sections = {s["name"]: s for s in ctx["sections"]}
    assert sections[SectionName.LATEST_FAILURE.value]["truncated"] is True
    assert isinstance(sections[SectionName.RELEVANT_CODE.value]["budget"], int)
    assert "boom" not in str(row.payload)  # telemetry never contains content
