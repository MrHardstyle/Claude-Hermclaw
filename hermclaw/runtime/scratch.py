"""Scratch workspace for jobs without a repository (R-026: SSH/admin, database, container and research jobs).

Every step that runs tools needs a workspace: the tool engine, the scope audit and the verifier are bound to one. A
job without a repository therefore gets a *scratch repository* of its own, created by the runtime:

* a local bare repository ``<data_dir>/scratch/<job_id>.git`` with one initial commit on ``main`` (git plumbing,
  hardened :class:`~hermclaw.gitops.runner.GitRunner`), registered as provider ``generic`` with
  ``metadata.scratch = true``, so it never gets a merge request and nothing leaves the orchestrator;
* a normal job workspace of it (``GitEngine.create_workspace``) – scope, audit, verification and commits work exactly
  as for project repositories.

Remote operations (``ssh``/``deploy`` steps) get the read-only deny-all-write scope of the scope engine; steps that
write files (reports, generated migrations, compose files) are scoped and verified like any other step, and the
finalize phase pushes the job branch into the scratch repository so the results survive workspace cleanup.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.logging import get_logger
from hermclaw.gitops.engine import GitEngine
from hermclaw.gitops.types import ACTIVE_WORKSPACE_STATES, WorkspaceInfo
from hermclaw.persistence.models import Job, Repository

log = get_logger(__name__)

SCRATCH_BRANCH = "main"
_README = (
    b"# Hermclaw scratch repository\n\n"
    b"Created by the Hermclaw runtime for a job without a repository. It holds the files the job's steps produced\n"
    b"(reports, generated artefacts); it is local to the orchestrator and is never pushed anywhere else.\n"
)


def scratch_repository_name(job_id: uuid.UUID) -> str:
    return f"scratch-{job_id.hex[:12]}"


class ScratchWorkspaces:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], git: GitEngine, root: Path) -> None:
        self.sm, self.git, self.root = sessionmaker, git, Path(root)

    async def is_scratch(self, repository_id: uuid.UUID | None) -> bool:
        if repository_id is None:
            return False
        async with self.sm() as s:
            meta = (await s.execute(select(Repository.metadata_).where(Repository.id == repository_id))).scalar_one_or_none()
        return bool((meta or {}).get("scratch"))

    async def existing(self, job_id: uuid.UUID) -> WorkspaceInfo | None:
        """The job's active scratch workspace, if one was created."""
        for ws in await self.git.list_workspaces(job_id):
            if ws.status in ACTIVE_WORKSPACE_STATES and await self.is_scratch(ws.repository_id):
                return ws
        return None

    async def ensure(self, job_id: uuid.UUID) -> WorkspaceInfo | None:
        """Scratch workspace of a repository-less job (created on first use); ``None`` for jobs with a repository."""
        async with self.sm() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        if job.repository_id is not None:
            return None
        found = await self.existing(job_id)
        if found is not None:
            return found
        bare = await self._bare_repository(job_id)
        repo = await self.git.registry.register(
            scratch_repository_name(job_id),
            bare.as_uri(),
            default_branch=SCRATCH_BRANCH,
            provider="generic",
            metadata={"scratch": True, "job_id": str(job_id)},
        )
        ws = await self.git.create_workspace(job_id, repo.id, base_branch=SCRATCH_BRANCH, slug=job.title)
        log.info("scratch workspace created", extra={"job_id": str(job_id), "workspace_id": str(ws.id), "repository": repo.name})
        return ws

    async def _bare_repository(self, job_id: uuid.UUID) -> Path:
        path = self.root / f"{job_id}.git"
        run = self.git.runner.run
        if (path / "HEAD").exists():
            head = await run(["rev-parse", "--verify", "--quiet", f"refs/heads/{SCRATCH_BRANCH}"], cwd=path, check=False)
            if head.returncode == 0:
                return path
        path.mkdir(parents=True, exist_ok=True)
        await run(["init", "--bare", "--quiet", f"--initial-branch={SCRATCH_BRANCH}", str(path)])
        blob = (await run(["hash-object", "-w", "--stdin"], cwd=path, input_data=_README)).text.strip()
        tree = (await run(["mktree"], cwd=path, input_data=f"100644 blob {blob}\tREADME.md\n".encode())).text.strip()
        commit = (await run(["commit-tree", tree, "-m", f"Hermclaw scratch repository for job {job_id}"], cwd=path)).text.strip()
        await run(["update-ref", f"refs/heads/{SCRATCH_BRANCH}", commit], cwd=path)
        return path


__all__ = ["SCRATCH_BRANCH", "ScratchWorkspaces", "scratch_repository_name"]
