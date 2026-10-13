"""Exact verification binding: ``verification_runs.workspace_id`` + ``content_digest`` -> ``GitEngine.commit_verified``.

Real: PostgreSQL, git (file:// remote), GitEngine workspaces, the deterministic Verifier with the GitEngine reader and a
shell executor. The digest is recomputed independently here (``git hash-object`` per file, sha256 over the sorted
``<path>\\0<blob sha | deleted>`` lines) so the test pins the specified format, not just "both sides agree".
"""

from __future__ import annotations

import hashlib
import os
import shlex
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from hermclaw.contracts.acceptance import AbsenceEvidence, PresenceEvidence
from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.step import StepContract
from hermclaw.core.config import PoliciesConfig, get_config
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.gitops.engine import content_digest, index_blobs, worktree_content_digest
from hermclaw.gitops.errors import CommitNotVerifiedError
from hermclaw.gitops.reader import WorkspaceGitReader
from hermclaw.gitops.runner import GitRunner
from hermclaw.gitops.types import WorkspaceInfo
from hermclaw.persistence.models import Event, VerificationRun
from hermclaw.runtime.adapters import VerifierAdapter
from hermclaw.verifier import VerificationStep, Verifier
from tests.integration.test_gitops_support import World, git, make_world, write
from tests.integration.test_verifier_support import GitCliReader, WorkspaceShellExecutor, handle, init_repo, make_job_step

pytestmark = pytest.mark.integration

SCOPE = ScopeContract(
    target_paths=["app.py", "src/**"],
    allowed_new_paths=["src/**"],
    allowed_operations=["create", "modify", "delete"],
)
POLICIES = PoliciesConfig.model_validate({"verifier": {"allow_deletions": True}})
MODULE_IS_2 = PresenceEvidence(path_glob="src/module.py", pattern=r"^VALUE = 2\b")
# The verified edits change the file size: these tests are about the binding, not about git's racy-clean detection of
# same-size edits in the reader's scratch index (BUG-059, covered by the gitops tests).
MODULE_V2 = "VALUE = 2  # verified\n"
APP_SWAPPED = "def add(a, b):\n    return b + a  # swapped\n"


@pytest.fixture
async def world(sessionmaker: Any, tmp_path: Path) -> World:
    return await make_world(sessionmaker, tmp_path, policies=POLICIES)


def hash_text(root: Path, text: str) -> str:
    res = subprocess.run(["git", "hash-object", "--stdin"], cwd=root, input=text.encode(), capture_output=True, check=True)
    return res.stdout.decode().strip()


def expected_digest(root: Path, files: list[str]) -> str:
    """Independent recomputation: blob sha of a regular file, ``<sha> <mode>`` for an executable/symlink."""
    lines = []
    for rel in files:
        full = root / rel
        if not full.is_symlink() and not full.exists():
            token = "deleted"
        elif full.is_symlink():
            token = f"{hash_text(root, os.readlink(full))} 120000"
        else:
            sha_text = git("hash-object", "--", rel, cwd=root)
            token = f"{sha_text} 100755" if full.stat().st_mode & stat.S_IXUSR else sha_text
        lines.append(f"{rel}\0{token}")
    return hashlib.sha256("".join(f"{line}\n" for line in sorted(lines)).encode()).hexdigest()


async def verify(world: World, ws: WorkspaceInfo, job_id: uuid.UUID, step_id: uuid.UUID, *acceptance: Any) -> uuid.UUID:
    verifier = Verifier(world.sessionmaker, POLICIES, WorkspaceShellExecutor(), world.engine.reader())
    vstep = VerificationStep.build(key="S1", kind="implement", acceptance=list(acceptance), scope=SCOPE)
    out = await verifier.run(vstep, await world.engine.handle(ws), job_id=job_id, step_id=step_id, attempt_id=None)
    assert out.report.passed, [(c.name, c.status, c.message) for c in out.report.checks if c.status in ("fail", "error")]
    return out.run_id


async def run_row(world: World, run_id: uuid.UUID) -> VerificationRun:
    async with world.sessionmaker() as s:
        row: VerificationRun | None = await s.get(VerificationRun, run_id)
    assert row is not None
    return row


async def bound_run(world: World, job_id: uuid.UUID, step_id: uuid.UUID, **kw: Any) -> uuid.UUID:
    async with world.sessionmaker() as s:
        vr = VerificationRun(job_id=job_id, step_id=step_id, passed=True, status="passed", **kw)
        s.add(vr)
        await s.commit()
        return vr.id


