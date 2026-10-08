"""Runtime push (6.9), protected-branch refusal (6.10) and optional GitLab merge requests against local services."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import GitPolicy, PoliciesConfig
from hermclaw.core.errors import ProtectedBranchError
from hermclaw.gitops import GitLabClient
from hermclaw.gitops.errors import NothingToPushError, NotJobBranchError, PushRejectedError
from hermclaw.persistence.models import Workspace
from tests.integration.test_gitops_support import FakeGitLab, World, git, make_world, write

pytestmark = pytest.mark.integration

SCOPE = ScopeContract(target_paths=["src/**", "app.py"], allowed_new_paths=["src/**"], allowed_operations=["create", "modify"])


async def _committed_workspace(world: World, title: str = "Push feature", content: str = "VALUE = 2\n"):
    job_id = await world.job(title)
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    write(Path(ws.path), "src/module.py", content)
    await world.engine.stage_allowed(ws, SCOPE)
    commit = await world.engine.commit_verified(ws, "change module", await world.verification(job_id, step_id))
    return job_id, step_id, ws, commit


@pytest.fixture
async def world(sessionmaker, tmp_path):
    return await make_world(sessionmaker, tmp_path)


async def test_push_job_branch_creates_remote_branch_only(world):
    main_before = world.upstream.head()
    job_id, _step, ws, commit = await _committed_workspace(world)
    res = await world.engine.push_job_branch(ws)
    assert res.sha == commit.sha and res.remote_sha_before is None and not res.forced and not res.up_to_date
    assert world.upstream.head(ws.branch) == commit.sha
    assert world.upstream.head() == main_before  # base branch untouched
    branches = git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=world.upstream.bare).splitlines()
    assert sorted(branches) == sorted(["main", ws.branch])
    info = await world.engine.get_workspace(ws.id)
    assert info.status == "pushed" and info.head_sha == commit.sha
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "push" and op.status == "ok" and op.ref == f"refs/heads/{ws.branch}" and op.sha_after == commit.sha
    pushed = [e for e in await world.events(job_id) if e.event_type == EventType.GIT_PUSHED]
    assert len(pushed) == 1 and pushed[0].payload["sha_after"] == commit.sha
    # second push is a no-op but still audited
    again = await world.engine.push_job_branch(ws)
    assert again.up_to_date and again.remote_sha_before == commit.sha
    assert res.merge_request is None and res.merge_request_error is None


async def test_push_after_new_commit_fast_forwards(world):
    job_id, step_id, ws, first = await _committed_workspace(world)
    await world.engine.push_job_branch(ws)
    write(Path(ws.path), "src/module.py", "VALUE = 3\n")
    await world.engine.stage_allowed(ws, SCOPE)
    second = await world.engine.commit_verified(ws, "again", await world.verification(job_id, step_id))
    assert (await world.engine.get_workspace(ws.id)).status == "committed"
    res = await world.engine.push_job_branch(ws)
    assert res.remote_sha_before == first.sha and res.sha == second.sha and not res.forced
    assert world.upstream.head(ws.branch) == second.sha


async def test_push_refuses_nothing_to_push(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    with pytest.raises(NothingToPushError):
        await world.engine.push_job_branch(ws)
    assert world.upstream.head(ws.branch) is None
    assert (await world.git_ops(job_id))[-1].status == "refused"


# ============================================================================================ 6.10 protected branches
@pytest.mark.parametrize("branch", ["main", "master", "release/2.0"])
async def test_push_refuses_protected_branch_locally(world, branch):
    job_id, _step, ws, _commit = await _committed_workspace(world)
    path = Path(ws.path)
    # tamper: point the workspace at a protected branch (e.g. a corrupted row or a hostile tool)
    git("branch", "-q", "-f", branch, "HEAD", cwd=path)
    git("checkout", "-q", branch, cwd=path)
    async with world.sessionmaker() as s:
        row = await s.get(Workspace, ws.id)
        row.branch = branch
        await s.commit()
    main_before = world.upstream.head()
    with pytest.raises(ProtectedBranchError):
        await world.engine.push_job_branch(ws.id)
    assert world.upstream.head() == main_before
    assert world.upstream.head(branch) == (main_before if branch == "main" else None)
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "push" and op.status == "refused" and op.details["error_code"] == "PROTECTED_BRANCH"
    ev = [e for e in await world.events(job_id) if e.payload.get("operation") == "push"][-1]
    assert ev.event_type == EventType.GIT_OPERATION and ev.severity == "warning"


async def test_push_refuses_branch_protected_after_creation(world):
    job_id, _step, ws, _commit = await _committed_workspace(world)
    await world.engine.registry.set_protected_branches(world.repo.id, ["hermclaw/*"])
    with pytest.raises(ProtectedBranchError):
        await world.engine.push_job_branch(ws)
    assert world.upstream.head(ws.branch) is None


async def test_push_refuses_non_job_branch(world):
    job_id, _step, ws, _commit = await _committed_workspace(world)
    path = Path(ws.path)
    git("checkout", "-q", "-b", "feature/manual", cwd=path)
    async with world.sessionmaker() as s:
        row = await s.get(Workspace, ws.id)
        row.branch = "feature/manual"
        await s.commit()
    with pytest.raises(NotJobBranchError):
        await world.engine.push_job_branch(ws.id)
    assert world.upstream.head("feature/manual") is None


async def test_protected_glob_patterns_from_policy(sessionmaker, tmp_path):
    policies = PoliciesConfig(git=GitPolicy(branch_prefix="", protected_branches=["main", "*-stable", "rel/**"]))
    world = await make_world(sessionmaker, tmp_path, policies=policies)
    eng, repo = world.engine, world.repo
    with pytest.raises(ProtectedBranchError):
        eng.assert_pushable(repo, "v1-stable")
    with pytest.raises(ProtectedBranchError):
        eng.assert_pushable(repo, "rel/1/2")
    with pytest.raises(ProtectedBranchError):
        eng.assert_pushable(repo, "main")
    eng.assert_pushable(repo, "feature-x")  # empty prefix: any non-protected branch is a job branch
    with pytest.raises(ProtectedBranchError):
        eng.assert_pushable(repo, "topic", base_branch="topic")


async def test_remote_rejection_is_reported(world):
    job_id, _step, ws, _commit = await _committed_workspace(world)
    git("config", "receive.denyNonFastForwards", "true", cwd=world.upstream.bare)
    await world.engine.push_job_branch(ws)
    # rewrite the job branch locally (as after a rebase) – the remote refuses non-fast-forward updates
    path = Path(ws.path)
    git("commit", "-q", "--amend", "-m", "amended", cwd=path)
    with pytest.raises(PushRejectedError) as err:
        await world.engine.push_job_branch(ws)
    assert err.value.details["branch"] == ws.branch
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "push" and op.status == "refused" and op.details["error_code"] == "PUSH_REJECTED"


async def test_force_with_lease_updates_rebased_job_branch(world):
    job_id, _step, ws, first = await _committed_workspace(world)
    await world.engine.push_job_branch(ws)
    path = Path(ws.path)
    git("-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q", "--amend", "-m", "amended", cwd=path)
    amended = git("rev-parse", "HEAD", cwd=path)
    res = await world.engine.push_job_branch(ws)
    assert res.forced and res.remote_sha_before == first.sha and world.upstream.head(ws.branch) == amended


# ============================================================================================ GitLab merge requests
async def test_push_creates_merge_request_with_duplicate_detection(sessionmaker, tmp_path):
    with FakeGitLab() as gl:
        client = GitLabClient(gl.base_url, token=gl.token, retries=0)
        world = await make_world(sessionmaker, tmp_path, gitlab=client, provider="gitlab", gitlab_project_id="grp/demo")
        try:
            job_id, step_id, ws, commit = await _committed_workspace(world, title="Add MR")
            res = await world.engine.push_job_branch(ws, create_merge_request=True)
            assert res.merge_request is not None and res.merge_request.created and res.merge_request.iid == 1
            assert res.merge_request.source_branch == ws.branch and res.merge_request.target_branch == "main"
            assert gl.mrs[0]["title"] == "Hermclaw: Add MR" and commit.sha in gl.mrs[0]["description"]
            assert all(h.get("PRIVATE-TOKEN") == gl.token for _m, _p, h in gl.requests)
            # second push with a new commit reuses the open MR
            write(Path(ws.path), "src/module.py", "VALUE = 99\n")
            await world.engine.stage_allowed(ws, SCOPE)
            await world.engine.commit_verified(ws, "more", await world.verification(job_id, step_id))
            res2 = await world.engine.push_job_branch(ws, create_merge_request=True)
            assert res2.merge_request is not None and not res2.merge_request.created and res2.merge_request.iid == 1
            assert len(gl.mrs) == 1
            ops_ = [o.operation for o in await world.git_ops(job_id)]
            assert "merge_request.create" in ops_ and "merge_request.reuse" in ops_
            created = [e for e in await world.events(job_id) if e.event_type == EventType.MERGE_REQUEST_CREATED]
            assert len(created) == 1 and created[0].payload["details"]["iid"] == 1
            # the token never ends up in events or audit rows
            for e in await world.events(job_id):
                assert gl.token not in str(e.payload)
        finally:
            await client.aclose()


async def test_merge_request_failure_does_not_fail_push(sessionmaker, tmp_path):
    with FakeGitLab() as gl:
        gl.fail_status, gl.fail_count = 503, 100
        client = GitLabClient(gl.base_url, token=gl.token, retries=1, backoff_seconds=0.01)
        world = await make_world(sessionmaker, tmp_path, gitlab=client, provider="gitlab", gitlab_project_id="grp/demo")
        try:
            job_id, _step, ws, commit = await _committed_workspace(world)
            res = await world.engine.push_job_branch(ws, create_merge_request=True)
            assert world.upstream.head(ws.branch) == commit.sha
            assert res.merge_request is None and res.merge_request_error and "503" in res.merge_request_error
            op = (await world.git_ops(job_id))[-1]
            assert op.operation == "merge_request.create" and op.status == "failed" and op.details["error_code"] == "GITLAB_ERROR"
        finally:
            await client.aclose()


async def test_merge_request_skipped_for_generic_provider(world):
    _job, _step, ws, _commit = await _committed_workspace(world)
    res = await world.engine.push_job_branch(ws, create_merge_request=True)
    assert res.merge_request is None and res.merge_request_error is not None


async def test_gitlab_client_protected_branch_sync(sessionmaker, tmp_path):
    with FakeGitLab() as gl:
        gl.protected = ["main", "prod/*"]
        client = GitLabClient(gl.base_url, token=gl.token, retries=0)
        world = await make_world(sessionmaker, tmp_path, gitlab=client, provider="gitlab", gitlab_project_id="grp/demo")
        try:
            synced = await world.engine.registry.sync_protected_branches(world.repo.id, client)
            assert "prod/*" in synced.protected_branches
            assert world.engine.registry.is_protected(synced, "prod/eu")
            bad = GitLabClient(gl.base_url, token="glpat-wrong-token-000000000000", retries=0)
            from hermclaw.gitops.errors import GitLabError

            with pytest.raises(GitLabError) as err:
                await bad.list_protected_branches("grp/demo")
            assert err.value.details["status"] == 401
            await bad.aclose()
        finally:
            await client.aclose()
