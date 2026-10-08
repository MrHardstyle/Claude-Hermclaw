"""Failure scenarios: stale base SHA (6.11), merge conflicts (6.12), interrupted operations, timeouts, outages."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.errors import MergeConflictError, StaleBaseError
from hermclaw.gitops import GitLabClient, GitRunner
from hermclaw.gitops.errors import GitLabError, GitTimeoutError, WorkspaceStateError
from tests.integration.test_gitops_support import World, git, make_world, write

pytestmark = pytest.mark.integration

SCOPE = ScopeContract(target_paths=["src/**", "app.py", "README.md"], allowed_new_paths=["src/**"], allowed_operations=["create", "modify"])


@pytest.fixture
async def world(sessionmaker, tmp_path):
    return await make_world(sessionmaker, tmp_path)


async def _commit(world: World, ws, job_id, files: dict[str, str], message: str = "work") -> str:
    step_id = await world.step(job_id, key=f"S{len(files)}{abs(hash(message)) % 1000}")
    for rel, content in files.items():
        write(Path(ws.path), rel, content)
    await world.engine.stage_allowed(ws, SCOPE)
    res = await world.engine.commit_verified(ws, message, await world.verification(job_id, step_id))
    return res.sha


def _assert_clean_state(path: Path, head: str) -> None:
    gdir = path / ".git"
    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        assert not (gdir / marker).exists(), marker
    assert git("rev-parse", "HEAD", cwd=path) == head
    assert git("diff", "--name-only", "--diff-filter=U", cwd=path) == ""


# ============================================================================================ 6.11 stale base
async def test_stale_base_detected_with_details(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    fresh = await world.engine.check_base(ws)
    assert not fresh.stale and fresh.remote_sha == ws.base_sha and fresh.commits_behind == 0
    assert await world.engine.ensure_base_current(ws) == fresh
    new_sha = world.upstream.commit({"docs/guide.md": "upstream edit\n"})
    world.upstream.commit({"docs/other.md": "more\n"})
    bs = await world.engine.check_base(ws)
    assert bs.stale and not bs.rewritten and bs.commits_behind == 2 and bs.remote_sha == world.upstream.head()
    assert bs.remote_sha != new_sha
    with pytest.raises(StaleBaseError) as err:
        await world.engine.ensure_base_current(ws)
    d = err.value.details
    assert d["base_sha"] == ws.base_sha and d["remote_sha"] == bs.remote_sha and d["commits_behind"] == 2 and d["base_branch"] == "main"
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "base.check" and op.status == "refused" and op.details["error_code"] == "STALE_BASE_SHA"
    # the workspace's tracking ref is refreshed from the mirror (audited)
    assert git("rev-parse", "refs/remotes/origin/main", cwd=Path(ws.path)) == bs.remote_sha
    assert "workspace.fetch_base" in [o.operation for o in await world.git_ops(job_id)]


async def test_stale_base_detects_rewritten_history(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    world.upstream.force_main({"README.md": "# rewritten\n"})
    bs = await world.engine.check_base(ws)
    assert bs.stale and bs.rewritten


async def test_push_with_require_current_base_refuses_stale(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
    world.upstream.commit({"docs/guide.md": "moved on\n"})
    with pytest.raises(StaleBaseError):
        await world.engine.push_job_branch(ws, require_current_base=True)
    assert world.upstream.head(ws.branch) is None


# ============================================================================================ 6.12 conflict handling
@pytest.mark.parametrize("strategy", ["rebase", "merge"])
async def test_update_to_base_without_conflict(world, strategy):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    mine = await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
    upstream_sha = world.upstream.commit({"docs/guide.md": "upstream\n"})
    res = await world.engine.update_to_base(ws, strategy=strategy)
    assert res.updated and res.new_base_sha == upstream_sha and res.old_base_sha == ws.base_sha and res.old_head_sha == mine
    path = Path(ws.path)
    assert git("merge-base", "--is-ancestor", upstream_sha, "HEAD", cwd=path, check=False) == ""
    assert (path / "docs/guide.md").read_text() == "upstream\n" and (path / "src/module.py").read_text() == "VALUE = 2\n"
    info = await world.engine.get_workspace(ws.id)
    assert info.base_sha == upstream_sha and info.head_sha == res.new_head_sha
    if strategy == "rebase":
        assert git("rev-list", "--count", f"{upstream_sha}..HEAD", cwd=path) == "1"
    else:
        assert git("rev-list", "--count", "--merges", f"{upstream_sha}..HEAD", cwd=path) == "1"
    assert not (await world.engine.check_base(ws)).stale
    op = [o for o in await world.git_ops(job_id) if o.operation == f"base.{strategy}"][-1]
    assert op.status == "ok" and op.sha_after == res.new_head_sha
    # nothing to do the second time
    again = await world.engine.update_to_base(ws, strategy=strategy)
    assert not again.updated and again.new_head_sha == res.new_head_sha


@pytest.mark.parametrize("strategy", ["rebase", "merge"])
async def test_update_to_base_conflict_aborts_cleanly(world, strategy):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    head = await _commit(world, ws, job_id, {"app.py": "def add(a, b):\n    return a + b  # mine\n", "src/module.py": "VALUE = 7\n"})
    world.upstream.commit({"app.py": "def add(a, b):\n    return b + a  # theirs\n"})
    with pytest.raises(MergeConflictError) as err:
        await world.engine.update_to_base(ws, strategy=strategy)
    assert err.value.details["files"] == ["app.py"] and err.value.details["strategy"] == strategy
    _assert_clean_state(path, head)
    assert git("status", "--porcelain", cwd=path) == ""
    info = await world.engine.get_workspace(ws.id)
    assert info.base_sha == ws.base_sha and info.head_sha == head  # unchanged
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == f"base.{strategy}" and op.status == "failed" and op.details["error_code"] == "MERGE_CONFLICT"
    # workspace is still fully usable afterwards
    write(path, "src/module.py", "VALUE = 8\n")
    assert (await world.engine.stage_allowed(ws, SCOPE)).staged_paths == ["src/module.py"]


async def test_update_to_base_preserves_uncommitted_changes(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
    write(path, "src/util.py", "def helper():\n    return 'wip'\n")
    write(path, "src/untracked_wip.py", "WIP = 1\n")
    upstream_sha = world.upstream.commit({"docs/guide.md": "upstream\n"})
    res = await world.engine.update_to_base(ws)
    assert res.updated and res.stashed and res.new_base_sha == upstream_sha
    assert (path / "src/util.py").read_text() == "def helper():\n    return 'wip'\n"
    assert (path / "src/untracked_wip.py").read_text() == "WIP = 1\n"
    assert git("stash", "list", cwd=path) == ""


async def test_update_to_base_autostash_conflict_restores_everything(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    head = await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
    write(path, "README.md", "# my uncommitted readme\n")
    write(path, "src/untracked_wip.py", "WIP = 1\n")
    world.upstream.commit({"README.md": "# upstream readme\n"})
    with pytest.raises(MergeConflictError) as err:
        await world.engine.update_to_base(ws)
    assert err.value.details["phase"] == "autostash" and "README.md" in err.value.details["files"]
    _assert_clean_state(path, head)
    assert (path / "README.md").read_text() == "# my uncommitted readme\n"
    assert (path / "src/untracked_wip.py").read_text() == "WIP = 1\n"
    assert git("stash", "list", cwd=path) == ""


async def test_update_to_base_after_force_pushed_base(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
    rewritten = world.upstream.force_main({"docs/guide.md": "rewritten base\n"})
    res = await world.engine.update_to_base(ws)
    path = Path(ws.path)
    assert res.updated and res.new_base_sha == rewritten
    assert git("rev-list", "--count", f"{rewritten}..HEAD", cwd=path) == "1"  # only the job's own commit replayed
    assert (path / "src/module.py").read_text() == "VALUE = 2\n"


async def test_interrupted_rebase_is_detected_and_recovered(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    head = await _commit(world, ws, job_id, {"app.py": "mine\n"})
    world.upstream.commit({"app.py": "theirs\n"})
    await world.engine.check_base(ws)
    # simulate a crash in the middle of a manual rebase
    git("rebase", "origin/main", cwd=path, check=False)
    assert (path / ".git" / "rebase-merge").exists() or (path / ".git" / "rebase-apply").exists()
    write(path, "src/module.py", "VALUE = 5\n")
    with pytest.raises(WorkspaceStateError):
        await world.engine.stage_allowed(ws, SCOPE)
    result = await world.engine.recover(ws)
    assert any("rebase --abort" in a for a in result.actions) and result.head_sha == head
    _assert_clean_state(path, head)
    assert (await world.engine.status(ws)).branch == ws.branch


# ============================================================================================ timeouts / outages
async def test_git_timeout_kills_process(tmp_path):
    runner = GitRunner(author_name="t", author_email="t@x")
    fifo = tmp_path / "hang"
    import os

    os.mkfifo(fifo)
    git("init", "-q", str(tmp_path / "r"), cwd=tmp_path)
    started = asyncio.get_running_loop().time()
    with pytest.raises(GitTimeoutError) as err:
        # hash-object blocks reading a FIFO nobody writes to
        await runner.run(["hash-object", str(fifo)], cwd=tmp_path / "r", timeout_s=0.5)
    assert asyncio.get_running_loop().time() - started < 5
    assert err.value.code == "GIT_TIMEOUT"


async def test_gitlab_down_raises_gitlab_error():
    client = GitLabClient("http://127.0.0.1:9", token="glpat-unused-token-0000000000000", retries=1, backoff_seconds=0.01, timeout=1.0)
    try:
        with pytest.raises(GitLabError) as err:
            await client.find_open_merge_request("grp/demo", source_branch="x")
        assert err.value.details["attempts"] == 2
    finally:
        await client.aclose()


async def test_mirror_fetch_failure_keeps_workspace_creation_atomic(world):
    job_id = await world.job()
    await world.engine.sync_mirror(world.repo)
    world.upstream.bare.rename(world.upstream.bare.with_name("moved.git"))
    from hermclaw.gitops.errors import GitCommandError

    with pytest.raises(GitCommandError):
        await world.engine.create_workspace(job_id, world.repo)
    assert await world.engine.list_workspaces(job_id) == []
    # fetch=False works from the cached mirror while the upstream is down
    ws = await world.engine.create_workspace(job_id, world.repo, fetch=False)
    assert Path(ws.path).is_dir()