async def setup(world: World) -> tuple[uuid.UUID, uuid.UUID, WorkspaceInfo, Path]:
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    return job_id, step_id, ws, Path(ws.path)


# ============================================================================================ recorded binding
async def test_run_records_workspace_and_exact_digest_and_unchanged_content_commits(world: World) -> None:
    job_id, step_id, ws, path = await setup(world)
    write(path, "src/module.py", MODULE_V2)
    write(path, "src/new.py", "NEW = True\n")
    (path / "src/util.py").unlink()  # a verified deletion is part of the digest ('deleted')
    run_id = await verify(world, ws, job_id, step_id, MODULE_IS_2, AbsenceEvidence(path_glob="src/util.py"))
    row = await run_row(world, run_id)
    files = ["src/module.py", "src/new.py", "src/util.py"]
    assert row.workspace_id == ws.id and row.changed_files == files
    assert row.content_digest == expected_digest(path, files)
    async with world.sessionmaker() as s:
        finished = (
            await s.execute(
                select(Event).where(
                    Event.event_type == EventType.VERIFIER_FINISHED, Event.payload["verification_run_id"].astext == str(run_id)
                )
            )
        ).scalar_one()
    assert finished.payload["content_digest"] == row.content_digest

    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    assert content_digest(await index_blobs(world.engine.runner, path, files)) == row.content_digest
    res = await world.engine.commit_verified(ws, "verified change", run_id, step_id=step_id, scope=SCOPE)
    assert sorted(res.files) == files
    assert git("show", f"{res.sha}:src/module.py", cwd=path) == MODULE_V2.rstrip("\n")
    assert git("ls-tree", "--name-only", res.sha, "src/util.py", cwd=path) == ""  # the deletion was committed
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "commit" and op.status == "ok" and op.details["content_digest"] == row.content_digest


async def test_verified_deletion_only_commits(world: World) -> None:
    job_id, step_id, ws, path = await setup(world)
    (path / "src/util.py").unlink()
    run_id = await verify(world, ws, job_id, step_id, AbsenceEvidence(path_glob="src/util.py"))
    row = await run_row(world, run_id)
    assert row.changed_files == ["src/util.py"]
    assert row.content_digest == hashlib.sha256(b"src/util.py\0deleted\n").hexdigest()
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    res = await world.engine.commit_verified(ws, "drop util", run_id, step_id=step_id, scope=SCOPE)
    assert res.files == ["src/util.py"]


# ============================================================================================ stale content
def _edit_module(path: Path) -> None:
    write(path, "src/module.py", "VALUE = 3  # edited after verification\n")


def _delete_module(path: Path) -> None:
    (path / "src/module.py").unlink()


def _revert_app(path: Path) -> None:
    git("checkout", "--", "app.py", cwd=path)  # one verified change restored: only a part would be committed


def _recreate_util(path: Path) -> None:
    write(path, "src/util.py", "def helper():\n    return 'back'\n")  # verified as deleted


def _symlink_swap(path: Path) -> None:
    # same blob (the link text equals the verified file content), but the commit would record a symlink (120000)
    (path / "src/alias.txt").unlink()
    os.symlink("module.py", path / "src/alias.txt")


def _make_executable(path: Path) -> None:
    os.chmod(path / "src/module.py", 0o755)  # same blob, the commit would record 100755


