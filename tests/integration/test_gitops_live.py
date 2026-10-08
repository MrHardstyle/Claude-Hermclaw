"""Live checks of the Git engine against the real GitLab host (.226) – BLOCKER-001: skipped by default.

Run on the orchestrator (.225) with::

    HERMCLAW_LIVE_GITLAB_REPO_URL=git@192.168.178.226:hermclaw/sandbox.git \\
    HERMCLAW_LIVE_GITLAB_API=http://192.168.178.226 \\
    HERMCLAW_LIVE_GITLAB_PROJECT=hermclaw/sandbox \\
    HERMCLAW_LIVE_GITLAB_TOKEN_REF=cred:gitlab-token \\
    HERMCLAW_LIVE_SSH_KEY_REF=file:/etc/hermclaw/ssh/id_ed25519 \\
    HERMCLAW_LIVE_KNOWN_HOSTS=/etc/hermclaw/ssh/known_hosts \\
    .venv/bin/pytest -m live tests/integration/test_gitops_live.py

The test pushes one throw-away job branch, opens (and re-finds) a merge request, verifies that ``main`` is
refused locally and deletes the job branch again.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import GitPolicy, PoliciesConfig
from hermclaw.core.errors import ProtectedBranchError
from hermclaw.core.settings import Settings
from hermclaw.gitops import GitEngine, GitLabClient, GitRunner, GitSshOptions
from hermclaw.gitops._secrets import secret_file_path
from hermclaw.persistence.models import Job, Step, VerificationRun

pytestmark = [pytest.mark.live, pytest.mark.integration]

REQUIRED = ("HERMCLAW_LIVE_GITLAB_REPO_URL", "HERMCLAW_LIVE_GITLAB_API", "HERMCLAW_LIVE_GITLAB_PROJECT")


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        pytest.skip(f"{name} not set (BLOCKER-001: GitLab .226 not reachable from this environment)")
    return value


async def test_live_gitlab_push_and_merge_request(sessionmaker, tmp_path):
    for name in REQUIRED:
        _env(name)
    ssh = GitSshOptions(
        key_path=secret_file_path(_env("HERMCLAW_LIVE_SSH_KEY_REF", "file:/etc/hermclaw/ssh/id_ed25519")),
        known_hosts=Path(_env("HERMCLAW_LIVE_KNOWN_HOSTS", "/etc/hermclaw/ssh/known_hosts")),
    )
    policies = PoliciesConfig(git=GitPolicy(create_merge_request=True))
    runner = GitRunner(author_name=policies.git.author_name, author_email=policies.git.author_email, ssh=ssh)
    gitlab = GitLabClient(_env("HERMCLAW_LIVE_GITLAB_API"), token_ref=_env("HERMCLAW_LIVE_GITLAB_TOKEN_REF", "cred:gitlab-token"))
    engine = GitEngine(sessionmaker, settings=Settings(data_dir=tmp_path), policies=policies, runner=runner, gitlab=gitlab)
    project = _env("HERMCLAW_LIVE_GITLAB_PROJECT")
    try:
        repo = await engine.registry.register(
            f"live-{uuid.uuid4().hex[:6]}", _env("HERMCLAW_LIVE_GITLAB_REPO_URL"), provider="gitlab", gitlab_project_id=project
        )
        repo = await engine.registry.sync_protected_branches(repo, gitlab)
        assert repo.default_branch in engine.registry.protected_patterns(repo)
        with pytest.raises(ProtectedBranchError):
            engine.assert_pushable(repo, repo.default_branch)
        async with sessionmaker() as s:
            job = Job(title=f"live git engine check {uuid.uuid4().hex[:6]}", prompt="live", repository_id=repo.id)
            s.add(job)
            await s.flush()
            step = Step(job_id=job.id, step_key="S1", title="t", kind="implement", capability="code", goal="g")
            s.add(step)
            await s.commit()
            job_id, step_id = job.id, step.id
        ws = await engine.create_workspace(job_id, repo)
        (Path(ws.path) / "hermclaw-live-check.txt").write_text(f"{job_id}\n")
        scope = ScopeContract(allowed_new_paths=["hermclaw-live-check.txt"], allowed_operations=["create"])
        assert (await engine.stage_allowed(ws, scope)).staged_paths == ["hermclaw-live-check.txt"]
        async with sessionmaker() as s:
            run = VerificationRun(job_id=job_id, step_id=step_id, passed=True, status="passed", changed_files=["hermclaw-live-check.txt"])
            s.add(run)
            await s.commit()
            run_id = run.id
        commit = await engine.commit_verified(ws, "live check", run_id)
        pushed = await engine.push_job_branch(ws)
        assert pushed.sha == commit.sha and pushed.merge_request is not None, pushed.merge_request_error
        again = await engine.create_merge_request(ws)
        assert not again.created and again.iid == pushed.merge_request.iid
        assert await gitlab.delete_branch(project, ws.branch)
        await engine.cleanup(ws, force=True)
    finally:
        await gitlab.aclose()
