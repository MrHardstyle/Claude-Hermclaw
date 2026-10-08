"""Runtime-controlled Git engine (Bauplan §27, §24; P06 6.1–6.12).

Only the runtime mutates repositories. Models get read-only access through
:class:`hermclaw.gitops.reader.WorkspaceGitReader`; everything that writes (mirror fetch, workspace creation,
staging, commit, push, base update, cleanup, merge requests) goes through :class:`GitEngine` and produces one
``git_operations`` row plus one event (``git.operation`` / ``git.commit.created`` / ``git.pushed`` /
``git.merge_request.created``). Refusals (protected branch, missing verification, scope) are recorded with
status ``refused`` before the error is raised.

Layout on the orchestrator::

    <settings.repos_cache_dir>/<repo>.git              bare mirror cache (heads + tags of the upstream)
    <settings.workspaces_dir>/<job_id>/<repo>/          isolated full clone per job, job branch checked out
    <settings.workspaces_dir>/.locks/                   flock files (cross-process safety)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import HermclawConfig, PoliciesConfig
from hermclaw.core.errors import (
    HermclawError,
    MergeConflictError,
    NotFoundError,
    ProtectedBranchError,
    ScopeViolation,
    StaleBaseError,
    ValidationFailed,
)
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.core.settings import Settings
from hermclaw.events.store import append_event
from hermclaw.gitops import _fs, ops
from hermclaw.gitops._secrets import secret_file_path
from hermclaw.gitops.audit import SOURCE_ID, SOURCE_TYPE, OpRecord, classify_error, write_audit
from hermclaw.gitops.errors import (
    BaseBranchNotFound,
    CommitNotVerifiedError,
    GitCommandError,
    GitLabError,
    NothingToCommitError,
    NothingToPushError,
    NotJobBranchError,
    PushRejectedError,
    WorkspaceNotFound,
    WorkspacePathViolation,
    WorkspaceStateError,
    WorkspaceTamperedError,
)
from hermclaw.gitops.gitlab import GitLabClient
from hermclaw.gitops.locks import KeyedLocks, combined_lock
from hermclaw.gitops.naming import job_branch_name, safe_dir_name, validate_branch_name
from hermclaw.gitops.parsing import parse_name_status, parse_push_porcelain
from hermclaw.gitops.reader import WorkspaceGitReader
from hermclaw.gitops.registry import RepoRef, RepositoryRegistry
from hermclaw.gitops.runner import GitRunner, GitSshOptions
from hermclaw.gitops.scope_guard import Operation, StagingGuard, first_match, gitattributes_lines, normalise_repo_path
from hermclaw.gitops.types import (
    ACTIVE_WORKSPACE_STATES,
    BaseStatus,
    CommitResult,
    DiffResult,
    MergeRequestInfo,
    PushResult,
    RecoveryResult,
    RefusedPath,
    RepositoryInfo,
    StagedPath,
    StageResult,
    UpdateResult,
    WorkspaceInfo,
    WorkspaceStatus,
)
from hermclaw.gitops.urls import project_path_from_url, strip_userinfo
from hermclaw.persistence.models import GitOperation, Job, Repository, VerificationRun, Workspace

log = get_logger(__name__)

WorkspaceRef = WorkspaceInfo | WorkspaceHandle | uuid.UUID
UpdateStrategy = Literal["rebase", "merge"]
MAX_LISTED_PATHS = 200
AUTOSTASH_MESSAGE = "hermclaw-autostash"
# git stash uses magic pathspecs (":/") internally; with GIT_LITERAL_PATHSPECS=1 untracked files are not cleaned
STASH_ENV = {"GIT_LITERAL_PATHSPECS": "0"}


@dataclass(frozen=True, slots=True)
class DiffLimits:
    max_patch_bytes: int = ops.DEFAULT_MAX_PATCH_BYTES
    max_files: int = ops.DEFAULT_MAX_FILES
    context_lines: int = 3


@dataclass(frozen=True, slots=True)
class EngineTimeouts:
    """Seconds. ``network`` applies to clone/fetch/push/ls-remote, ``lock_wait`` to mirror/workspace locks."""

    network: float = 900.0
    lock_wait: float = 300.0


def ssh_options_from_config(config: HermclawConfig) -> GitSshOptions:
    """SSH transport for the GitLab host: dedicated key (``ssh.key_ref``), pinned known_hosts, strict checking."""
    hosts = config.hosts.by_role("gitlab")
    ssh = hosts[0].ssh if hosts else None
    if ssh is None:
        return GitSshOptions()
    key = secret_file_path(ssh.key_ref)
    known = Path(ssh.known_hosts) if ssh.known_hosts else None
    return GitSshOptions(key_path=key, known_hosts=known)


def build_commit_message(message: str, *, job_id: uuid.UUID, step_id: uuid.UUID | None, verification_run_id: uuid.UUID) -> str:
    """Redacted, normalised commit message with audit trailers (subject ≤ 200 chars, body ≤ 20k chars)."""
    text = DEFAULT_REDACTOR.text(message).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    lines = [line.rstrip() for line in text.strip().split("\n")]
    subject = lines[0].strip() if lines else ""
    if not subject:
        raise ValidationFailed("commit message must not be empty")
    if len(subject) > 200:
        subject = subject[:197] + "..."
    body = "\n".join(lines[1:]).strip()[:20_000]
    trailers = [f"Hermclaw-Job: {job_id}"]
    if step_id is not None:
        trailers.append(f"Hermclaw-Step: {step_id}")
    trailers.append(f"Hermclaw-Verification: {verification_run_id}")
    parts = [subject, *([body] if body else []), "\n".join(trailers)]
    return "\n\n".join(parts) + "\n"


def _verified_paths(changed: Any) -> set[str]:
    out: set[str] = set()
    for item in changed or []:
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, dict):
            for key in ("path", "old_path"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    out.add(value)
    return out


def _cap(items: Sequence[Any], limit: int = MAX_LISTED_PATHS) -> list[Any]:
    return list(items[:limit])


class GitEngine:
    """All runtime git writes. One instance per process; safe for concurrent use (per-mirror/workspace locks)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        policies: PoliciesConfig,
        runner: GitRunner | None = None,
        gitlab: GitLabClient | None = None,
        diff_limits: DiffLimits | None = None,
        timeouts: EngineTimeouts | None = None,
    ) -> None:
        self._sm = sessionmaker
        self.settings = settings
        self.policies = policies
        self.git_policy = policies.git
        self.runner = runner or GitRunner(author_name=policies.git.author_name, author_email=policies.git.author_email)
        self.gitlab = gitlab
        self.registry = RepositoryRegistry(sessionmaker, git_policy=policies.git)
        self.diff_limits = diff_limits or DiffLimits()
        self.timeouts = timeouts or EngineTimeouts()
        self.workspaces_root = Path(settings.workspaces_dir)
        self.mirrors_root = Path(settings.repos_cache_dir)
        self._locks = KeyedLocks()

    @classmethod
    def from_config(
        cls,
        config: HermclawConfig,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        gitlab: GitLabClient | Literal["auto"] | None = "auto",
        **kwargs: Any,
    ) -> GitEngine:
        runner = GitRunner(
            author_name=config.policies.git.author_name,
            author_email=config.policies.git.author_email,
            ssh=ssh_options_from_config(config),
        )
        client = GitLabClient.from_config(config, settings) if gitlab == "auto" else gitlab
        return cls(sessionmaker, settings=settings, policies=config.policies, runner=runner, gitlab=client, **kwargs)

    def reader(self) -> WorkspaceGitReader:
        """Read-only git access for LLM tools (implements ``hermclaw.core.interfaces.GitReader``)."""
        return WorkspaceGitReader(
            self.runner,
            workspaces_root=self.workspaces_root,
            max_patch_bytes=self.diff_limits.max_patch_bytes,
            forbidden_globs=self.policies.scope.always_forbidden,
        )

    # ================================================================================== paths & locks
    def mirror_path(self, repo: RepositoryInfo) -> Path:
        return self.mirrors_root / f"{safe_dir_name(repo.name)}.git"

    def workspace_path(self, job_id: uuid.UUID, repo: RepositoryInfo) -> Path:
        return self.workspaces_root / str(job_id) / safe_dir_name(repo.name)

    def _lock_dir(self) -> Path:
        return self.workspaces_root / ".locks"

    def _mirror_lock(self, repo: RepositoryInfo) -> contextlib.AbstractAsyncContextManager[None]:
        name = safe_dir_name(repo.name)
        return combined_lock(
            self._locks, f"mirror:{name}", self.mirrors_root / ".locks" / f"{name}.lock", wait_seconds=self.timeouts.lock_wait
        )

    def _workspace_lock(self, workspace_id: uuid.UUID) -> contextlib.AbstractAsyncContextManager[None]:
        return combined_lock(
            self._locks, f"ws:{workspace_id}", self._lock_dir() / f"ws-{workspace_id}.lock", wait_seconds=self.timeouts.lock_wait
        )

    def _create_lock(self, job_id: uuid.UUID, repo: RepositoryInfo) -> contextlib.AbstractAsyncContextManager[None]:
        key = f"create-{job_id}-{repo.id}"
        return combined_lock(self._locks, key, self._lock_dir() / f"{key}.lock", wait_seconds=self.timeouts.lock_wait)

    def _verification_lock(self, verification_run_id: uuid.UUID) -> contextlib.AbstractAsyncContextManager[None]:
        """Serialises commits that cite the same verification run (a run may back exactly one commit)."""
        key = f"vr-{verification_run_id}"
        return combined_lock(self._locks, key, self._lock_dir() / f"{key}.lock", wait_seconds=self.timeouts.lock_wait)

    # ================================================================================== audit
    async def _record(self, rec: OpRecord, *, status: str, error: BaseException | None = None) -> None:
        """Record a failed/refused operation; never masks the original error."""
        try:
            async with self._sm() as session:
                await write_audit(session, rec, status=status, error=error)
                await session.commit()
        except Exception:
            log.exception("could not record git operation", extra={"operation": rec.operation, "status": status})

    @contextlib.asynccontextmanager
    async def _audited(self, rec: OpRecord) -> AsyncIterator[OpRecord]:
        """Every write: ``ok`` row on success (unless the body recorded it transactionally), ``failed``/``refused`` on error."""
        try:
            yield rec
        except Exception as exc:
            if not rec.recorded:
                await self._record(rec, status=classify_error(exc), error=exc)
            raise
        if not rec.recorded:
            async with self._sm() as session:
                await write_audit(session, rec, status="ok")
                await session.commit()

    # ================================================================================== 6.2 clone / fetch
    async def sync_mirror(self, repo: RepoRef, *, job_id: uuid.UUID | None = None) -> Path:
        """Clone (first use) or fetch (``--prune``) the bare mirror cache of ``repo``; returns the mirror path."""
        info = await self.registry.resolve(repo)
        async with self._mirror_lock(info):
            return await self._sync_mirror_locked(info, job_id=job_id)

    clone = sync_mirror  # Bauplan §27 operation names: the first sync clones, later ones fetch
    fetch = sync_mirror

    async def _sync_mirror_locked(self, info: RepositoryInfo, *, job_id: uuid.UUID | None) -> Path:
        path = self.mirror_path(info)
        exists = await asyncio.to_thread(_fs.exists, path / "HEAD")
        rec = OpRecord(
            operation="mirror.fetch" if exists else "mirror.clone",
            job_id=job_id,
            details={"repository": info.name, "repository_id": str(info.id), "url": strip_userinfo(info.url), "mirror": str(path)},
        )
        async with self._audited(rec):
            if exists:
                await self._ensure_mirror_remote(info, path, rec)
                res = await self.runner.run(["fetch", "--prune", "--quiet", "origin"], cwd=path, timeout_s=self.timeouts.network)
                rec.details["duration_ms"] = res.duration_ms
            else:
                await self._init_mirror(info, path)
            rec.ref = f"refs/heads/{info.default_branch}"
            rec.sha_after = await ops.rev_parse(self.runner, path, f"refs/heads/{info.default_branch}")
        return path

    async def _init_mirror(self, info: RepositoryInfo, path: Path) -> None:
        await asyncio.to_thread(_fs.ensure_dir, path.parent)
        tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            await self.runner.run(["init", "--bare", "--quiet", str(tmp)])
            await self.runner.run(["config", "remote.origin.url", info.url], cwd=tmp)
            await self.runner.run(["config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"], cwd=tmp)
            await self.runner.run(["config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*"], cwd=tmp)
            await self.runner.run(["fetch", "--prune", "--quiet", "origin"], cwd=tmp, timeout_s=self.timeouts.network)
            await self.runner.run(["symbolic-ref", "HEAD", f"refs/heads/{info.default_branch}"], cwd=tmp)
            await asyncio.to_thread(_fs.rename, tmp, path)
        except BaseException:
            await asyncio.to_thread(_fs.remove_tree, tmp)
            raise

    async def _ensure_mirror_remote(self, info: RepositoryInfo, path: Path, rec: OpRecord) -> None:
        res = await self.runner.run(["config", "--get", "remote.origin.url"], cwd=path, check=False)
        if res.first_line != info.url:
            await self.runner.run(["config", "remote.origin.url", info.url], cwd=path)
            rec.details["remote_url_updated"] = True

    # ================================================================================== 6.3 base SHA
    async def resolve_base_sha(
        self, repo: RepoRef, branch: str | None = None, *, fetch: bool = True, job_id: uuid.UUID | None = None
    ) -> str:
        """Current SHA of ``branch`` (default branch if omitted) on the upstream, via the mirror cache."""
        info = await self.registry.resolve(repo)
        name = validate_branch_name(branch or info.default_branch)
        async with self._mirror_lock(info):
            path = self.mirror_path(info)
            if fetch or not await asyncio.to_thread(_fs.exists, path / "HEAD"):
                await self._sync_mirror_locked(info, job_id=job_id)
            sha = await ops.rev_parse(self.runner, path, f"refs/heads/{name}")
        if sha is None:
            raise BaseBranchNotFound(
                f"branch '{name}' does not exist in repository '{info.name}'", details={"branch": name, "repository": info.name}
            )
        return sha

    # ================================================================================== 6.4 / 6.5 workspaces + job branches
    def job_branch(self, job_id: uuid.UUID, title_or_slug: str) -> str:
        return job_branch_name(self.git_policy.branch_prefix, job_id, title_or_slug)

    async def create_workspace(
        self,
        job_id: uuid.UUID,
        repo: RepoRef,
        *,
        base_branch: str | None = None,
        slug: str | None = None,
        step_id: uuid.UUID | None = None,
        fetch: bool = True,
    ) -> WorkspaceInfo:
        """Isolated full clone for ``job_id`` on a new job branch ``<prefix><job-short-id>-<slug>``.

        Idempotent: an active workspace of the same job and repository is returned as is. If the job branch
        already exists upstream and contains the base commit (a retried job), the workspace resumes from it.
        """
        info = await self.registry.resolve(repo)
        base = validate_branch_name(base_branch or info.default_branch)
        async with self._create_lock(job_id, info):
            async with self._sm() as session:
                job = await session.get(Job, job_id)
                if job is None:
                    raise NotFoundError(f"job {job_id} not found", details={"job_id": str(job_id)})
                job_title = job.title
                rows = (
                    await session.execute(
                        select(Workspace)
                        .where(
                            Workspace.job_id == job_id, Workspace.repository_id == info.id, Workspace.status.in_(ACTIVE_WORKSPACE_STATES)
                        )
                        .order_by(Workspace.created_at.desc())
                    )
                ).scalars()
                existing = [WorkspaceInfo.from_row(r, repository_name=info.name) for r in rows]
            for ws in existing:
                if await self._usable(ws):
                    return ws
                await self._archive_missing(ws)
            branch = self.job_branch(job_id, slug or job_title)
            return await self._create_workspace_locked(job_id, info, base, branch, step_id=step_id, fetch=fetch)

    async def _usable(self, ws: WorkspaceInfo) -> bool:
        path = Path(ws.path)
        return await asyncio.to_thread(_fs.is_within, path, self.workspaces_root, min_depth=2) and await ops.is_work_tree(self.runner, path)

    async def _archive_missing(self, ws: WorkspaceInfo) -> None:
        rec = OpRecord(
            operation="workspace.archive",
            job_id=ws.job_id,
            workspace_id=ws.id,
            ref=ws.branch,
            details={"reason": "workspace directory missing or invalid", "path": ws.path},
        )
        async with self._sm() as session:
            row = await session.get(Workspace, ws.id)
            if row is not None:
                row.status = "archived"
            await write_audit(session, rec, status="ok")
            await session.commit()

    async def _create_workspace_locked(
        self, job_id: uuid.UUID, info: RepositoryInfo, base: str, branch: str, *, step_id: uuid.UUID | None, fetch: bool
    ) -> WorkspaceInfo:
        rec = OpRecord(
            operation="workspace.create",
            job_id=job_id,
            step_id=step_id,
            ref=branch,
            details={"repository": info.name, "repository_id": str(info.id), "base_branch": base, "branch": branch},
        )
        async with self._audited(rec):
            pattern = self.registry.protected_match(info, branch)
            if pattern is not None or branch == base:
                raise ProtectedBranchError(
                    f"job branch '{branch}' matches protected branch rule '{pattern or base}'",
                    details={"branch": branch, "pattern": pattern or base},
                )
            ws_path = self.workspace_path(job_id, info)
            if not await asyncio.to_thread(_fs.is_within, ws_path, self.workspaces_root, min_depth=2):
                raise WorkspacePathViolation("workspace path escapes the workspace root", details={"path": str(ws_path)})
            await asyncio.to_thread(_fs.ensure_dir, ws_path.parent)
            tmp = ws_path.with_name(f".{ws_path.name}.tmp-{uuid.uuid4().hex[:8]}")
            mirror = self.mirror_path(info)
            try:
                async with self._mirror_lock(info):
                    if fetch or not await asyncio.to_thread(_fs.exists, mirror / "HEAD"):
                        await self._sync_mirror_locked(info, job_id=job_id)
                    base_sha = await ops.rev_parse(self.runner, mirror, f"refs/heads/{base}")
                    if base_sha is None:
                        raise BaseBranchNotFound(
                            f"base branch '{base}' does not exist in repository '{info.name}'", details={"branch": base}
                        )
                    remote_job_sha = await ops.rev_parse(self.runner, mirror, f"refs/heads/{branch}")
                    await self.runner.run(
                        # --no-hardlinks: the sandbox can write the workspace; it must never share object files
                        # (inodes) with the mirror or other jobs' workspaces
                        ["clone", "--quiet", "--no-hardlinks", "--origin", "origin", "--branch", base, "--", str(mirror), str(tmp)],
                        timeout_s=self.timeouts.network,
                    )
                start = base_sha
                resumed = False
                if remote_job_sha and remote_job_sha != base_sha and await ops.is_ancestor(self.runner, tmp, base_sha, remote_job_sha):
                    start, resumed = remote_job_sha, True
                await self._configure_workspace(tmp, info)
                await self.runner.run(["checkout", "--quiet", "--no-track", "-b", branch, start], cwd=tmp)
                await self.runner.run(["branch", "--quiet", "-D", base], cwd=tmp)
                if await asyncio.to_thread(_fs.exists, ws_path):
                    rec.details["replaced_leftover"] = True
                    await asyncio.to_thread(_fs.remove_tree, ws_path)
                await asyncio.to_thread(_fs.rename, tmp, ws_path)
            except BaseException:
                await asyncio.to_thread(_fs.remove_tree, tmp)
                await asyncio.to_thread(_fs.remove_dir_if_empty, ws_path.parent)
                raise
            rec.sha_before = base_sha
            rec.sha_after = start
            rec.details.update(
                {"base_sha": base_sha, "path": str(ws_path), "resumed_from_remote_branch": resumed, "remote_job_sha": remote_job_sha}
            )
            try:
                async with self._sm() as session:
                    row = Workspace(
                        id=uuid.uuid4(),
                        job_id=job_id,
                        repository_id=info.id,
                        path=str(ws_path),
                        branch=branch,
                        base_branch=base,
                        base_sha=base_sha,
                        head_sha=start,
                        status="active",
                    )
                    session.add(row)
                    await session.flush()
                    rec.workspace_id = row.id
                    await write_audit(session, rec, status="ok")
                    await session.commit()
            except BaseException:
                await asyncio.to_thread(_fs.remove_tree, ws_path)
                raise
            return WorkspaceInfo.from_row(row, repository_name=info.name)

    async def _configure_workspace(self, path: Path, info: RepositoryInfo) -> None:
        # pushes go to the real upstream (never the mirror); fetches of the base use the local mirror path
        await self.runner.run(["remote", "set-url", "origin", info.url], cwd=path)
        await self.runner.run(["config", "push.default", "nothing"], cwd=path)
        await self.runner.run(["config", "core.autocrlf", "false"], cwd=path)
        gdir = await ops.git_dir(self.runner, path)
        await asyncio.to_thread(_fs.write_text, gdir / "info" / "attributes", self._attributes_text())

    async def get_workspace(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        info, _repo, _path = await self._load(workspace_id, require_active=False)
        return info

    async def list_workspaces(self, job_id: uuid.UUID) -> list[WorkspaceInfo]:
        async with self._sm() as session:
            rows = (
                (await session.execute(select(Workspace).where(Workspace.job_id == job_id).order_by(Workspace.created_at))).scalars().all()
            )
            names: dict[uuid.UUID, str] = {}
            for r in rows:
                if r.repository_id and r.repository_id not in names:
                    repo = await session.get(Repository, r.repository_id)
                    if repo is not None:
                        names[r.repository_id] = repo.name
            return [WorkspaceInfo.from_row(r, repository_name=names.get(r.repository_id) if r.repository_id else None) for r in rows]

    async def handle(self, ws: WorkspaceRef) -> WorkspaceHandle:
        info, _repo, _path = await self._load(ws)
        return info.to_handle()

    async def _load(
        self, ws: WorkspaceRef, *, require_active: bool = True, verify: bool = True
    ) -> tuple[WorkspaceInfo, RepositoryInfo | None, Path]:
        """Row + repository + path. ``require_active`` also demands a usable work tree whose git metadata is intact."""
        ws_id = ws if isinstance(ws, uuid.UUID) else ws.id
        async with self._sm() as session:
            row = await session.get(Workspace, ws_id)
            if row is None:
                raise WorkspaceNotFound(f"workspace {ws_id} not found", details={"workspace_id": str(ws_id)})
            repo_row = await session.get(Repository, row.repository_id) if row.repository_id else None
            repo = RepositoryInfo.from_row(repo_row) if repo_row is not None else None
            info = WorkspaceInfo.from_row(row, repository_name=repo.name if repo else None)
        path = Path(info.path)
        if not await asyncio.to_thread(_fs.is_within, path, self.workspaces_root, min_depth=2):
            raise WorkspacePathViolation("workspace path is outside the workspace root", details={"workspace_id": str(info.id)})
        if require_active:
            if not info.active:
                raise WorkspaceStateError(f"workspace is {info.status}", details={"workspace_id": str(info.id), "status": info.status})
            if not await ops.is_work_tree(self.runner, path):
                raise WorkspaceStateError("workspace directory is missing or not a git work tree", details={"workspace_id": str(info.id)})
            if verify:
                await self._verify_integrity(info, path)
        return info, repo, path

    @contextlib.asynccontextmanager
    async def _locked(
        self, ws: WorkspaceRef, *, require_active: bool = True
    ) -> AsyncIterator[tuple[WorkspaceInfo, RepositoryInfo | None, Path]]:
        """Workspace lock + a fresh load *inside* the lock (a concurrent cleanup/update may have changed the row)."""
        first, _repo, _path = await self._load(ws, require_active=False)
        async with self._workspace_lock(first.id):
            yield await self._load(first.id, require_active=require_active)

    def _attributes_text(self) -> str:
        return "\n".join(gitattributes_lines(self.policies.scope.always_forbidden)) + "\n"

    async def _verify_integrity(self, info: WorkspaceInfo, path: Path) -> None:
        """Refuse workspaces whose ``.git`` was modified outside the runtime; re-assert the ``-diff`` attributes."""
        problems = await ops.integrity_problems(self.runner, path)
        if problems:
            err = WorkspaceTamperedError(
                "workspace git metadata was modified outside the runtime; refusing to run git in it",
                details={"workspace_id": str(info.id), "problems": _cap(problems, 20)},
            )
            rec = OpRecord(
                operation="workspace.integrity",
                job_id=info.job_id,
                workspace_id=info.id,
                ref=info.branch,
                details={"problems": _cap(problems, 20)},
            )
            await self._record(rec, status="refused", error=err)
            raise err
        attributes = path / ".git" / "info" / "attributes"
        expected = self._attributes_text()
        if await asyncio.to_thread(_fs.read_text, attributes) != expected:
            await asyncio.to_thread(_fs.write_text_nofollow, attributes, expected)
            rec = OpRecord(
                operation="workspace.attributes",
                job_id=info.job_id,
                workspace_id=info.id,
                ref=info.branch,
                details={"repaired": True, "lines": expected.count("\n")},
            )
            async with self._sm() as session:
                await write_audit(session, rec, status="ok")
                await session.commit()

    async def _assert_on_branch(self, info: WorkspaceInfo, path: Path) -> None:
        busy = await ops.operations_in_progress(self.runner, path)
        if busy:
            raise WorkspaceStateError(
                f"a git {busy[0]} is in progress in the workspace (run recover())",
                details={"workspace_id": str(info.id), "in_progress": busy},
            )
        branch = await ops.current_branch(self.runner, path)
        if branch != info.branch:
            raise WorkspaceStateError(
                f"workspace is on {branch or 'a detached HEAD'}, expected job branch '{info.branch}'",
                details={"workspace_id": str(info.id), "branch": branch, "expected": info.branch},
            )

    # ================================================================================== 6.6 status / diff
    async def status(self, ws: WorkspaceRef) -> WorkspaceStatus:
        info, _repo, path = await self._load(ws)
        entries = await ops.read_status(self.runner, path, renames=True)
        head = await ops.head_sha(self.runner, path)
        return WorkspaceStatus(
            branch=await ops.current_branch(self.runner, path),
            head_sha=head,
            base_sha=info.base_sha,
            entries=entries,
            commits_ahead_of_base=await ops.count_commits(self.runner, path, f"{info.base_sha}..{head}"),
        )

    async def diff(
        self,
        ws: WorkspaceRef,
        *,
        paths: Sequence[str] | None = None,
        include_patch: bool = True,
        include_untracked: bool = True,
        max_patch_bytes: int | None = None,
        max_files: int | None = None,
        context_lines: int | None = None,
    ) -> DiffResult:
        """Everything that differs from the workspace's ``base_sha`` (commits, staged, unstaged, untracked)."""
        info, _repo, path = await self._load(ws)
        clean_paths = self._validate_paths(paths)
        return await ops.read_diff(
            self.runner,
            path,
            info.base_sha,
            paths=clean_paths,
            include_patch=include_patch,
            include_untracked=include_untracked,
            max_patch_bytes=max_patch_bytes if max_patch_bytes is not None else self.diff_limits.max_patch_bytes,
            max_files=max_files if max_files is not None else self.diff_limits.max_files,
            context_lines=context_lines if context_lines is not None else self.diff_limits.context_lines,
        )

    async def changed_files(self, ws: WorkspaceRef) -> list[str]:
        info, _repo, path = await self._load(ws)
        return await ops.changed_files(self.runner, path, info.base_sha)

    @staticmethod
    def _validate_paths(paths: Sequence[str] | None) -> list[str] | None:
        if not paths:
            return None
        out: list[str] = []
        for p in paths:
            norm = normalise_repo_path(p)
            if norm is None or ".git" in norm.split("/"):
                raise ValidationFailed(f"invalid repository path {p!r}", details={"path": p})
            out.append(norm)
        return out

    # ================================================================================== 6.7 safe staging
    async def stage_allowed(self, ws: WorkspaceRef, scope: ScopeContract, *, step_id: uuid.UUID | None = None) -> StageResult:
        """Stage exactly the changed paths the scope permits; the index ends up as HEAD + allowed changes.

        Out-of-scope, forbidden and ``always_forbidden`` paths stay untouched in the working tree and are
        reported in ``refused`` (plus a ``scope.violation`` event). Nothing outside the scope is ever staged.
        """
        async with self._locked(ws) as (info, _repo, path):
            rec = OpRecord(
                operation="stage",
                job_id=info.job_id,
                step_id=step_id,
                workspace_id=info.id,
                ref=info.branch,
                details={"scope_version": scope.version, "scope_source": scope.source},
            )
            async with self._audited(rec):
                await self._assert_on_branch(info, path)
                before = await ops.read_status(self.runner, path)
                conflicted = [e.path for e in before if e.conflicted]
                if conflicted:
                    raise WorkspaceStateError("workspace has unresolved conflicts", details={"conflicted": _cap(conflicted)})
                rec.sha_before = rec.sha_after = await ops.head_sha(self.runner, path)
                # reset the index to HEAD: whatever was staged before (by anyone) is re-decided below
                await self.runner.run(["reset", "--quiet", "--mixed", "HEAD"], cwd=path)
                entries = await ops.read_status(self.runner, path)
                guard = StagingGuard(scope, self.policies.scope)
                staged: list[StagedPath] = []
                refused: list[RefusedPath] = []
                for entry in entries:
                    op: Operation = entry.operation
                    decision = guard.decide(entry.path, op)
                    if decision.allowed and op != "delete" and await asyncio.to_thread(_symlink_escapes, path, entry.path):
                        refused.append(RefusedPath(path=entry.path, operation=op, reason="unsafe_symlink"))
                        continue
                    if decision.allowed:
                        staged.append(StagedPath(path=decision.path, operation=op, matched=decision.matched))
                    else:
                        refused.append(
                            RefusedPath(
                                path=entry.path, operation=op, reason=str(decision.reason), matched=decision.matched, detail=decision.detail
                            )
                        )
                if staged:
                    await self.runner.run(
                        ["add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul"],
                        cwd=path,
                        input_data=ops.nul_join([s.path for s in staged]),
                    )
                actual = set(await ops.staged_paths(self.runner, path))
                allowed = {s.path for s in staged}
                extra = sorted(actual - allowed)
                if extra:  # defence in depth – must never happen
                    await self.runner.run(["reset", "--quiet", "--mixed", "HEAD"], cwd=path)
                    raise ScopeViolation("staging produced index entries outside the allowed set", details={"paths": _cap(extra)})
                staged = [s for s in staged if s.path in actual]
                rec.details.update(
                    {
                        "staged": _cap([s.path for s in staged]),
                        "refused": _cap([{"path": r.path, "operation": r.operation, "reason": r.reason} for r in refused]),
                        "staged_count": len(staged),
                        "refused_count": len(refused),
                    }
                )
                async with self._sm() as session:
                    await write_audit(session, rec, status="ok")
                    if refused:
                        await append_event(
                            session,
                            EventType.SCOPE_VIOLATION,
                            source_type=SOURCE_TYPE,
                            source_id=SOURCE_ID,
                            job_id=info.job_id,
                            step_id=step_id,
                            severity=Severity.warning,
                            payload=DEFAULT_REDACTOR.obj(
                                {
                                    "workspace_id": str(info.id),
                                    "phase": "stage",
                                    "scope_version": scope.version,
                                    "refused": rec.details["refused"],
                                    "refused_count": len(refused),
                                }
                            ),
                        )
                    await session.commit()
                return StageResult(staged=staged, refused=refused)
        raise AssertionError("unreachable")  # pragma: no cover

    # ================================================================================== 6.8 runtime commit
    async def commit_verified(
        self,
        ws: WorkspaceRef,
        message: str,
        verification_run_id: uuid.UUID,
        *,
        step_id: uuid.UUID | None = None,
        scope: ScopeContract | None = None,
    ) -> CommitResult:
        """Commit the staged changes – only with a passed, unused verification run of this workspace's job.

        Refused (``COMMIT_NOT_VERIFIED``) if the run is missing, belongs to another job/step, did not pass, was
        already used for a commit, or did not cover a staged path. ``always_forbidden`` paths (and, if
        ``scope`` is given, any out-of-scope path) in the index are refused as scope violations.
        """
        async with self._locked(ws) as (info, _repo, path), self._verification_lock(verification_run_id):
            rec = OpRecord(
                operation="commit",
                job_id=info.job_id,
                step_id=step_id,
                workspace_id=info.id,
                ref=f"refs/heads/{info.branch}",
                event_type=EventType.GIT_COMMIT_CREATED,
                details={"verification_run_id": str(verification_run_id)},
            )
            async with self._audited(rec):
                verified = await self._check_verification(info, verification_run_id, step_id, rec)
                await self._assert_on_branch(info, path)
                if self.registry.protected_match(_repo_or_placeholder(_repo, info), info.branch) is not None:
                    raise ProtectedBranchError(f"refusing to commit on protected branch '{info.branch}'", details={"branch": info.branch})
                parent = await ops.head_sha(self.runner, path)
                rec.sha_before = parent
                changes = await self._staged_changes(path)
                if not changes:
                    raise NothingToCommitError("nothing is staged", details={"workspace_id": str(info.id)})
                self._check_commit_scope(changes, scope)
                staged = sorted(changes)
                if verified:
                    unverified = sorted(set(staged) - verified)
                    if unverified:
                        raise CommitNotVerifiedError(
                            "staged paths were not covered by the verification run",
                            details={"unverified_paths": _cap(unverified), "verification_run_id": str(verification_run_id)},
                        )
                msg = build_commit_message(message, job_id=info.job_id, step_id=rec.step_id, verification_run_id=verification_run_id)
                await self.runner.run(
                    ["commit", "--quiet", "--no-verify", "--cleanup=whitespace", "--file=-"], cwd=path, input_data=msg.encode("utf-8")
                )
                new_sha = await ops.head_sha(self.runner, path)
                rec.sha_after = new_sha
                rec.details.update({"files": _cap(staged), "file_count": len(staged), "subject": msg.split("\n", 1)[0]})
                async with self._sm() as session:
                    row = await session.get(Workspace, info.id)
                    if row is None:
                        raise WorkspaceNotFound(f"workspace {info.id} vanished")
                    row.head_sha = new_sha
                    row.status = "committed"
                    op_row = await write_audit(session, rec, status="ok")
                    await session.commit()
                # the run is used now; later waiters re-check inside the lock, so the lock file can go
                await asyncio.to_thread(_fs.remove_file, self._lock_dir() / f"vr-{verification_run_id}.lock")
                return CommitResult(
                    sha=new_sha,
                    parent_sha=parent,
                    branch=info.branch,
                    files=staged,
                    verification_run_id=verification_run_id,
                    git_operation_id=op_row.id,
                    message=msg,
                )
        raise AssertionError("unreachable")  # pragma: no cover

    async def _check_verification(
        self, info: WorkspaceInfo, verification_run_id: uuid.UUID, step_id: uuid.UUID | None, rec: OpRecord
    ) -> set[str]:
        async with self._sm() as session:
            run = await session.get(VerificationRun, verification_run_id)
            problems: list[str] = []
            verified: set[str] = set()
            if run is None:
                problems.append("verification run does not exist")
            else:
                rec.step_id = rec.step_id or run.step_id
                if run.job_id != info.job_id:
                    problems.append("verification run belongs to a different job")
                if step_id is not None and run.step_id != step_id:
                    problems.append("verification run belongs to a different step")
                if not (run.passed and run.status == "passed"):
                    problems.append(f"verification run did not pass (status={run.status}, passed={run.passed})")
                used = (
                    await session.execute(
                        select(GitOperation.id).where(
                            GitOperation.operation == "commit",
                            GitOperation.status == "ok",
                            GitOperation.details.contains({"verification_run_id": str(verification_run_id)}),
                        )
                    )
                ).first()
                if used is not None:
                    problems.append("verification run was already used for a commit")
                state_since = await self._workspace_state_since(session, info.id)
                if state_since is not None and run.created_at < state_since:
                    problems.append("verification run predates the current workspace state (workspace created or rebased after it)")
                verified = _verified_paths(run.changed_files)
        if problems:
            raise CommitNotVerifiedError(
                "commit refused: " + "; ".join(problems),
                details={"verification_run_id": str(verification_run_id), "problems": problems},
            )
        return verified

    @staticmethod
    async def _workspace_state_since(session: AsyncSession, workspace_id: uuid.UUID) -> datetime | None:
        """When the runtime last replaced the workspace content wholesale (creation or a base update)."""
        row = await session.get(Workspace, workspace_id)
        created = row.created_at if row is not None else None
        updated = (
            await session.execute(
                select(func.max(GitOperation.created_at)).where(
                    GitOperation.workspace_id == workspace_id,
                    GitOperation.operation.in_(("base.rebase", "base.merge")),
                    GitOperation.status == "ok",
                )
            )
        ).scalar_one_or_none()
        candidates = [t for t in (created, updated) if t is not None]
        return max(candidates) if candidates else None

    async def _staged_changes(self, path: Path) -> dict[str, Operation]:
        res = await self.runner.run(["diff", "--cached", "--name-status", "--no-renames", "-z", "HEAD"], cwd=path)
        out: dict[str, Operation] = {}
        for entry in parse_name_status(res.stdout):
            out[entry.path] = "create" if entry.status == "A" else "delete" if entry.status == "D" else "modify"
        return out

    def _check_commit_scope(self, changes: dict[str, Operation], scope: ScopeContract | None) -> None:
        bad: list[dict[str, str]] = []
        for p, op in sorted(changes.items()):
            if scope is not None:
                d = StagingGuard(scope, self.policies.scope).decide(p, op)
                if not d.allowed:
                    bad.append({"path": p, "operation": op, "reason": str(d.reason)})
            elif ".git" in p.split("/") or first_match(p, self.policies.scope.always_forbidden):
                bad.append({"path": p, "operation": op, "reason": "always_forbidden"})
        if bad:
            raise ScopeViolation("staged changes violate the scope policy", details={"violations": _cap(bad)})

    # ================================================================================== 6.9 / 6.10 runtime push
    async def push_job_branch(
        self,
        ws: WorkspaceRef,
        *,
        create_merge_request: bool | None = None,
        title: str | None = None,
        description: str | None = None,
        require_current_base: bool = False,
    ) -> PushResult:
        """Push the workspace's job branch (and nothing else) to the upstream; optionally open a GitLab MR.

        Protected branches (repository row, default branch, ``policies.git.protected_branches`` – fnmatch
        globs) are refused locally before any network access (``PROTECTED_BRANCH``). The push goes to the
        registered repository URL (never a workspace-configured remote) with ``--force-with-lease`` pinned to
        the remote SHA seen just before the push. A non-fast-forward update (rebased job branch) is only
        allowed when that remote SHA is one the runtime itself pushed for this job or saw when it created the
        workspace – commits someone else added to the job branch are never overwritten (``PUSH_REJECTED``).
        """
        async with self._locked(ws) as (info, repo, path):
            if repo is None:
                raise WorkspaceStateError("workspace has no repository", details={"workspace_id": str(info.id)})
            ref = f"refs/heads/{info.branch}"
            url = repo.url
            rec = OpRecord(
                operation="push",
                job_id=info.job_id,
                workspace_id=info.id,
                ref=ref,
                event_type=EventType.GIT_PUSHED,
                details={"repository": repo.name, "branch": info.branch, "url": strip_userinfo(url)},
            )
            async with self._audited(rec):
                self.assert_pushable(repo, info.branch, base_branch=info.base_branch)
                await self._assert_on_branch(info, path)
                head = await ops.head_sha(self.runner, path)
                rec.sha_after = head
                if await ops.count_commits(self.runner, path, f"{info.base_sha}..{head}") == 0:
                    raise NothingToPushError("job branch has no commits beyond the base", details={"branch": info.branch})
                if require_current_base:
                    bs = await self._check_base_locked(info, repo, path, fetch=True)
                    if bs.stale:
                        raise self._stale_error(info, bs)
                remote_before = await ops.ls_remote_head(self.runner, path, url, info.branch, timeout_s=self.timeouts.network)
                rec.sha_before = remote_before
                up_to_date = remote_before == head
                forced = False
                if not up_to_date:
                    if remote_before is not None and not await self._may_replace(info, path, remote_before, head):
                        raise PushRejectedError(
                            f"remote job branch '{info.branch}' has commits the runtime did not push; refusing to overwrite them",
                            details={"branch": info.branch, "reason": "foreign_commits", "remote_sha": remote_before, "head_sha": head},
                        )
                    lease = f"--force-with-lease={ref}:{remote_before or ''}"
                    res = await self.runner.run(
                        ["push", "--porcelain", "--no-verify", lease, url, f"{ref}:{ref}"],
                        cwd=path,
                        check=False,
                        timeout_s=self.timeouts.network,
                    )
                    results = [r for r in parse_push_porcelain(res.text) if r.destination == ref]
                    if res.returncode != 0 or not results or not results[0].ok:
                        reason = results[0].reason or results[0].summary if results else None
                        raise PushRejectedError(
                            f"push of '{info.branch}' was rejected: {reason or 'see stderr'}",
                            details={
                                "branch": info.branch,
                                "reason": reason,
                                "exit_code": res.returncode,
                                "stderr": DEFAULT_REDACTOR.text(res.err.strip())[-2000:],
                            },
                        )
                    forced = results[0].flag == "+"
                rec.details.update({"forced": forced, "up_to_date": up_to_date, "remote_sha_before": remote_before})
                async with self._sm() as session:
                    row = await session.get(Workspace, info.id)
                    if row is None:
                        raise WorkspaceNotFound(f"workspace {info.id} vanished")
                    row.head_sha = head
                    row.status = "pushed"
                    op_row = await write_audit(session, rec, status="ok")
                    await session.commit()
        want_mr = self.git_policy.create_merge_request if create_merge_request is None else create_merge_request
        mr: MergeRequestInfo | None = None
        mr_error: str | None = None
        if want_mr:
            mr, mr_error = await self._ensure_merge_request(info, repo, head, title=title, description=description)
        return PushResult(
            branch=info.branch,
            sha=head,
            remote_sha_before=remote_before,
            forced=forced,
            up_to_date=up_to_date,
            git_operation_id=op_row.id,
            merge_request=mr,
            merge_request_error=mr_error,
        )

    async def _known_remote_shas(self, info: WorkspaceInfo) -> set[str]:
        """Remote job-branch SHAs the runtime is entitled to replace: its own pushes of this ref for this job
        (any workspace of the job) and the remote tip it saw when this workspace was created."""
        ref = f"refs/heads/{info.branch}"
        async with self._sm() as session:
            pushed = (
                await session.execute(
                    select(GitOperation.sha_after).where(
                        GitOperation.job_id == info.job_id,
                        GitOperation.operation == "push",
                        GitOperation.status == "ok",
                        GitOperation.ref == ref,
                    )
                )
            ).scalars()
            known = {sha for sha in pushed if sha}
            created = (
                await session.execute(
                    select(GitOperation.details).where(
                        GitOperation.workspace_id == info.id,
                        GitOperation.operation == "workspace.create",
                        GitOperation.status == "ok",
                    )
                )
            ).scalars()
            for details in created:
                seen = (details or {}).get("remote_job_sha")
                if isinstance(seen, str) and ops.is_sha(seen):
                    known.add(seen)
        return known

    async def _may_replace(self, info: WorkspaceInfo, path: Path, remote_sha: str, head: str) -> bool:
        """True if updating the remote job branch from ``remote_sha`` to ``head`` loses no foreign commits."""
        if await ops.has_commit(self.runner, path, remote_sha) and await ops.is_ancestor(self.runner, path, remote_sha, head):
            return True  # fast-forward
        return remote_sha in await self._known_remote_shas(info)

    def assert_pushable(self, repo: RepositoryInfo, branch: str, *, base_branch: str | None = None) -> None:
        """Local push policy: never a protected branch, never the base branch, only ``branch_prefix`` branches."""
        validate_branch_name(branch)
        pattern = self.registry.protected_match(repo, branch)
        if pattern is not None:
            raise ProtectedBranchError(
                f"refusing to push protected branch '{branch}' (rule '{pattern}')", details={"branch": branch, "pattern": pattern}
            )
        if base_branch is not None and branch == base_branch:
            raise ProtectedBranchError(f"refusing to push the base branch '{branch}'", details={"branch": branch, "pattern": base_branch})
        prefix = self.git_policy.branch_prefix
        if prefix and not branch.startswith(prefix):
            raise NotJobBranchError(
                f"only job branches ('{prefix}*') may be pushed, not '{branch}'", details={"branch": branch, "prefix": prefix}
            )

    async def create_merge_request(self, ws: WorkspaceRef, *, title: str | None = None, description: str | None = None) -> MergeRequestInfo:
        """Open (or reuse – duplicate detection) the GitLab merge request job branch -> base branch.

        Only for a pushed workspace; :meth:`push_job_branch` does this automatically when
        ``policies.git.create_merge_request`` (or its ``create_merge_request`` argument) is set.
        """
        info, repo, _path = await self._load(ws)
        if repo is None:
            raise WorkspaceStateError("workspace has no repository", details={"workspace_id": str(info.id)})
        if info.status != "pushed" or not info.head_sha:
            raise WorkspaceStateError(
                "push the job branch before opening a merge request", details={"workspace_id": str(info.id), "status": info.status}
            )
        self.assert_pushable(repo, info.branch, base_branch=info.base_branch)
        mr, error = await self._ensure_merge_request(info, repo, info.head_sha, title=title, description=description)
        if mr is None:
            raise GitLabError(f"merge request not created: {error}", details={"workspace_id": str(info.id), "reason": error})
        return mr

    async def _ensure_merge_request(
        self, info: WorkspaceInfo, repo: RepositoryInfo, head: str, *, title: str | None, description: str | None
    ) -> tuple[MergeRequestInfo | None, str | None]:
        if self.gitlab is None:
            return None, "no GitLab client configured"
        if repo.provider != "gitlab":
            return None, f"repository provider '{repo.provider}' does not support merge requests"
        project = repo.gitlab_project_id or project_path_from_url(repo.url)
        if not project:
            return None, "repository has no GitLab project id"
        async with self._sm() as session:
            job = await session.get(Job, info.job_id)
            job_title = job.title if job is not None else str(info.job_id)
        mr_title = title or f"Hermclaw: {job_title}"
        mr_description = description or "\n".join(
            [
                "Created by the Hermclaw runtime after deterministic verification.",
                "",
                f"- Job: `{info.job_id}`",
                f"- Base: `{info.base_branch}` @ `{info.base_sha}`",
                f"- Head: `{head}`",
            ]
        )
        rec = OpRecord(
            operation="merge_request.create",
            job_id=info.job_id,
            workspace_id=info.id,
            ref=info.branch,
            sha_after=head,
            event_type=EventType.MERGE_REQUEST_CREATED,
            details={"project": project, "source_branch": info.branch, "target_branch": info.base_branch},
        )
        try:
            async with self._audited(rec):
                mr = await self.gitlab.create_merge_request(
                    project,
                    source_branch=info.branch,
                    target_branch=info.base_branch,
                    title=mr_title,
                    description=mr_description,
                    labels=["hermclaw"],
                )
                rec.details.update({"iid": mr.iid, "mr_id": mr.id, "web_url": mr.web_url, "created": mr.created})
                if not mr.created:
                    rec.operation = "merge_request.reuse"
                    rec.event_type = EventType.GIT_OPERATION
        except HermclawError as exc:
            return None, exc.message
        return mr, None

    # ================================================================================== 6.11 stale base detection
    async def check_base(self, ws: WorkspaceRef, *, fetch: bool = True) -> BaseStatus:
        """Compare the workspace's ``base_sha`` with the upstream base branch (fetching the mirror first)."""
        async with self._locked(ws) as (info, repo, path):
            if repo is None:
                raise WorkspaceStateError("workspace has no repository", details={"workspace_id": str(info.id)})
            return await self._check_base_locked(info, repo, path, fetch=fetch)
        raise AssertionError("unreachable")  # pragma: no cover

    async def ensure_base_current(self, ws: WorkspaceRef, *, fetch: bool = True) -> BaseStatus:
        """Raise :class:`StaleBaseError` (recorded as refused ``base.check``) if the upstream base moved."""
        bs = await self.check_base(ws, fetch=fetch)
        if bs.stale:
            info, _repo, _path = await self._load(ws, require_active=False)
            err = self._stale_error(info, bs)
            rec = OpRecord(
                operation="base.check",
                job_id=info.job_id,
                workspace_id=info.id,
                ref=f"refs/heads/{info.base_branch}",
                sha_before=info.base_sha,
                sha_after=bs.remote_sha,
                details=dict(err.details),
            )
            await self._record(rec, status="refused", error=err)
            raise err
        return bs

    @staticmethod
    def _stale_error(info: WorkspaceInfo, bs: BaseStatus) -> StaleBaseError:
        return StaleBaseError(
            f"base branch '{info.base_branch}' moved from {info.base_sha[:12]} to {bs.remote_sha[:12]}"
            + (" (history rewritten)" if bs.rewritten else f" ({bs.commits_behind} new commit(s))"),
            details={
                "workspace_id": str(info.id),
                "base_branch": info.base_branch,
                "base_sha": info.base_sha,
                "remote_sha": bs.remote_sha,
                "commits_behind": bs.commits_behind,
                "rewritten": bs.rewritten,
            },
        )

    async def _check_base_locked(self, info: WorkspaceInfo, repo: RepositoryInfo, path: Path, *, fetch: bool) -> BaseStatus:
        mirror = self.mirror_path(repo)
        async with self._mirror_lock(repo):
            if fetch or not await asyncio.to_thread(_fs.exists, mirror / "HEAD"):
                await self._sync_mirror_locked(repo, job_id=info.job_id)
            remote_sha = await ops.rev_parse(self.runner, mirror, f"refs/heads/{info.base_branch}")
            if remote_sha is None:
                raise BaseBranchNotFound(
                    f"base branch '{info.base_branch}' no longer exists upstream", details={"branch": info.base_branch}
                )
            tracking = f"refs/remotes/origin/{info.base_branch}"
            before = await ops.rev_parse(self.runner, path, tracking)
            if before != remote_sha:
                rec = OpRecord(
                    operation="workspace.fetch_base",
                    job_id=info.job_id,
                    workspace_id=info.id,
                    ref=tracking,
                    sha_before=before,
                    sha_after=remote_sha,
                    details={"base_branch": info.base_branch},
                )
                async with self._audited(rec):
                    await self.runner.run(
                        ["fetch", "--quiet", "--no-tags", "--force", str(mirror), f"+refs/heads/{info.base_branch}:{tracking}"],
                        cwd=path,
                        timeout_s=self.timeouts.network,
                    )
        head = await ops.head_sha(self.runner, path)
        stale = remote_sha != info.base_sha
        rewritten = stale and not await ops.is_ancestor(self.runner, path, info.base_sha, remote_sha)
        behind = await ops.count_commits(self.runner, path, f"{info.base_sha}..{remote_sha}") if stale else 0
        ahead = await ops.count_commits(self.runner, path, f"{info.base_sha}..{head}")
        return BaseStatus(
            base_branch=info.base_branch,
            base_sha=info.base_sha,
            remote_sha=remote_sha,
            stale=stale,
            rewritten=rewritten,
            commits_behind=behind,
            commits_ahead=ahead,
        )

    # ================================================================================== 6.12 conflict handling
    async def update_to_base(self, ws: WorkspaceRef, *, strategy: UpdateStrategy = "rebase", fetch: bool = True) -> UpdateResult:
        """Move the job branch onto the current upstream base (``rebase --onto`` or a merge commit).

        Uncommitted changes are stashed (incl. untracked files) and re-applied. On any conflict the operation
        is aborted, the workspace is restored to exactly its previous HEAD and working tree, and
        :class:`MergeConflictError` lists the conflicted files. The workspace is never left conflicted.
        After a successful update the job's verification must be re-run (the commits changed).
        """
        if strategy not in ("rebase", "merge"):
            raise ValidationFailed(f"unknown update strategy {strategy!r}")
        async with self._locked(ws) as (info, repo, path):
            if repo is None:
                raise WorkspaceStateError("workspace has no repository", details={"workspace_id": str(info.id)})
            bs = await self._check_base_locked(info, repo, path, fetch=fetch)
            head = await ops.head_sha(self.runner, path)
            if not bs.stale:
                return UpdateResult(
                    strategy=strategy,
                    updated=False,
                    old_base_sha=info.base_sha,
                    new_base_sha=info.base_sha,
                    old_head_sha=head,
                    new_head_sha=head,
                )
            rec = OpRecord(
                operation=f"base.{strategy}",
                job_id=info.job_id,
                workspace_id=info.id,
                ref=f"refs/heads/{info.branch}",
                sha_before=head,
                details={"old_base_sha": info.base_sha, "new_base_sha": bs.remote_sha, "rewritten": bs.rewritten},
            )
            async with self._audited(rec):
                await self._assert_on_branch(info, path)
                entries = await ops.read_status(self.runner, path)
                if any(e.conflicted for e in entries):
                    raise WorkspaceStateError(
                        "workspace has unresolved conflicts", details={"conflicted": [e.path for e in entries if e.conflicted]}
                    )
                stashed = False
                if entries:
                    stash_before = await ops.rev_parse(self.runner, path, "refs/stash")
                    await self.runner.run(
                        ["stash", "push", "--include-untracked", "--quiet", "-m", AUTOSTASH_MESSAGE], cwd=path, env=STASH_ENV
                    )
                    stashed = await ops.rev_parse(self.runner, path, "refs/stash") != stash_before
                try:
                    await self._apply_update(info, path, strategy, bs.remote_sha)
                    new_head = await ops.head_sha(self.runner, path)
                    if stashed:
                        pop = await self.runner.run(["stash", "pop", "--quiet"], cwd=path, check=False, env=STASH_ENV)
                        stashed_ok = pop.returncode == 0
                        if not stashed_ok:
                            files = await ops.conflicted_files(self.runner, path)
                            if not files:
                                files = sorted({e.path for e in await ops.read_status(self.runner, path)})
                            details = self._conflict_details(info, bs, strategy, files, phase="autostash")
                            details["stderr"] = DEFAULT_REDACTOR.text(pop.err.strip())[-2000:]
                            raise MergeConflictError("re-applying uncommitted changes onto the new base conflicts", details=details)
                except BaseException:
                    await self._restore(path, head, stashed=stashed)
                    raise
                rec.sha_after = new_head
                rec.details.update(
                    {"stashed": stashed, "commits_rebased": await ops.count_commits(self.runner, path, f"{bs.remote_sha}..{new_head}")}
                )
                async with self._sm() as session:
                    row = await session.get(Workspace, info.id)
                    if row is None:
                        raise WorkspaceNotFound(f"workspace {info.id} vanished")
                    row.base_sha = bs.remote_sha
                    row.head_sha = new_head
                    if row.status == "pushed" and new_head != head:
                        row.status = "committed"  # the rewritten job branch is not on the remote yet
                    op_row = await write_audit(session, rec, status="ok")
                    await session.commit()
                return UpdateResult(
                    strategy=strategy,
                    updated=True,
                    old_base_sha=info.base_sha,
                    new_base_sha=bs.remote_sha,
                    old_head_sha=head,
                    new_head_sha=new_head,
                    stashed=stashed,
                    git_operation_id=op_row.id,
                )
        raise AssertionError("unreachable")  # pragma: no cover

    async def _apply_update(self, info: WorkspaceInfo, path: Path, strategy: UpdateStrategy, new_base: str) -> None:
        if strategy == "rebase":
            args = ["rebase", "--quiet", "--no-autostash", "--onto", new_base, info.base_sha]
        else:
            args = [
                "merge",
                "--quiet",
                "--no-ff",
                "--no-edit",
                "-m",
                f"Merge {info.base_branch} ({new_base[:12]}) into {info.branch}",
                new_base,
            ]
        res = await self.runner.run(args, cwd=path, check=False, timeout_s=self.timeouts.network)
        if res.returncode == 0:
            return
        files = await ops.conflicted_files(self.runner, path)
        if not files and "conflict" not in (res.text + res.err).lower():
            raise GitCommandError(
                f"git {strategy} failed (exit {res.returncode})",
                details={"exit_code": res.returncode, "stderr": DEFAULT_REDACTOR.text(res.err.strip())[-2000:]},
            )
        bs_details = {"workspace_id": str(info.id), "base_sha": info.base_sha, "remote_sha": new_base, "strategy": strategy}
        raise MergeConflictError(
            f"{strategy} onto {info.base_branch} ({new_base[:12]}) conflicts in {len(files)} file(s)",
            details={**bs_details, "files": _cap(files), "phase": strategy},
        )

    @staticmethod
    def _conflict_details(info: WorkspaceInfo, bs: BaseStatus, strategy: str, files: list[str], *, phase: str) -> dict[str, Any]:
        return {
            "workspace_id": str(info.id),
            "base_sha": info.base_sha,
            "remote_sha": bs.remote_sha,
            "strategy": strategy,
            "files": _cap(files),
            "phase": phase,
        }

    async def _restore(self, path: Path, head: str, *, stashed: bool) -> list[str]:
        """Return the work tree to ``head`` (+ the stashed changes) after a failed update."""
        actions: list[str] = []
        for op_name in await ops.operations_in_progress(self.runner, path):
            res = await self.runner.run([op_name, "--abort"], cwd=path, check=False)
            actions.append(f"{op_name} --abort (exit {res.returncode})")
        current = await ops.rev_parse(self.runner, path, "HEAD")
        status = await ops.read_status(self.runner, path)
        if current != head or any(not e.untracked for e in status):
            await self.runner.run(["reset", "--quiet", "--hard", head], cwd=path)
            actions.append(f"reset --hard {head[:12]}")
        if stashed or any(e.untracked for e in status):
            await self.runner.run(["clean", "--quiet", "-f", "-d"], cwd=path)
            actions.append("clean -fd")
        if stashed:
            await self.runner.run(["stash", "pop", "--quiet"], cwd=path, env=STASH_ENV)
            actions.append("stash pop")
        return actions

    async def recover(self, ws: WorkspaceRef) -> RecoveryResult:
        """Crash recovery: abort interrupted rebase/merge/cherry-pick, back to the job branch tip."""
        async with self._locked(ws) as (info, _repo, path):
            rec = OpRecord(operation="workspace.recover", job_id=info.job_id, workspace_id=info.id, ref=info.branch)
            async with self._audited(rec):
                actions: list[str] = []
                for op_name in await ops.operations_in_progress(self.runner, path):
                    res = await self.runner.run([op_name, "--abort"], cwd=path, check=False)
                    actions.append(f"{op_name} --abort (exit {res.returncode})")
                gdir = await ops.git_dir(self.runner, path)
                if await asyncio.to_thread(_fs.exists, gdir / "index.lock"):
                    await asyncio.to_thread(_fs.remove_file, gdir / "index.lock")
                    actions.append("removed stale index.lock")
                branch = await ops.current_branch(self.runner, path)
                if branch != info.branch:
                    await self.runner.run(["checkout", "--quiet", info.branch], cwd=path)
                    actions.append(f"checkout {info.branch}")
                if await ops.conflicted_files(self.runner, path):
                    await self.runner.run(["reset", "--quiet", "--mixed", "HEAD"], cwd=path)
                    actions.append("reset index (unmerged entries)")
                head = await ops.head_sha(self.runner, path)
                status = await ops.read_status(self.runner, path)
                rec.sha_after = head
                rec.details["actions"] = actions
                return RecoveryResult(actions=actions, head_sha=head, clean=not status)
        raise AssertionError("unreachable")  # pragma: no cover

    # ================================================================================== cleanup
    async def cleanup(self, ws: WorkspaceRef, *, force: bool = False) -> WorkspaceInfo:
        """Remove the workspace directory and mark the row ``cleaned`` (idempotent).

        Refuses (``WORKSPACE_STATE``) if the job branch has verified commits that were never pushed, unless
        ``force``. A workspace whose git metadata was tampered with is removed without running git in it.
        """
        async with self._locked(ws, require_active=False) as (info, _repo, path):
            if info.status == "cleaned":
                return info
            rec = OpRecord(
                operation="workspace.cleanup",
                job_id=info.job_id,
                workspace_id=info.id,
                ref=info.branch,
                sha_before=info.head_sha,
                details={"path": info.path, "status_before": info.status, "force": force},
            )
            async with self._audited(rec):
                present = await ops.is_work_tree(self.runner, path)
                if present and info.status == "committed" and not force:
                    raise WorkspaceStateError(
                        "workspace has verified commits that were never pushed (use force=True to discard)",
                        details={"workspace_id": str(info.id), "head_sha": info.head_sha},
                    )
                if present:
                    problems = await ops.integrity_problems(self.runner, path)
                    if problems:
                        rec.details["integrity_problems"] = _cap(problems, 20)
                    else:
                        entries = await ops.read_status(self.runner, path)
                        rec.details.update({"head_sha": await ops.head_sha(self.runner, path), "uncommitted_changes": len(entries)})
                await asyncio.to_thread(_fs.remove_tree, path)
                await asyncio.to_thread(_fs.remove_dir_if_empty, path.parent)
                async with self._sm() as session:
                    row = await session.get(Workspace, info.id)
                    if row is None:
                        raise WorkspaceNotFound(f"workspace {info.id} vanished")
                    row.status = "cleaned"
                    await write_audit(session, rec, status="ok")
                    await session.commit()
                    cleaned = WorkspaceInfo.from_row(row, repository_name=info.repository_name)
        # safe to drop: every operation re-reads the row inside the lock and refuses a cleaned workspace
        await asyncio.to_thread(_fs.remove_file, self._lock_dir() / f"ws-{info.id}.lock")
        return cleaned

    async def cleanup_job(self, job_id: uuid.UUID, *, force: bool = False) -> list[WorkspaceInfo]:
        out: list[WorkspaceInfo] = []
        for ws in await self.list_workspaces(job_id):
            out.append(await self.cleanup(ws.id, force=force) if ws.status != "cleaned" else ws)
        return out


def _repo_or_placeholder(repo: RepositoryInfo | None, info: WorkspaceInfo) -> RepositoryInfo:
    if repo is not None:
        return repo
    return RepositoryInfo(
        id=info.repository_id or uuid.UUID(int=0),
        name=info.repository_name or "unknown",
        url="/dev/null",
        default_branch=info.base_branch,
        provider="generic",
    )


def _symlink_escapes(root: Path, rel: str) -> bool:
    """True if ``root/rel`` is a symlink whose target resolves outside ``root``."""
    candidate = root / rel
    try:
        if not candidate.is_symlink():
            return False
        target = Path(os.path.realpath(candidate))
        base = Path(os.path.realpath(root))
    except OSError:
        return True
    return not (target == base or target.is_relative_to(base))