@pytest.mark.parametrize(
    "after",
    [_edit_module, _delete_module, _revert_app, _recreate_util, _symlink_swap, _make_executable],
    ids=["edited", "deleted", "reverted", "recreated", "symlink-swap", "chmod-x"],
)
async def test_content_changed_after_verification_is_never_committed(world: World, after: Any) -> None:
    """Regression: the staged content (blob *and* git mode) must equal the verified content of every verified path."""
    job_id, step_id, ws, path = await setup(world)
    write(path, "src/module.py", MODULE_V2)
    write(path, "app.py", APP_SWAPPED)
    write(path, "src/alias.txt", "module.py")
    (path / "src/util.py").unlink()
    run_id = await verify(world, ws, job_id, step_id)
    verified_digest = (await run_row(world, run_id)).content_digest
    assert verified_digest is not None
    after(path)
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    with pytest.raises(CommitNotVerifiedError) as ei:
        await world.engine.commit_verified(ws, "stale", run_id, step_id=step_id, scope=SCOPE)
    assert ei.value.code == "VERIFICATION_STALE", ei.value.details
    assert ei.value.details["verified_digest"] == verified_digest and ei.value.details["staged_digest"] != verified_digest
    assert ei.value.details["worktree_matches_verification"] is False
    assert git("rev-parse", "HEAD", cwd=path) == ws.base_sha
    op = (await world.git_ops(job_id))[-1]
    assert op.operation == "commit" and op.status == "refused" and op.details["error_code"] == "VERIFICATION_STALE"
    assert (await world.engine.get_workspace(ws.id)).status == "active"
    # verifying the new content again binds a new run to it
    rerun = await verify(world, ws, job_id, step_id)
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    res = await world.engine.commit_verified(ws, "re-verified", rerun, step_id=step_id, scope=SCOPE)
    assert git("rev-parse", "HEAD", cwd=path) == res.sha


async def test_verified_executable_and_symlink_commit_with_their_modes(world: World) -> None:
    job_id, step_id, ws, path = await setup(world)
    write(path, "src/run.sh", "#!/bin/sh\necho ok\n")
    os.chmod(path / "src/run.sh", 0o755)
    os.symlink("module.py", path / "src/alias.py")
    run_id = await verify(world, ws, job_id, step_id)
    row = await run_row(world, run_id)
    files = ["src/alias.py", "src/run.sh"]
    assert row.changed_files == files and row.content_digest == expected_digest(path, files)
    blobs_only = {"src/alias.py": hash_text(path, "module.py"), "src/run.sh": git("hash-object", "--", "src/run.sh", cwd=path)}
    assert row.content_digest != content_digest(blobs_only)  # the modes are part of the binding
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    res = await world.engine.commit_verified(ws, "modes", run_id, step_id=step_id, scope=SCOPE)
    modes = {line.split("\t")[1]: line.split(" ")[0] for line in git("ls-tree", res.sha, "src/", cwd=path).splitlines()}
    assert modes["src/run.sh"] == "100755" and modes["src/alias.py"] == "120000"


async def test_binding_is_on_the_index_not_on_later_unstaged_edits(world: World) -> None:
    job_id, step_id, ws, path = await setup(world)
    write(path, "src/module.py", MODULE_V2)
    run_id = await verify(world, ws, job_id, step_id, MODULE_IS_2)
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    write(path, "src/module.py", "VALUE = 99\n")  # after staging: the commit still records the verified content
    res = await world.engine.commit_verified(ws, "verified", run_id, step_id=step_id, scope=SCOPE)
    assert git("show", f"{res.sha}:src/module.py", cwd=path) == MODULE_V2.rstrip("\n")
    assert git("status", "--porcelain", cwd=path) == " M src/module.py"


async def test_restaging_after_unstaged_edit_is_refused(world: World) -> None:
    job_id, step_id, ws, path = await setup(world)
    write(path, "src/module.py", MODULE_V2)
    run_id = await verify(world, ws, job_id, step_id, MODULE_IS_2)
    write(path, "src/module.py", MODULE_V2 + "EXTRA = 1\n")  # still satisfies the acceptance, but was never verified
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    with pytest.raises(CommitNotVerifiedError, match="differs from the content the verification run checked"):
        await world.engine.commit_verified(ws, "stale", run_id, step_id=step_id, scope=SCOPE)


# ============================================================================================ run/workspace rules
async def test_digest_run_without_changed_files_covers_nothing(world: World) -> None:
    job_id, step_id, ws, path = await setup(world)
    run_id = await bound_run(world, job_id, step_id, workspace_id=ws.id, changed_files=[], content_digest=content_digest({}))
    write(path, "src/module.py", MODULE_V2)
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    with pytest.raises(CommitNotVerifiedError, match="not covered"):
        await world.engine.commit_verified(ws, "unverified", run_id, step_id=step_id)


async def test_run_of_another_workspace_is_refused(world: World) -> None:
    job_id, step_id, ws, path = await setup(world)
    other_job = await world.job("other job")
    other_ws = await world.engine.create_workspace(other_job, world.repo)
    write(path, "src/module.py", MODULE_V2)
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    digest = await worktree_content_digest(path, ["src/module.py"], runner=world.engine.runner)
    run_id = await bound_run(world, job_id, step_id, workspace_id=other_ws.id, changed_files=["src/module.py"], content_digest=digest)
    with pytest.raises(CommitNotVerifiedError, match="different workspace") as ei:
        await world.engine.commit_verified(ws, "foreign", run_id, step_id=step_id)
    assert ei.value.code == "COMMIT_NOT_VERIFIED"


