"""Regression tests from the P06 review: tampered workspaces, foreign commits on the job branch, verification
freshness, lock re-validation, object isolation and the public merge-request operation."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import GitPolicy, PoliciesConfig
from hermclaw.gitops import GitLabClient
from hermclaw.gitops.errors import (
    CommitNotVerifiedError,
    PushRejectedError,
    WorkspaceStateError,
    WorkspaceTamperedError,
)
from hermclaw.persistence.models import Workspace
from tests.integration.test_gitops_support import FakeGitLab, World, git, make_world, write

pytestmark = pytest.mark.integration

SCOPE = ScopeContract(
    target_paths=["app.py", "src/**", "README.md"],
    allowed_new_paths=["src/**", ".gitattributes"],
    allowed_operations=["create", "modify"],
)


@pytest.fixture
async def world(sessionmaker, tmp_path):
    return await make_world(sessionmaker, tmp_path)


async def _commit(world: World, ws, job_id: uuid.UUID, files: dict[str, str], message: str = "work") -> str:
    step_id = await world.step(job_id, key=f"S-{uuid.uuid4().hex[:6]}")
    for rel, content in files.items():
        write(Path(ws.path), rel, content)
    await world.engine.stage_allowed(ws, SCOPE)
    res = await world.engine.commit_verified(ws, message, await world.verification(job_id, step_id))
    return res.sha


# ============================================================================================ tampered .git
async def test_tampered_config_with_filter_driver_is_refused_and_never_executed(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    marker = world.root / "pwned"
    # what a malicious test run inside the sandbox could do to the bind-mounted workspace
    with (path / ".git" / "config").open("a") as fh:
        fh.write(f'[filter "evil"]\n\tclean = "touch {marker}; cat"\n\tsmudge = cat\n')
    write(path, ".gitattributes", "* filter=evil\n")
    write(path, "src/module.py", "VALUE = 2\n")
    with pytest.raises(WorkspaceTamperedError) as err:
        await world.engine.stage_allowed(ws, SCOPE)
    assert any("filter.evil.clean" in p for p in err.value.details["problems"])
    with pytest.raises(WorkspaceTamperedError):
        await world.engine.status(ws)
    with pytest.raises(WorkspaceTamperedError):
        await world.engine.diff(ws)
    with pytest.raises(WorkspaceTamperedError):
        await world.engine.reader().status(ws.to_handle())
    assert not marker.exists(), "the runtime executed a filter driver from a workspace-controlled config"
    ops_ = await world.git_ops(job_id)
    integrity = [o for o in ops_ if o.operation == "workspace.integrity"]
    assert integrity and integrity[-1].status == "refused" and integrity[-1].details["error_code"] == "WORKSPACE_TAMPERED"
    assert "stage" not in [o.operation for o in ops_]  # nothing was staged
    events = [e for e in await world.events(job_id) if e.payload.get("operation") == "workspace.integrity"]
    assert events and events[-1].severity == "warning"
    # cleanup still works and does not run git status (which would invoke the clean filter)
    cleaned = await world.engine.cleanup(ws, force=True)
    assert cleaned.status == "cleaned" and not path.exists() and not marker.exists()
    op = [o for o in await world.git_ops(job_id) if o.operation == "workspace.cleanup"][-1]
    assert op.status == "ok" and op.details["integrity_problems"]


@pytest.mark.parametrize(
    "tamper",
    ["include", "alternates", "insteadof", "uploadpack", "gitfile", "bare"],
)
async def test_other_git_dir_tampering_is_detected(world, tamper):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    gdir = path / ".git"
    if tamper == "include":
        (world.root / "evil.cfg").write_text("[core]\n\tfsmonitor = touch /tmp/x\n")
        git("config", "include.path", str(world.root / "evil.cfg"), cwd=path)
    elif tamper == "alternates":
        (gdir / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (gdir / "objects" / "info" / "alternates").write_text(str(world.upstream.bare / "objects") + "\n")
    elif tamper == "insteadof":
        git("config", "url.file:///tmp/evil.git.insteadOf", world.upstream.url, cwd=path)
    elif tamper == "uploadpack":
        git("config", f"remote.{world.engine.mirror_path(world.repo)}.uploadpack", "touch /tmp/x; git-upload-pack", cwd=path)
    elif tamper == "gitfile":
        real = world.root / "moved-git-dir"
        gdir.rename(real)
        (path / ".git").write_text(f"gitdir: {real}\n")
    elif tamper == "bare":
        git("config", "core.bare", "true", cwd=path)
    with pytest.raises((WorkspaceTamperedError, WorkspaceStateError)):
        await world.engine.check_base(ws)
    with pytest.raises((WorkspaceTamperedError, WorkspaceStateError)):
        await world.engine.push_job_branch(ws)


async def test_untampered_workspace_passes_integrity_through_full_lifecycle(world):
    # every runtime write must keep .git/config inside the allow-list (no false positives)
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
    world.upstream.commit({"docs/guide.md": "upstream\n"})
    write(Path(ws.path), "src/util.py", "def helper():\n    return 'wip'\n")
    await world.engine.update_to_base(ws)
    await world.engine.update_to_base(ws, strategy="merge")
    await world.engine.push_job_branch(ws)
    await world.engine.recover(ws)
    assert not [o for o in await world.git_ops(job_id) if o.operation == "workspace.integrity"]


async def test_removed_forbidden_attributes_are_repaired_by_engine_and_refused_by_reader(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    attrs = path / ".git" / "info" / "attributes"
    original = attrs.read_text()
    attrs.write_text("")
    write(path, ".env", "TOKEN=supersecretvalue123\n")
    reader = world.engine.reader()
    with pytest.raises(WorkspaceTamperedError):
        await reader.diff(ws.to_handle())
    diff = await world.engine.diff(ws)  # the engine re-asserts the runtime-owned attributes first
    assert attrs.read_text() == original
    assert "supersecretvalue123" not in diff.patch
    repair = [o for o in await world.git_ops(job_id) if o.operation == "workspace.attributes"]
    assert repair and repair[-1].status == "ok"
    assert "supersecretvalue123" not in await reader.diff(ws.to_handle())


# ============================================================================================ push: foreign commits
async def test_push_refuses_to_overwrite_foreign_commits_on_the_job_branch(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    first = await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
    await world.engine.push_job_branch(ws)
    # a reviewer pushes a fix-up onto the job branch
    git("fetch", "-q", "origin", cwd=world.upstream.seed)
    git("checkout", "-q", "-b", "review", f"origin/{ws.branch}", cwd=world.upstream.seed)
    write(world.upstream.seed, "docs/guide.md", "reviewer fix\n")
    git("commit", "-q", "-am", "reviewer fix-up", cwd=world.upstream.seed)
    git("push", "-q", "origin", f"HEAD:refs/heads/{ws.branch}", cwd=world.upstream.seed)
    foreign = world.upstream.head(ws.branch)
    git("checkout", "-q", "main", cwd=world.upstream.seed)
    assert foreign != first
    # the runtime commits again on its (now outdated) local branch
    await _commit(world, ws, job_id, {"src/module.py": "VALUE = 3\n"}, message="second")
    with pytest.raises(PushRejectedError) as err:
        await world.engine.push_job_branch(ws)
    assert err.value.details["reason"] == "foreign_commits" and err.value.details["remote_sha"] == foreign
    assert world.upstream.head(ws.branch) == foreign  # nothing overwritten
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "push" and op.status == "refused" and op.details["error_code"] == "PUSH_REJECTED"


async def test_push_may_replace_stale_job_branch_seen_at_workspace_creation(world):
    job_id = await world.job("retry after rewrite")
    branch = world.engine.job_branch(job_id, "retry after rewrite")
    # an earlier attempt of this job left a job branch based on the old main ...
    git("checkout", "-q", "-b", branch, cwd=world.upstream.seed)
    write(world.upstream.seed, "src/module.py", "VALUE = 99\n")
    git("commit", "-q", "-am", "earlier attempt", cwd=world.upstream.seed)
    git("push", "-q", "origin", branch, cwd=world.upstream.seed)
    old_tip = git("rev-parse", "HEAD", cwd=world.upstream.seed)
    git("checkout", "-q", "main", cwd=world.upstream.seed)
    # ... then main was rewritten, so the old branch no longer contains the base
    world.upstream.force_main({"README.md": "# rewritten\n"})
    ws = await world.engine.create_workspace(job_id, world.repo)
    assert ws.head_sha == ws.base_sha != old_tip  # not resumed
    new = await _commit(world, ws, job_id, {"src/module.py": "VALUE = 3\n"})
    res = await world.engine.push_job_branch(ws)
    assert res.forced and res.remote_sha_before == old_tip and world.upstream.head(branch) == new


async def test_push_uses_registered_url_not_workspace_remote(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    sha = await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
    decoy = world.root / "decoy.git"
    git("init", "-q", "--bare", str(decoy), cwd=world.root)
    git("remote", "set-url", "origin", f"file://{decoy}", cwd=Path(ws.path))  # allowed key, but never trusted
    await world.engine.push_job_branch(ws)
    assert world.upstream.head(ws.branch) == sha
    assert git("for-each-ref", cwd=decoy) == ""


# ============================================================================================ commit: freshness
async def test_commit_refuses_verification_older_than_base_update(world):
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    write(path, "src/module.py", "VALUE = 2\n")
    stale_run = await world.verification(job_id, step_id)  # verified before the rebase below
    world.upstream.commit({"docs/guide.md": "upstream\n"})
    res = await world.engine.update_to_base(ws)
    assert res.updated and res.stashed
    await world.engine.stage_allowed(ws, SCOPE)
    with pytest.raises(CommitNotVerifiedError) as err:
        await world.engine.commit_verified(ws, "change", stale_run)
    assert any("predates" in p for p in err.value.details["problems"])
    fresh = await world.verification(job_id, step_id)
    assert (await world.engine.commit_verified(ws, "change", fresh)).files == ["src/module.py"]


async def test_commit_refuses_verification_from_before_the_workspace_existed(world):
    job_id = await world.job()
    step_id = await world.step(job_id)
    early = await world.verification(job_id, step_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    write(Path(ws.path), "src/module.py", "VALUE = 2\n")
    await world.engine.stage_allowed(ws, SCOPE)
    with pytest.raises(CommitNotVerifiedError):
        await world.engine.commit_verified(ws, "change", early)


async def test_concurrent_commits_cannot_share_one_verification_run(world):
    job_id = await world.job()
    step_id = await world.step(job_id)
    other = await world.engine.registry.register(f"grp/second-{uuid.uuid4().hex[:6]}", world.upstream.url, provider="generic")
    ws_a = await world.engine.create_workspace(job_id, world.repo)
    ws_b = await world.engine.create_workspace(job_id, other)
    for ws in (ws_a, ws_b):
        write(Path(ws.path), "src/module.py", "VALUE = 5\n")
        await world.engine.stage_allowed(ws, SCOPE)
    run = await world.verification(job_id, step_id)
    results = await asyncio.gather(
        world.engine.commit_verified(ws_a, "a", run), world.engine.commit_verified(ws_b, "b", run), return_exceptions=True
    )
    assert sum(1 for r in results if isinstance(r, CommitNotVerifiedError)) == 1
    assert sum(1 for r in results if not isinstance(r, BaseException)) == 1


# ============================================================================================ locks / state
async def test_operation_waiting_for_the_lock_sees_a_concurrent_cleanup(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    write(Path(ws.path), "src/module.py", "VALUE = 2\n")
    async with world.engine._workspace_lock(ws.id):
        task = asyncio.create_task(world.engine.stage_allowed(ws, SCOPE))
        await asyncio.sleep(0.2)  # the task has loaded the (still active) row and now waits for the lock
        assert not task.done()
        async with world.sessionmaker() as s:
            row = await s.get(Workspace, ws.id)
            row.status = "cleaned"
            await s.commit()
    with pytest.raises(WorkspaceStateError) as err:
        await task
    assert err.value.details["status"] == "cleaned"
    assert len(world.engine._locks) == 0  # in-process lock entries are released, not accumulated


async def test_workspace_objects_are_not_hardlinked_to_the_mirror(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    objects = Path(ws.path) / ".git" / "objects"
    files = [p for p in objects.rglob("*") if p.is_file()]
    assert files and all(os.stat(p).st_nlink == 1 for p in files)


async def test_rebasing_a_pushed_workspace_marks_it_committed_again(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
    await world.engine.push_job_branch(ws)
    world.upstream.commit({"docs/guide.md": "upstream\n"})
    await world.engine.update_to_base(ws)
    assert (await world.engine.get_workspace(ws.id)).status == "committed"
    with pytest.raises(WorkspaceStateError):
        await world.engine.cleanup(ws)  # rebased commits are not on the remote yet
    res = await world.engine.push_job_branch(ws)  # our own earlier push is replaceable
    assert res.forced and (await world.engine.get_workspace(ws.id)).status == "pushed"


async def test_scope_violation_event_payload_is_redacted(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    secret_name = "notes-glpat-abcdefghijklmnopqrstuvwx.txt"
    write(Path(ws.path), secret_name, "x\n")
    res = await world.engine.stage_allowed(ws, SCOPE)
    assert secret_name in res.refused_paths
    events = [e for e in await world.events(job_id) if e.event_type == EventType.SCOPE_VIOLATION]
    assert events and "glpat-abcdefghijklmnopqrstuvwx" not in str(events[-1].payload)


# ============================================================================================ merge request op
async def test_public_create_merge_request_after_push(sessionmaker, tmp_path):
    with FakeGitLab() as gl:
        client = GitLabClient(gl.base_url, token=gl.token, backoff_seconds=0.0)
        world = await make_world(
            sessionmaker,
            tmp_path,
            gitlab=client,
            provider="gitlab",
            gitlab_project_id="grp/demo",
            policies=PoliciesConfig(git=GitPolicy(create_merge_request=False)),
        )
        try:
            job_id = await world.job("Add feature")
            ws = await world.engine.create_workspace(job_id, world.repo)
            await _commit(world, ws, job_id, {"src/module.py": "VALUE = 2\n"})
            with pytest.raises(WorkspaceStateError):
                await world.engine.create_merge_request(ws)  # not pushed yet
            res = await world.engine.push_job_branch(ws)
            assert res.merge_request is None and not gl.mrs
            mr = await world.engine.create_merge_request(ws, title="Add feature")
            assert mr.created and mr.source_branch == ws.branch and mr.target_branch == "main"
            again = await world.engine.create_merge_request(ws)
            assert not again.created and again.iid == mr.iid and len(gl.mrs) == 1
            created = [e for e in await world.events(job_id) if e.event_type == EventType.MERGE_REQUEST_CREATED]
            assert len(created) == 1
        finally:
            await client.aclose()


async def test_symlinked_attributes_file_is_refused_and_target_untouched(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    victim = world.root / "victim.txt"
    victim.write_text("keep me\n")
    attrs = Path(ws.path) / ".git" / "info" / "attributes"
    attrs.unlink()
    attrs.symlink_to(victim)
    with pytest.raises(WorkspaceTamperedError) as err:
        await world.engine.status(ws)
    assert any("symlink" in p for p in err.value.details["problems"])
    assert victim.read_text() == "keep me\n"
