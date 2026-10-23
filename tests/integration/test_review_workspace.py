"""WorkspaceReviewer with a real git repository, real PostgreSQL rows (job goal, command_runs) and a repo-context fake."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.config import MemoryPolicy
from hermclaw.core.errors import ExternalServiceError
from hermclaw.core.interfaces import RepoHit, WorkspaceHandle
from hermclaw.memory.store import MemoryStore, import_entries
from hermclaw.persistence.models import CommandRun, MemoryEntry
from hermclaw.review import REVIEW_CONTEXT_ERROR, HeavyReviewer, WorkspaceReviewer, is_test_path
from tests.integration.test_repo_intelligence_support import HashingEmbedder
from tests.integration.test_review_support import (
    GitCliReader,
    ScriptedChatModel,
    StaticRepoContext,
    config,
    events_for,
    git,
    handle,
    load_run,
    make_job_step,
    make_step,
    report,
    write,
)

pytestmark = pytest.mark.integration

SM = async_sessionmaker[AsyncSession]
ENV_SECRET = "DB_PASSWORD=Sup3rS3cretValue!"


def _change_repo(repo: Path) -> str:
    base = git(repo, "rev-parse", "HEAD").strip()
    write(repo, "app.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    write(repo, "tests/test_app.py", "from app import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")  # untracked
    write(repo, ".env", ENV_SECRET + "\n")  # untracked secret file: must never be shown
    return base


async def test_collects_diff_goal_commands_and_snippets(sessionmaker: SM, tmp_repo: Path) -> None:
    base = _change_repo(tmp_repo)
    job_id, step_id = await make_job_step(sessionmaker)
    attempt_id = uuid.uuid4()
    async with sessionmaker() as s:
        s.add(CommandRun(job_id=job_id, step_id=step_id, attempt_id=attempt_id, command="pytest -q tests/test_app.py", exit_code=0))
        s.add(CommandRun(job_id=job_id, step_id=step_id, attempt_id=uuid.uuid4(), command="echo other-attempt", exit_code=0))
        await s.commit()
    repo_ctx = StaticRepoContext(
        hits=[
            RepoHit(path="app.py", start_line=1, end_line=2, snippet="def add(a, b):\n    return a + b\n"),
            RepoHit(path=".env", start_line=1, end_line=1, snippet=ENV_SECRET),
            RepoHit(path="tests/test_app.py", start_line=1, end_line=5, snippet="def test_sub():\n    assert sub(3, 1) == 2\n"),
            RepoHit(path="../outside.py", start_line=1, end_line=1, snippet="print('x')"),
        ]
    )
    chat = ScriptedChatModel([{"verdict": "pass", "findings": [], "summary": "fine"}])
    adapter = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), GitCliReader(), repo_ctx)
    step = make_step(job_id, step_id)

    review = await adapter.review(
        step,
        handle(tmp_repo, job_id, base),
        report(changed=("app.py", "tests/test_app.py")),
        job_id=job_id,
        step_id=step_id,
        attempt_id=attempt_id,
    )

    assert review.verdict == "pass"
    user = chat.calls[0].messages[1].content
    assert "Extend the calculator module with subtraction." in user  # job goal from PostgreSQL
    assert "+def sub(a, b):" in user and "### FILE: app.py" in user
    assert "### FILE: tests/test_app.py [added" in user  # untracked file part of the diff
    assert "### FILE: .env" in user and "content withheld by policy" in user
    assert "Sup3rS3cret" not in user  # neither from the diff nor from the snippets
    assert "pytest -q tests/test_app.py" in user and "other-attempt" not in user  # only this attempt's commands
    snippets = user.split("## RELEVANT CODE AND TESTS", 1)[1]
    assert snippets.index("tests/test_app.py:1-5 (test)") < snippets.index("app.py:1-2 (code)")  # tests first
    assert "outside.py" not in snippets
    assert "Add subtraction" in repo_ctx.queries[0] and "app.py" in repo_ctx.queries[0]


async def test_documents_named_as_rules_are_read_from_the_workspace(sessionmaker: SM, tmp_repo: Path) -> None:
    """BUG-290: README.md named in the step goal and hinted reaches the review whole; a protected or unnamed document
    and a path outside the workspace do not."""
    base = _change_repo(tmp_repo)
    write(tmp_repo, "README.md", "# Calculator\n\nRules: sub(a, b) accepts whole numbers only.\n")
    write(tmp_repo, "NOTES.md", "unrelated notes\n")
    job_id, step_id = await make_job_step(sessionmaker)
    chat = ScriptedChatModel([{"verdict": "pass", "findings": [], "summary": "fine"}])
    adapter = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), GitCliReader(), StaticRepoContext(hits=[]))
    step = make_step(
        job_id,
        step_id,
        goal="Add sub(a, b) to app.py following the rules in README.md.",
        repo_hints=["app.py", "README.md", "NOTES.md", "../outside.md", ".env.md"],
    )
    inp = await adapter.collect(step, handle(tmp_repo, job_id, base), report(), job_id=job_id, step_id=step_id, attempt_id=None)
    assert [p for p, _ in inp.requirement_docs] == ["README.md"]
    assert "whole numbers only" in inp.requirement_docs[0][1]


async def test_conventions_for_code_the_change_adds_reach_the_review(sessionmaker: SM, tmp_repo: Path) -> None:
    """Ladder A10 #8: the coder was given the convention about SQL LIKE with user text when it wrote the search and did
    not apply it, and the review never heard of it. Entries whose code patterns match an added line reach the review;
    one about other code, one scoped to another repository and a pattern found only in removed lines do not."""
    async with sessionmaker() as s:
        await s.execute(delete(MemoryEntry))
        await s.commit()
    policy = MemoryPolicy()
    entries = [
        {"kind": "convention", "title": "SQL LIKE with user text", "content": "escape % and _ with ESCAPE", "tags": [r"code:\bLIKE\b"]},
        {"kind": "convention", "title": "Strict dates", "content": "YYYY-MM-DD only", "tags": [r"code:strptime\("]},
        {"kind": "convention", "title": "Other repo", "content": "not here", "tags": [r"code:\bLIKE\b"], "scope": "repo:elsewhere"},
        {"kind": "convention", "title": "Removed code", "content": "about sub", "tags": [r"code:return a - b"]},
    ]
    await import_entries(sessionmaker, HashingEmbedder(), policy, entries)
    _change_repo(tmp_repo)
    git(tmp_repo, "add", "-A")
    git(tmp_repo, "commit", "-qm", "sub")
    base = git(tmp_repo, "rev-parse", "HEAD").strip()
    write(tmp_repo, "app.py", 'def find(db, q):\n    return db.execute("SELECT * FROM t WHERE title LIKE ?", (f"%{q}%",))\n')
    job_id, step_id = await make_job_step(sessionmaker)
    chat = ScriptedChatModel([{"verdict": "pass", "findings": [], "summary": "fine"}])
    memory = MemoryStore(sessionmaker, HashingEmbedder(), policy)
    adapter = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), GitCliReader(), StaticRepoContext(hits=[]), memory)
    step = make_step(job_id, step_id)
    ws = handle(tmp_repo, job_id, base)
    inp = await adapter.collect(step, ws, report(changed=("app.py",)), job_id=job_id, step_id=step_id, attempt_id=None)
    assert len(inp.conventions) == 1 and "SQL LIKE with user text" in inp.conventions[0]
    await adapter.review(step, ws, report(changed=("app.py",)), job_id=job_id, step_id=step_id, attempt_id=None)
    user = chat.calls[0].messages[1].content
    assert "## CONVENTIONS FOR CODE LIKE THIS CHANGE" in user and "escape % and _ with ESCAPE" in user
    assert "YYYY-MM-DD" not in user and "not here" not in user and "about sub" not in user
    plain = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), GitCliReader(), StaticRepoContext(hits=[]))
    assert (await plain.collect(step, ws, report(), job_id=job_id, step_id=step_id, attempt_id=None)).conventions == []


async def test_review_outcome_persists_run(sessionmaker: SM, tmp_repo: Path) -> None:
    base = _change_repo(tmp_repo)
    job_id, step_id = await make_job_step(sessionmaker)
    chat = ScriptedChatModel(
        [{"verdict": "fix_required", "findings": [{"severity": "major", "path": "app.py", "summary": "no type hints"}]}]
    )
    adapter = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), GitCliReader())
    outcome = await adapter.review_outcome(
        make_step(job_id, step_id), handle(tmp_repo, job_id, base), report(), job_id=job_id, step_id=step_id, attempt_id=None
    )
    assert outcome.status == "completed" and outcome.verdict == "fix_required"
    _, rows = await load_run(sessionmaker, outcome.review_run_id)
    assert [r.summary for r in rows] == ["no type hints"]


async def test_unchanged_workspace_fails_closed_for_implement(sessionmaker: SM, tmp_repo: Path) -> None:
    base = git(tmp_repo, "rev-parse", "HEAD").strip()
    job_id, step_id = await make_job_step(sessionmaker)
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}])
    adapter = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), GitCliReader())
    review = await adapter.review(
        make_step(job_id, step_id), handle(tmp_repo, job_id, base), report(changed=()), job_id=job_id, step_id=step_id, attempt_id=None
    )
    assert review.verdict == "fix_required" and "produced no changes" in review.summary
    assert chat.calls == []


async def test_unreadable_diff_fails_closed(sessionmaker: SM, tmp_repo: Path) -> None:
    base = _change_repo(tmp_repo)
    job_id, step_id = await make_job_step(sessionmaker)
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}])
    adapter = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), GitCliReader(fail=True))
    outcome = await adapter.review_outcome(
        make_step(job_id, step_id), handle(tmp_repo, job_id, base), report(), job_id=job_id, step_id=step_id, attempt_id=None
    )
    assert outcome.status == "error" and outcome.error_code == REVIEW_CONTEXT_ERROR and outcome.verdict == "fix_required"
    assert "repository is corrupt" in outcome.reason and chat.calls == []
    run, _ = await load_run(sessionmaker, outcome.review_run_id)
    assert run.status == "error"
    assert [e.event_type for e in await events_for(sessionmaker, step_id)] == [EventType.REVIEW_STARTED, EventType.REVIEW_FINISHED]


async def test_repo_context_failure_only_drops_snippets(sessionmaker: SM, tmp_repo: Path) -> None:
    base = _change_repo(tmp_repo)
    job_id, step_id = await make_job_step(sessionmaker)
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}])
    repo_ctx = StaticRepoContext(error=ExternalServiceError("embedding host asleep"))
    adapter = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), GitCliReader(), repo_ctx)
    review = await adapter.review(
        make_step(job_id, step_id), handle(tmp_repo, job_id, base), report(), job_id=job_id, step_id=step_id, attempt_id=None
    )
    assert review.verdict == "pass"
    assert "RELEVANT CODE AND TESTS" not in chat.calls[0].messages[1].content


def test_should_review_delegates() -> None:
    adapter = WorkspaceReviewer(HeavyReviewer(ScriptedChatModel(), None, config()), GitCliReader())  # type: ignore[arg-type]
    assert adapter.should_review("implement", True) and not adapter.should_review("implement", False)
    assert not adapter.should_review("inventory", True)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_app.py", True),
        ("test_app.py", True),
        ("pkg/app_test.go", True),
        ("web/src/App.test.tsx", True),
        ("web/src/__tests__/x.ts", True),
        ("spec/models/user_spec.rb", True),
        ("app.py", False),
        ("src/contest.py", False),
        ("docs/testing-guide.md", False),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


class _ExplodingReader(GitCliReader):
    async def changed_files(self, workspace: WorkspaceHandle) -> list[str]:
        raise RuntimeError("unexpected reader bug")


async def test_any_collection_error_fails_closed(sessionmaker: SM, tmp_repo: Path) -> None:
    base = _change_repo(tmp_repo)
    job_id, step_id = await make_job_step(sessionmaker)
    chat = ScriptedChatModel([{"verdict": "pass", "findings": []}])
    adapter = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), _ExplodingReader())
    review = await adapter.review(
        make_step(job_id, step_id), handle(tmp_repo, job_id, base), report(), job_id=job_id, step_id=step_id, attempt_id=None
    )
    assert review.verdict == "fix_required" and "RuntimeError" in review.summary and chat.calls == []


async def test_unreachable_model_host_is_no_verdict_but_a_retry(sessionmaker: SM, tmp_repo: Path) -> None:
    """BUG-198: a model-host restart during the heavy review returned a fail-closed fix_required without findings –
    a correction round (implement step) or a replan (job review) for a change nobody had looked at."""
    from hermclaw.core.errors import ModelError, ResourceUnavailable

    base = _change_repo(tmp_repo)
    job_id, step_id = await make_job_step(sessionmaker)
    chat = ScriptedChatModel([ModelError("connection refused", code="MODEL_UNAVAILABLE")])
    adapter = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config()), GitCliReader())
    args = (make_step(job_id, step_id), handle(tmp_repo, job_id, base), report())
    with pytest.raises(ResourceUnavailable, match="MODEL_UNAVAILABLE"):
        await adapter.review(*args, job_id=job_id, step_id=step_id, attempt_id=None)
    # still fail-closed for everything that is about the change or the model's answer
    chat2 = ScriptedChatModel(["not json"] * 3)
    review = await WorkspaceReviewer(HeavyReviewer(chat2, sessionmaker, config()), GitCliReader()).review(
        *args, job_id=job_id, step_id=step_id, attempt_id=None
    )
    assert review.verdict == "fix_required"