async def test_bound_run_without_digest_is_refused_but_legacy_run_keeps_old_rules(world: World) -> None:
    job_id, step_id, ws, path = await setup(world)
    write(path, "src/module.py", MODULE_V2)
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    bound = await bound_run(world, job_id, step_id, workspace_id=ws.id, changed_files=["src/module.py"])
    with pytest.raises(CommitNotVerifiedError, match="no content digest") as ei:
        await world.engine.commit_verified(ws, "undigested", bound, step_id=step_id)
    assert ei.value.code == "COMMIT_NOT_VERIFIED"
    legacy = await bound_run(world, job_id, step_id, changed_files=["src/module.py"])  # written before the columns existed
    res = await world.engine.commit_verified(ws, "legacy", legacy, step_id=step_id)
    assert res.files == ["src/module.py"]


async def test_handle_without_workspace_row_is_stored_without_reference(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"README.md": "# demo\n", "src/module.py": "VALUE = 1\n"})
    job_id, step_id = await make_job_step(sessionmaker)
    write(repo, "src/module.py", "VALUE = 2\n")
    h = handle(repo, job_id)  # ad-hoc handle: its id names no workspaces row
    verifier = Verifier(sessionmaker, POLICIES, WorkspaceShellExecutor(), GitCliReader())
    vstep = VerificationStep.build(key="S1", kind="implement", acceptance=[MODULE_IS_2], scope=SCOPE)
    out = await verifier.run(vstep, h, job_id=job_id, step_id=step_id, attempt_id=None)
    assert out.report.passed
    async with sessionmaker() as s:
        row = await s.get(VerificationRun, out.run_id)
    assert row is not None and row.workspace_id is None
    assert row.content_digest == expected_digest(repo, ["src/module.py"])


# ============================================================================================ runtime adapter
async def test_adapter_hashes_with_the_git_runner_of_the_engine_reader(world: World, tmp_path: Path) -> None:
    """The VerifierAdapter hands the verifier the reader's GitRunner: the digest is computed with the same git
    binary/environment as GitEngine.commit_verified (here: a logging wrapper around git)."""
    calls = tmp_path / "git-calls.log"
    wrapper = tmp_path / "git-wrapper"
    wrapper.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(calls))}\nexec git "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    cfg = get_config()
    runner = GitRunner(author_name=cfg.policies.git.author_name, author_email=cfg.policies.git.author_email, git_binary=str(wrapper))

    async def executor_for(ws: WorkspaceHandle, step: StepContract | None) -> WorkspaceShellExecutor:
        return WorkspaceShellExecutor()

    adapter = VerifierAdapter(world.sessionmaker, cfg, WorkspaceGitReader(runner), executor_for)
    job_id, step_id, ws, path = await setup(world)
    acceptance = [{"type": "presence", "path_glob": "src/module.py", "pattern": "VALUE = 2"}]
    scope = ScopeContract(target_paths=["src/module.py"])
    contract = StepContract.model_validate(
        {
            "id": step_id,
            "job_id": job_id,
            "step_key": "S1",
            "title": "t",
            "kind": "implement",
            "capability": "code",
            "goal": "g",
            "status": "running",
            "acceptance": acceptance,
            "scope": scope,
        }
    )
    write(path, "src/module.py", MODULE_V2)
    out = await adapter.verify(contract, await world.engine.handle(ws), job_id=job_id, step_id=step_id, attempt_id=uuid.uuid4())
    assert out.report.passed, out.report.summary
    logged = calls.read_text(encoding="utf-8").splitlines()  # "-c <cfg> ... <subcommand> <args>" per call
    assert any(" hash-object -- src/module.py" in f" {line}" for line in logged), "the digest bypassed the reader's runner"
    row = await run_row(world, out.run_id)
    assert row.workspace_id == ws.id and row.content_digest == expected_digest(path, ["src/module.py"])
    await world.engine.stage_allowed(ws, scope, step_id=step_id)
    res = await world.engine.commit_verified(ws, "S1", out.run_id, step_id=step_id, scope=scope)
    assert res.files == ["src/module.py"]
