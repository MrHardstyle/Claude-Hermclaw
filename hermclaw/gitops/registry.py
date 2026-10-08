"""Repository registry (P06 6.1): rows in ``repositories`` plus protected-branch policy resolution."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.config import GitPolicy
from hermclaw.core.errors import ConflictError, NotFoundError, ValidationFailed
from hermclaw.events.store import append_event
from hermclaw.gitops.audit import SOURCE_ID, SOURCE_TYPE
from hermclaw.gitops.naming import matches_protected, validate_branch_name
from hermclaw.gitops.types import RepositoryInfo
from hermclaw.gitops.urls import project_path_from_url, strip_userinfo, validate_remote_url
from hermclaw.persistence.models import Repository

if TYPE_CHECKING:
    from hermclaw.gitops.gitlab import GitLabClient

RepoRef = RepositoryInfo | uuid.UUID | str
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
PROVIDERS = frozenset({"gitlab", "generic"})


def validate_repository_name(name: str) -> str:
    value = name.strip()
    if not value or len(value) > 200 or not _NAME.match(value) or ".." in value or value.endswith((".git", ".lock")):
        raise ValidationFailed(
            "repository name must be 1-200 chars of [A-Za-z0-9._-] segments separated by '/', without '..' or a .git suffix",
            details={"name": name},
        )
    return value


def _clean_patterns(patterns: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        q = p.strip()
        if q.startswith("refs/heads/"):
            q = q[len("refs/heads/") :]
        if not q or any(ch.isspace() for ch in q):
            raise ValidationFailed("protected branch patterns must be non-empty and contain no whitespace", details={"pattern": p})
        if q not in out:
            out.append(q)
    return out


class RepositoryRegistry:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], *, git_policy: GitPolicy) -> None:
        self._sessionmaker = sessionmaker
        self._policy = git_policy

    # ------------------------------------------------------------------------------------------- policy
    def protected_patterns(self, repo: RepositoryInfo) -> list[str]:
        """Union of the repository default branch, the repo row's patterns and ``policies.git.protected_branches``."""
        out: list[str] = []
        for p in [repo.default_branch, *repo.protected_branches, *self._policy.protected_branches]:
            if p and p not in out:
                out.append(p)
        return out

    def protected_match(self, repo: RepositoryInfo, branch: str) -> str | None:
        return matches_protected(branch, self.protected_patterns(repo))

    def is_protected(self, repo: RepositoryInfo, branch: str) -> bool:
        return self.protected_match(repo, branch) is not None

    # ------------------------------------------------------------------------------------------- crud
    async def register(
        self,
        name: str,
        url: str,
        *,
        default_branch: str = "main",
        provider: str = "gitlab",
        gitlab_project_id: str | None = None,
        protected_branches: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
        update: bool = False,
    ) -> RepositoryInfo:
        """Register a repository. Idempotent for identical URLs; a different URL needs ``update=True``."""
        name = validate_repository_name(name)
        url = validate_remote_url(url)
        validate_branch_name(default_branch)
        if provider not in PROVIDERS:
            raise ValidationFailed(f"unsupported provider '{provider}'", details={"allowed": sorted(PROVIDERS)})
        patterns = _clean_patterns(protected_branches)
        async with self._sessionmaker() as session:
            row = (await session.execute(select(Repository).where(Repository.name == name))).scalar_one_or_none()
            created = row is None
            if row is None:
                row = Repository(
                    id=uuid.uuid4(),
                    name=name,
                    url=url,
                    default_branch=default_branch,
                    provider=provider,
                    gitlab_project_id=gitlab_project_id or (project_path_from_url(url) if provider == "gitlab" else None),
                    protected_branches=patterns,
                    metadata_=dict(metadata or {}),
                )
                session.add(row)
            elif row.url != url and not update:
                raise ConflictError(
                    f"repository '{name}' is already registered with a different URL",
                    details={"name": name, "registered_url": strip_userinfo(row.url)},
                )
            elif update:
                row.url = url
                row.default_branch = default_branch
                row.provider = provider
                row.gitlab_project_id = gitlab_project_id or row.gitlab_project_id
                row.protected_branches = patterns or list(row.protected_branches or [])
                if metadata is not None:
                    row.metadata_ = dict(metadata)
            else:
                unchanged = RepositoryInfo.from_row(row)
                await session.rollback()
                return unchanged
            await session.flush()
            await append_event(
                session,
                EventType.GIT_OPERATION,
                source_type=SOURCE_TYPE,
                source_id=SOURCE_ID,
                payload={
                    "operation": "repository.register" if created else "repository.update",
                    "status": "ok",
                    "repository_id": str(row.id),
                    "repository": name,
                    "url": strip_userinfo(url),
                    "default_branch": default_branch,
                },
            )
            await session.commit()
            return RepositoryInfo.from_row(row)

    async def get(self, repo_id: uuid.UUID) -> RepositoryInfo:
        async with self._sessionmaker() as session:
            row = await session.get(Repository, repo_id)
            if row is None:
                raise NotFoundError(f"repository {repo_id} not found", details={"repository_id": str(repo_id)})
            return RepositoryInfo.from_row(row)

    async def get_by_name(self, name: str) -> RepositoryInfo:
        async with self._sessionmaker() as session:
            row = (await session.execute(select(Repository).where(Repository.name == name))).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"repository '{name}' not found", details={"name": name})
            return RepositoryInfo.from_row(row)

    async def resolve(self, ref: RepoRef) -> RepositoryInfo:
        """Accept a snapshot, an id or a name; always re-read the row (the DB is the source of truth)."""
        if isinstance(ref, RepositoryInfo):
            return await self.get(ref.id)
        if isinstance(ref, uuid.UUID):
            return await self.get(ref)
        return await self.get_by_name(ref)

    async def list_repositories(self) -> list[RepositoryInfo]:
        async with self._sessionmaker() as session:
            rows = (await session.execute(select(Repository).order_by(Repository.name))).scalars()
            return [RepositoryInfo.from_row(r) for r in rows]

    async def set_protected_branches(self, ref: RepoRef, patterns: Sequence[str], *, source: str = "manual") -> RepositoryInfo:
        repo = await self.resolve(ref)
        cleaned = _clean_patterns(patterns)
        async with self._sessionmaker() as session:
            row = await session.get(Repository, repo.id)
            if row is None:
                raise NotFoundError(f"repository {repo.id} not found")
            row.protected_branches = cleaned
            await append_event(
                session,
                EventType.GIT_OPERATION,
                source_type=SOURCE_TYPE,
                source_id=SOURCE_ID,
                payload={
                    "operation": "repository.protected_branches",
                    "status": "ok",
                    "repository_id": str(row.id),
                    "patterns": cleaned,
                    "source": source,
                },
            )
            await session.commit()
            return RepositoryInfo.from_row(row)

    async def sync_protected_branches(self, ref: RepoRef, gitlab: GitLabClient) -> RepositoryInfo:
        """Merge GitLab's protected-branch rules into the repository row (local policy is never weakened)."""
        repo = await self.resolve(ref)
        project = repo.gitlab_project_id or project_path_from_url(repo.url)
        if not project:
            raise ValidationFailed("repository has no GitLab project id", details={"repository": repo.name})
        remote = await gitlab.list_protected_branches(project)
        merged = list(dict.fromkeys([*repo.protected_branches, *remote]))
        return await self.set_protected_branches(repo.id, merged, source="gitlab")
