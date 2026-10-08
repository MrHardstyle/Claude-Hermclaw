"""GitEngine against real git + PostgreSQL: registry, mirror, base SHA, workspaces, status/diff, staging, commit, cleanup."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import GitPolicy, PoliciesConfig
from hermclaw.core.errors import ConflictError, NotFoundError, ProtectedBranchError, ScopeViolation, ValidationFailed
from hermclaw.core.interfaces import GitReader
from hermclaw.gitops.errors import (
    BaseBranchNotFound,
    CommitNotVerifiedError,
    InvalidRemoteUrl,
    NothingToCommitError,
    WorkspaceNotFound,
    WorkspaceStateError,
)
from hermclaw.persistence.models import Workspace
from tests.integration.test_gitops_support import git, make_world, write

pytestmark = pytest.mark.integration

SCOPE = ScopeContract(
    target_paths=["app.py", "src/**", "README.md"],
    allowed_new_paths=["src/**", "tests/**"],
    forbidden_paths=["config/**"],
    allowed_operations=["create", "modify"],
)


@pytest.fixture
async def world(sessionmaker, tmp_path):
    return await make_world(sessionmaker, tmp_path)


# ============================================================================================ 6.1 registry
async def test_registry_register_get_list_and_protection(world):
    reg = world.engine.registry
    assert (await reg.get(world.repo.id)).name == world.repo.name
    assert (await reg.get_by_name(world.repo.name)).id == world.repo.id
    assert world.repo.id in [r.id for r in await reg.list_repositories()]
    # idempotent for the same URL, conflict for a different one, update allowed explicitly
    again = await reg.register(world.repo.name, world.upstream.url)
    assert again.id == world.repo.id
    with pytest.raises(ConflictError):
        await reg.register(world.repo.name, "file:///elsewhere/repo.git")
    patterns = reg.protected_patterns(world.repo)
    assert {"main", "master", "release/*"} <= set(patterns)
    assert reg.is_protected(world.repo, "release/1.0")
    assert reg.is_protected(world.repo, "refs/heads/main")
    assert not reg.is_protected(world.repo, "hermclaw/abc-fix")
    updated = await reg.set_protected_branches(world.repo.id, ["stable/*", "refs/heads/prod"])
    assert updated.protected_branches == ["stable/*", "prod"]
    assert reg.is_protected(updated, "stable/2") and reg.is_protected(updated, "prod")
    with pytest.raises(NotFoundError):
        await reg.get(uuid.uuid4())
    with pytest.raises(NotFoundError):
        await reg.get_by_name("does/not-exist")


@pytest.mark.parametrize(
    "url",
    ["ext::sh -c touch% /tmp/pwned", "-uhelp", "https://user:secret@gitlab.example/x.git", "ftp://host/x.git", "relative/path", ""],
)
async def test_registry_rejects_unsafe_urls(world, url):
    with pytest.raises((InvalidRemoteUrl, ValidationFailed)):
        await world.engine.registry.register(f"grp/bad-{uuid.uuid4().hex[:6]}", url)


async def test_registry_rejects_bad_names(world):
    for name in ["../escape", "a/../b", "x.git", "", " spaced name"]:
        with pytest.raises(ValidationFailed):
            await world.engine.registry.register(name, world.upstream.url)


async def test_registry_register_emits_event(world, sessionmaker):
    from hermclaw.persistence.models import Event

    async with sessionmaker() as s:
        rows = list(
            (
                await s.execute(
                    select(Event).where(Event.event_type == EventType.GIT_OPERATION, Event.payload["repository_id"].astext == str(world.repo.id))
                )
            ).scalars()
        )
    assert any(r.payload["operation"] == "repository.register" for r in rows)


# ============================================================================================ 6.2 / 6.3 mirror + base SHA
async def test_mirror_clone_then_fetch_and_resolve_base_sha(world):
    eng = world.engine
    mirror = await eng.sync_mirror(world.repo)
    assert (mirror / "HEAD").exists() and git("rev-parse", "--is-bare-repository", cwd=mirror) == "true"
    assert await eng.resolve_base_sha(world.repo) == world.upstream.head()
    new_sha = world.upstream.commit({"README.md": "# v2\n"})
    # without fetch the cached SHA is returned, with fetch the new one
    assert await eng.resolve_base_sha(world.repo, fetch=False) != new_sha
    assert await eng.resolve_base_sha(world.repo.id) == new_sha
    world.upstream.commit({"x.txt": "x\n"}, branch="main")
    git("push", "-q", "origin", "main:feature/other", cwd=world.upstream.seed)
    assert await eng.resolve_base_sha(world.repo.name, "feature/other") == world.upstream.head("feature/other")
    with pytest.raises(BaseBranchNotFound):
        await eng.resolve_base_sha(world.repo, "no-such-branch")
    ops_ = [o for o in await world.git_ops() if o.details.get("repository_id") == str(world.repo.id)]
    kinds = [o.operation for o in ops_]
    assert kinds[0] == "mirror.clone" and "mirror.fetch" in kinds
    assert all(o.status == "ok" for o in ops_)
    # mirror only carries heads and tags
    refs = git("for-each-ref", "--format=%(refname)", cwd=mirror).splitlines()
    assert all(r.startswith(("refs/heads/", "refs/tags/")) for r in refs)


async def test_mirror_fetch_prunes_deleted_branches(world):
    eng = world.engine
    git("push", "-q", "origin", "main:tmp/branch", cwd=world.upstream.seed)
    mirror = await eng.sync_mirror(world.repo)
    assert "refs/heads/tmp/branch" in git("for-each-ref", cwd=mirror)
    git("push", "-q", "origin", ":tmp/branch", cwd=world.upstream.seed)
    await eng.sync_mirror(world.repo)
    assert "refs/heads/tmp/branch" not in git("for-each-ref", cwd=mirror)


async def test_unreachable_remote_is_recorded_as_failed(sessionmaker, tmp_path):
    world = await make_world(sessionmaker, tmp_path)
    reg = world.engine.registry
    broken = await reg.register(f"grp/broken-{uuid.uuid4().hex[:6]}", f"file://{tmp_path}/missing.git")
    from hermclaw.gitops.errors import GitCommandError

    with pytest.raises(GitCommandError):
        await world.engine.sync_mirror(broken)
    failed = [o for o in await world.git_ops() if o.details.get("repository_id") == str(broken.id)]
    assert failed and failed[-1].status == "failed" and failed[-1].operation == "mirror.clone"
    assert not world.engine.mirror_path(broken).exists()  # no half-initialised mirror left behind


# ============================================================================================ 6.4 / 6.5 workspaces + branches
async def test_create_workspace_isolated_clone_on_job_branch(world):
    job_id = await world.job("Fix: Login Bug!")
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    assert path == world.settings.workspaces_dir / str(job_id) / path.name
    assert ws.branch == f"hermclaw/{job_id.hex[:8]}-fix-login-bug"
    assert ws.base_branch == "main" and ws.base_sha == world.upstream.head() == ws.head_sha
    assert ws.status == "active"
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == ws.branch
    assert git("rev-parse", "HEAD", cwd=path) == ws.base_sha
    assert (path / "app.py").read_text() == "def add(a, b):\n    return a + b\n"
    # full clone, not a worktree/shared object store of the mirror
    assert (path / ".git").is_dir() and not (path / ".git" / "objects" / "info" / "alternates").exists()
    # origin points to the real upstream, push.default is disabled, local base branch removed
    assert git("remote", "get-url", "origin", cwd=path) == world.upstream.url
    assert git("config", "push.default", cwd=path) == "nothing"
    assert git("branch", "--list", "main", cwd=path) == ""
    attrs = (path / ".git" / "info" / "attributes").read_text()
    assert "/**/.env -diff" in attrs or "**/.env -diff" in attrs
    # DB row + audit row + event
    async with world.sessionmaker() as s:
        row = await s.get(Workspace, ws.id)
        assert row is not None and row.path == ws.path and row.base_sha == ws.base_sha
    ops_ = await world.git_ops(job_id)
    assert [o.operation for o in ops_ if o.operation.startswith("workspace")] == ["workspace.create"]
    assert any(e.event_type == EventType.GIT_OPERATION and e.payload["operation"] == "workspace.create" for e in await world.events(job_id))
    handle = ws.to_handle()
    assert handle.path == path and handle.repository_key == world.repo.name


async def test_create_workspace_is_idempotent_and_isolated_per_job(world):
    job_a, job_b = await world.job("A"), await world.job("B")
    ws_a = await world.engine.create_workspace(job_a, world.repo)
    again = await world.engine.create_workspace(job_a, world.repo)
    assert again.id == ws_a.id
    ws_b = await world.engine.create_workspace(job_b, world.repo, slug="custom slug")
    assert ws_b.path != ws_a.path and ws_b.branch.endswith("-custom-slug")
    write(Path(ws_a.path), "src/module.py", "VALUE = 2\n")
    assert (Path(ws_b.path) / "src/module.py").read_text() == "VALUE = 1\n"


async def test_create_workspace_recreates_missing_directory(world):
    import shutil

    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    shutil.rmtree(ws.path)
    ws2 = await world.engine.create_workspace(job_id, world.repo)
    assert ws2.id != ws.id and Path(ws2.path).is_dir()
    assert (await world.engine.get_workspace(ws.id)).status == "archived"


async def test_create_workspace_unknown_job_and_base_branch(world):
    with pytest.raises(NotFoundError):
        await world.engine.create_workspace(uuid.uuid4(), world.repo)
    job_id = await world.job()
    with pytest.raises(BaseBranchNotFound):
        await world.engine.create_workspace(job_id, world.repo, base_branch="nope")
    assert not (world.settings.workspaces_dir / str(job_id)).exists()  # no temp clone / empty job dir left behind
    ops_ = await world.git_ops(job_id)
    assert ops_[-1].operation == "workspace.create" and ops_[-1].status == "failed"
    assert ops_[-1].details["error_code"] == "BASE_BRANCH_NOT_FOUND"


async def test_create_workspace_refuses_protected_job_branch(sessionmaker, tmp_path):
    world = await make_world(sessionmaker, tmp_path, protected_branches=["hermclaw/*"])
    job_id = await world.job()
    with pytest.raises(ProtectedBranchError):
        await world.engine.create_workspace(job_id, world.repo)
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "workspace.create" and op.status == "refused" and op.details["error_code"] == "PROTECTED_BRANCH"


async def test_create_workspace_resumes_existing_remote_job_branch(world):
    job_id = await world.job("resume me")
    branch = world.engine.job_branch(job_id, "resume me")
    git("checkout", "-q", "-b", branch, cwd=world.upstream.seed)
    write(world.upstream.seed, "src/module.py", "VALUE = 42\n")
    git("commit", "-q", "-am", "earlier attempt", cwd=world.upstream.seed)
    git("push", "-q", "origin", branch, cwd=world.upstream.seed)
    tip = git("rev-parse", "HEAD", cwd=world.upstream.seed)
    git("checkout", "-q", "main", cwd=world.upstream.seed)
    ws = await world.engine.create_workspace(job_id, world.repo)
    assert ws.head_sha == tip and ws.base_sha == world.upstream.head()
    assert (Path(ws.path) / "src/module.py").read_text() == "VALUE = 42\n"


async def test_workspace_path_tampering_is_rejected(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    async with world.sessionmaker() as s:
        row = await s.get(Workspace, ws.id)
        row.path = "/etc"
        await s.commit()
    from hermclaw.gitops.errors import WorkspacePathViolation

    with pytest.raises(WorkspacePathViolation):
        await world.engine.status(ws.id)
    with pytest.raises(WorkspacePathViolation):
        await world.engine.cleanup(ws.id, force=True)
    with pytest.raises(WorkspaceNotFound):
        await world.engine.status(uuid.uuid4())


# ============================================================================================ 6.6 status / diff
async def test_status_and_diff_include_untracked_and_limits(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    write(path, "app.py", "def add(a, b):\n    return a + b + 0\n")
    write(path, "src/new_file.py", "NEW = True\n")
    write(path, "debug.log", "ignored\n")  # .gitignore'd
    (path / "docs/guide.md").unlink()
    write(path, "bin.dat", "")
    (path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    st = await world.engine.status(ws)
    codes = {e.path: e.index + e.worktree for e in st.entries}
    assert codes == {"app.py": " M", "src/new_file.py": "??", "docs/guide.md": " D", "bin.dat": "??"}
    assert st.branch == ws.branch and st.head_sha == ws.base_sha and st.commits_ahead_of_base == 0
    assert set(st.untracked) == {"src/new_file.py", "bin.dat"}
    diff = await world.engine.diff(ws)
    by_path = {f.path: f for f in diff.files}
    assert by_path["app.py"].status == "M" and by_path["app.py"].additions == 1 and by_path["app.py"].deletions == 1
    assert by_path["src/new_file.py"].status == "A" and by_path["src/new_file.py"].additions == 1
    assert by_path["docs/guide.md"].status == "D" and by_path["docs/guide.md"].deletions == 2
    assert by_path["bin.dat"].binary
    assert "debug.log" not in by_path
    assert "+NEW = True" in diff.patch and "return a + b + 0" in diff.patch
    assert not diff.patch_truncated
    # the real index was not touched by the intent-to-add trick
    assert git("diff", "--cached", "--name-only", cwd=path) == ""
    assert git("status", "--porcelain", "--untracked-files=all", "--", "src/new_file.py", cwd=path) == "?? src/new_file.py"
    small = await world.engine.diff(ws, max_patch_bytes=40)
    assert small.patch_truncated and len(small.patch.encode()) <= 40
    only = await world.engine.diff(ws, paths=["app.py"])
    assert [f.path for f in only.files] == ["app.py"]
    capped = await world.engine.diff(ws, max_files=2, include_patch=False)
    assert capped.files_truncated and len(capped.files) == 2 and capped.patch == ""
    assert await world.engine.changed_files(ws) == sorted(["app.py", "bin.dat", "docs/guide.md", "src/new_file.py"])
    with pytest.raises(ValidationFailed):
        await world.engine.diff(ws, paths=["../outside"])


async def test_diff_hides_forbidden_file_content_and_redacts(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    write(path, ".env", "DB_PASSWORD=supersecretvalue\n")
    write(path, "src/settings.py", 'API_KEY = "abcd1234efgh5678"\n')
    diff = await world.engine.diff(ws)
    assert ".env" in [f.path for f in diff.files]
    assert "supersecretvalue" not in diff.patch
    assert "abcd1234efgh5678" not in diff.patch and "***REDACTED***" in diff.patch


async def test_diff_against_base_includes_commits_after_rename(world):
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    git("mv", "src/util.py", "src/helpers.py", cwd=path)
    scope = ScopeContract(target_paths=["src/**"], allowed_new_paths=["src/**"], allowed_operations=["create", "modify", "delete"])
    res = await world.engine.stage_allowed(ws, scope)
    assert sorted(res.staged_paths) == ["src/helpers.py", "src/util.py"]
    vr = await world.verification(job_id, step_id)
    await world.engine.commit_verified(ws, "rename util", vr)
    diff = await world.engine.diff(ws)
    assert [(f.status, f.old_path, f.path) for f in diff.files] == [("R", "src/util.py", "src/helpers.py")]
    assert await world.engine.changed_files(ws) == ["src/helpers.py", "src/util.py"]


async def test_reader_implements_git_reader_protocol(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    write(Path(ws.path), "src/module.py", "VALUE = 3\n")
    reader = world.engine.reader()
    assert isinstance(reader, GitReader)
    handle = ws.to_handle()
    assert [(e.path, e.status) for e in await reader.status(handle)] == [("src/module.py", " M")]
    text = await reader.diff(handle, ["src/module.py"])
    assert "+VALUE = 3" in text
    assert "truncated" in await reader.diff(handle, max_bytes=10)
    assert await reader.changed_files(handle) == ["src/module.py"]
    assert await reader.show_base_file(handle, "src/module.py") == "VALUE = 1\n"
    assert await reader.show_base_file(handle, "missing.txt") is None
    from hermclaw.core.errors import PolicyViolation

    with pytest.raises(PolicyViolation):
        await reader.show_base_file(handle, "deploy/.env")
    assert await reader.log(handle) == []
    # read-only: nothing staged, no git_operations written by reads
    assert git("diff", "--cached", "--name-only", cwd=Path(ws.path)) == ""
    assert [o.operation for o in await world.git_ops(job_id)] == ["mirror.clone", "workspace.create"]


# ============================================================================================ 6.7 safe staging
async def test_stage_allowed_only_stages_scope_paths(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    write(path, "app.py", "def add(a, b):\n    return b + a\n")  # target -> staged
    write(path, "src/feature.py", "FEATURE = 1\n")  # allowed new -> staged
    write(path, "docs/guide.md", "changed\n")  # not in scope -> refused
    write(path, "config/settings.toml", "debug = true\n")  # forbidden -> refused
    write(path, "src/.env", "TOKEN=abc\n")  # always forbidden -> refused
    write(path, "notes.txt", "scratch\n")  # new outside allowed_new_paths -> refused
    (path / "README.md").unlink()  # delete not allowed by operations -> refused
    res = await world.engine.stage_allowed(ws, SCOPE)
    assert sorted(res.staged_paths) == ["app.py", "src/feature.py"]
    reasons = {r.path: r.reason for r in res.refused}
    assert reasons == {
        "docs/guide.md": "outside_scope",
        "config/settings.toml": "forbidden",
        "src/.env": "always_forbidden",
        "notes.txt": "outside_scope",
        "README.md": "operation_not_allowed",
    }
    staged = git("diff", "--cached", "--name-only", cwd=path).splitlines()
    assert sorted(staged) == ["app.py", "src/feature.py"]
    # refused changes stay in the working tree, unstaged
    assert (path / "docs/guide.md").read_text() == "changed\n" and (path / "src/.env").exists()
    porcelain = git("status", "--porcelain", "--untracked-files=all", cwd=path).splitlines()
    assert " M docs/guide.md" in porcelain and "?? notes.txt" in porcelain and " D README.md" in porcelain
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "stage" and op.status == "ok" and op.details["staged_count"] == 2 and op.details["refused_count"] == 5
    violations = [e for e in await world.events(job_id) if e.event_type == EventType.SCOPE_VIOLATION]
    assert violations and violations[-1].payload["refused_count"] == 5


async def test_stage_allowed_unstages_previously_staged_out_of_scope_paths(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    write(path, "docs/guide.md", "sneaky\n")
    write(path, "src/module.py", "VALUE = 5\n")
    git("add", "-A", cwd=path)  # somebody staged everything behind the runtime's back
    res = await world.engine.stage_allowed(ws, SCOPE)
    assert res.staged_paths == ["src/module.py"]
    assert git("diff", "--cached", "--name-only", cwd=path).splitlines() == ["src/module.py"]


async def test_stage_allowed_deletes_and_strict_vs_non_strict(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    (path / "src/util.py").unlink()
    write(path, "docs/new.md", "new doc\n")
    strict = ScopeContract(target_paths=["src/util.py"], allowed_new_paths=[], allowed_operations=["create", "modify", "delete"])
    res = await world.engine.stage_allowed(ws, strict)
    assert res.staged_paths == ["src/util.py"] and res.staged[0].operation == "delete"
    assert {r.path: r.reason for r in res.refused} == {"docs/new.md": "outside_scope"}
    lax = ScopeContract(strict_target_paths=False, target_paths=["src/util.py"], allowed_new_paths=["docs/**"], allowed_operations=["create", "delete"])
    res2 = await world.engine.stage_allowed(ws, lax)
    assert sorted(res2.staged_paths) == ["docs/new.md", "src/util.py"]
    assert git("diff", "--cached", "--name-status", cwd=path).splitlines() == ["A\tdocs/new.md", "D\tsrc/util.py"]


async def test_stage_refuses_escaping_symlink_and_odd_names(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    (path / "src/link_out").symlink_to("/etc/passwd")
    (path / "src/link_in").symlink_to("module.py")
    write(path, "src/with space.py", "x = 1\n")
    write(path, "src/-dash.py", "x = 2\n")
    write(path, 'src/quo"te.py', "x = 3\n")
    write(path, "src/umläut.py", "x = 4\n")
    res = await world.engine.stage_allowed(ws, SCOPE)
    assert {r.path: r.reason for r in res.refused} == {"src/link_out": "unsafe_symlink"}
    assert sorted(res.staged_paths) == sorted(["src/link_in", "src/with space.py", "src/-dash.py", 'src/quo"te.py', "src/umläut.py"])
    staged = git("-c", "core.quotePath=false", "diff", "--cached", "--name-only", cwd=path).splitlines()
    assert "src/link_out" not in staged and len(staged) == 5


async def test_stage_refuses_embedded_repository(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    nested = path / "src" / "vendored"
    nested.mkdir(parents=True)
    git("init", "-q", str(nested), cwd=path)
    write(nested, "x.py", "x\n")
    git("add", "-A", cwd=nested)
    git("commit", "-q", "-m", "n", cwd=nested)
    res = await world.engine.stage_allowed(ws, SCOPE)
    assert [r.reason for r in res.refused] == ["embedded_repository"]
    assert res.staged == []


# ============================================================================================ 6.8 runtime commit
async def test_commit_verified_happy_path(world):
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    write(path, "src/module.py", "VALUE = 2\n")
    write(path, "docs/guide.md", "out of scope\n")
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    vr = await world.verification(job_id, step_id, changed_files=["src/module.py", {"path": "docs/guide.md"}])
    res = await world.engine.commit_verified(ws, "Update module value\n\nbody text", vr, step_id=step_id, scope=SCOPE)
    assert res.files == ["src/module.py"] and res.parent_sha == ws.base_sha
    assert git("rev-parse", "HEAD", cwd=path) == res.sha
    log = git("log", "-1", "--format=%an <%ae>%n%B", cwd=path)
    assert log.startswith("Hermclaw Runtime <hermclaw@localhost>\nUpdate module value\n\nbody text")
    assert f"Hermclaw-Verification: {vr}" in log and f"Hermclaw-Job: {job_id}" in log and f"Hermclaw-Step: {step_id}" in log
    # out-of-scope change still only in the working tree
    assert git("status", "--porcelain", cwd=path) == " M docs/guide.md"
    info = await world.engine.get_workspace(ws.id)
    assert info.status == "committed" and info.head_sha == res.sha
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "commit" and op.status == "ok" and op.sha_after == res.sha and op.details["verification_run_id"] == str(vr)
    evs = [e for e in await world.events(job_id) if e.event_type == EventType.GIT_COMMIT_CREATED]
    assert len(evs) == 1 and evs[0].payload["sha_after"] == res.sha and evs[0].step_id == step_id
    st = await world.engine.status(ws)
    assert st.commits_ahead_of_base == 1


@pytest.mark.parametrize("variant", ["missing", "failed", "running", "other_job", "other_step"])
async def test_commit_refused_without_passed_verification(world, variant):
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    write(Path(ws.path), "src/module.py", "VALUE = 9\n")
    await world.engine.stage_allowed(ws, SCOPE)
    if variant == "missing":
        vr = uuid.uuid4()
    elif variant == "failed":
        vr = await world.verification(job_id, step_id, passed=False)
    elif variant == "running":
        vr = await world.verification(job_id, step_id, passed=False, status="running")
    elif variant == "other_job":
        other = await world.job("other")
        vr = await world.verification(other, await world.step(other))
    else:
        vr = await world.verification(job_id, await world.step(job_id, "S2"))
    with pytest.raises(CommitNotVerifiedError):
        await world.engine.commit_verified(ws, "should not commit", vr, step_id=step_id)
    assert git("rev-parse", "HEAD", cwd=Path(ws.path)) == ws.base_sha
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "commit" and op.status == "refused" and op.details["error_code"] == "COMMIT_NOT_VERIFIED"
    assert (await world.engine.get_workspace(ws.id)).status == "active"


async def test_commit_refuses_reused_verification_and_unverified_paths(world):
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    write(path, "src/module.py", "VALUE = 2\n")
    await world.engine.stage_allowed(ws, SCOPE)
    vr = await world.verification(job_id, step_id)
    await world.engine.commit_verified(ws, "first", vr)
    write(path, "src/module.py", "VALUE = 3\n")
    await world.engine.stage_allowed(ws, SCOPE)
    with pytest.raises(CommitNotVerifiedError, match="already used"):
        await world.engine.commit_verified(ws, "replay", vr)
    narrow = await world.verification(job_id, step_id, changed_files=["app.py"])
    with pytest.raises(CommitNotVerifiedError, match="not covered"):
        await world.engine.commit_verified(ws, "unverified", narrow)


async def test_commit_refuses_nothing_staged_and_forbidden_in_index(world):
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    vr = await world.verification(job_id, step_id)
    with pytest.raises(NothingToCommitError):
        await world.engine.commit_verified(ws, "empty", vr)
    write(path, "keys/server.pem", "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    git("add", "keys/server.pem", cwd=path)  # staged outside the engine
    with pytest.raises(ScopeViolation):
        await world.engine.commit_verified(ws, "leak key", vr)
    write(path, "docs/guide.md", "x\n")
    git("reset", "-q", cwd=path)
    git("add", "docs/guide.md", cwd=path)
    with pytest.raises(ScopeViolation):
        await world.engine.commit_verified(ws, "outside", vr, scope=SCOPE)
    with pytest.raises(ValidationFailed):
        await world.engine.commit_verified(ws, "   \n  ", vr)
    assert git("rev-parse", "HEAD", cwd=path) == ws.base_sha


async def test_commit_refused_when_not_on_job_branch(world):
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    git("checkout", "-q", "-b", "other", cwd=path)
    write(path, "src/module.py", "VALUE = 2\n")
    with pytest.raises(WorkspaceStateError):
        await world.engine.stage_allowed(ws, SCOPE)
    git("add", "src/module.py", cwd=path)
    with pytest.raises(WorkspaceStateError):
        await world.engine.commit_verified(ws, "wrong branch", await world.verification(job_id, step_id))


# ============================================================================================ cleanup
async def test_cleanup_removes_directory_and_marks_row(world):
    job_id = await world.job()
    ws = await world.engine.create_workspace(job_id, world.repo)
    write(Path(ws.path), "src/module.py", "dirty\n")
    cleaned = await world.engine.cleanup(ws)
    assert cleaned.status == "cleaned" and not Path(ws.path).exists()
    assert not (world.settings.workspaces_dir / str(job_id)).exists()
    assert (await world.engine.cleanup(ws.id)).status == "cleaned"  # idempotent
    with pytest.raises(WorkspaceStateError):
        await world.engine.status(ws.id)
    op = [o for o in await world.git_ops(job_id) if o.operation == "workspace.cleanup"]
    assert len(op) == 1 and op[0].status == "ok" and op[0].details["uncommitted_changes"] == 1


async def test_cleanup_refuses_unpushed_commits_without_force(world):
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    write(Path(ws.path), "src/module.py", "VALUE = 2\n")
    await world.engine.stage_allowed(ws, SCOPE)
    await world.engine.commit_verified(ws, "c", await world.verification(job_id, step_id))
    with pytest.raises(WorkspaceStateError):
        await world.engine.cleanup(ws)
    assert Path(ws.path).exists()
    done = await world.engine.cleanup_job(job_id, force=True)
    assert [w.status for w in done] == ["cleaned"] and not Path(ws.path).exists()


async def test_engine_from_config_uses_policies(sessionmaker, tmp_path):
    from hermclaw.core.config import load_config
    from hermclaw.core.settings import Settings
    from hermclaw.gitops import GitEngine

    cfg = load_config()
    eng = GitEngine.from_config(cfg, Settings(data_dir=tmp_path), sessionmaker, gitlab=None)
    env = eng.runner.environment()
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["LC_ALL"] == "C"
    assert "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]
    assert eng.runner.author_name == cfg.policies.git.author_name
    custom = PoliciesConfig(git=GitPolicy(branch_prefix="bot/", author_name="Bot", author_email="bot@x"))
    world = await make_world(sessionmaker, tmp_path / "w", policies=custom)
    job_id = await world.job("prefix test")
    ws = await world.engine.create_workspace(job_id, world.repo)
    assert ws.branch.startswith("bot/")
