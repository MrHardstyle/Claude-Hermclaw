"""Immutable result/snapshot models returned by the Git engine (safe to pass across components and the API)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.persistence.models import Repository, Workspace

WorkspaceStatusName = Literal["active", "committed", "pushed", "archived", "cleaned"]
ACTIVE_WORKSPACE_STATES: frozenset[str] = frozenset({"active", "committed", "pushed"})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RepositoryInfo(_Frozen):
    id: uuid.UUID
    name: str
    url: str
    default_branch: str
    provider: str
    gitlab_project_id: str | None = None
    protected_branches: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Repository) -> RepositoryInfo:
        return cls(
            id=row.id,
            name=row.name,
            url=row.url,
            default_branch=row.default_branch,
            provider=row.provider,
            gitlab_project_id=row.gitlab_project_id,
            protected_branches=list(row.protected_branches or []),
            metadata=dict(row.metadata_ or {}),
        )


class WorkspaceInfo(_Frozen):
    id: uuid.UUID
    job_id: uuid.UUID
    repository_id: uuid.UUID | None
    path: str
    branch: str
    base_branch: str
    base_sha: str
    head_sha: str | None
    status: str
    repository_name: str | None = None

    @classmethod
    def from_row(cls, row: Workspace, *, repository_name: str | None = None) -> WorkspaceInfo:
        return cls(
            id=row.id,
            job_id=row.job_id,
            repository_id=row.repository_id,
            path=row.path,
            branch=row.branch,
            base_branch=row.base_branch,
            base_sha=row.base_sha,
            head_sha=row.head_sha,
            status=row.status,
            repository_name=repository_name,
        )

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_WORKSPACE_STATES

    def to_handle(self) -> WorkspaceHandle:
        """The shared wave-2 handle (``hermclaw.core.interfaces.WorkspaceHandle``) for tools/executors."""
        key = self.repository_name or (str(self.repository_id) if self.repository_id else Path(self.path).name)
        return WorkspaceHandle(
            id=self.id,
            job_id=self.job_id,
            path=Path(self.path),
            branch=self.branch,
            base_branch=self.base_branch,
            base_sha=self.base_sha,
            repository_key=key,
        )


ChangeOperation = Literal["create", "modify", "delete"]


class StatusEntry(_Frozen):
    path: str
    index: str = Field(description="porcelain X column")
    worktree: str = Field(description="porcelain Y column")
    orig_path: str | None = None

    @property
    def untracked(self) -> bool:
        return self.index == "?" and self.worktree == "?"

    @property
    def conflicted(self) -> bool:
        return (self.index + self.worktree) in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

    @property
    def operation(self) -> ChangeOperation:
        if self.untracked or self.index in ("A", "C"):
            return "create"
        if "D" in (self.index, self.worktree):
            return "delete"
        return "modify"


class WorkspaceStatus(_Frozen):
    branch: str | None
    head_sha: str
    base_sha: str
    entries: list[StatusEntry]
    commits_ahead_of_base: int

    @property
    def clean(self) -> bool:
        return not self.entries

    @property
    def tracked_dirty(self) -> bool:
        return any(not e.untracked for e in self.entries)

    @property
    def conflicted(self) -> list[str]:
        return [e.path for e in self.entries if e.conflicted]

    @property
    def untracked(self) -> list[str]:
        return [e.path for e in self.entries if e.untracked]


class FileChange(_Frozen):
    path: str
    status: str = Field(description="A added, M modified, D deleted, R renamed, C copied, T type change")
    old_path: str | None = None
    additions: int | None = None
    deletions: int | None = None
    binary: bool = False


class DiffResult(_Frozen):
    base: str
    files: list[FileChange]
    files_truncated: bool = False
    patch: str = ""
    patch_truncated: bool = False
    patch_bytes: int = 0
    total_additions: int = 0
    total_deletions: int = 0

    @property
    def changed_paths(self) -> list[str]:
        out: list[str] = []
        for f in self.files:
            for p in (f.old_path, f.path):
                if p and p not in out:
                    out.append(p)
        return out


class StagedPath(_Frozen):
    path: str
    operation: ChangeOperation
    matched: str | None = None


class RefusedPath(_Frozen):
    path: str
    operation: ChangeOperation
    reason: str
    matched: str | None = None
    detail: str | None = None


class StageResult(_Frozen):
    staged: list[StagedPath]
    refused: list[RefusedPath]

    @property
    def staged_paths(self) -> list[str]:
        return [s.path for s in self.staged]

    @property
    def refused_paths(self) -> list[str]:
        return [r.path for r in self.refused]


class CommitResult(_Frozen):
    sha: str
    parent_sha: str
    branch: str
    files: list[str]
    verification_run_id: uuid.UUID
    git_operation_id: uuid.UUID
    message: str


class MergeRequestInfo(_Frozen):
    id: int
    iid: int
    web_url: str | None = None
    state: str | None = None
    title: str | None = None
    source_branch: str
    target_branch: str
    created: bool = Field(default=True, description="False when an already open MR was reused (duplicate detection)")


class PushResult(_Frozen):
    branch: str
    sha: str
    remote_sha_before: str | None
    forced: bool
    up_to_date: bool
    git_operation_id: uuid.UUID
    merge_request: MergeRequestInfo | None = None
    merge_request_error: str | None = None


class BaseStatus(_Frozen):
    base_branch: str
    base_sha: str
    remote_sha: str
    stale: bool
    rewritten: bool = Field(default=False, description="remote base no longer contains base_sha (force-push)")
    commits_behind: int = 0
    commits_ahead: int = 0


class UpdateResult(_Frozen):
    strategy: Literal["rebase", "merge"]
    updated: bool
    old_base_sha: str
    new_base_sha: str
    old_head_sha: str
    new_head_sha: str
    stashed: bool = Field(default=False, description="uncommitted changes were stashed and re-applied")
    git_operation_id: uuid.UUID | None = None


class RecoveryResult(_Frozen):
    actions: list[str]
    head_sha: str
    clean: bool
