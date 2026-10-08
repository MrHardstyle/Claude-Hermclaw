"""Runtime-controlled Git engine (Bauplan §27, P06).

Public entry points:

* :class:`GitEngine` – every git write (mirror, workspace, stage, commit, push, base update, cleanup)
* :class:`RepositoryRegistry` – ``repositories`` rows and protected-branch policy
* :class:`WorkspaceGitReader` – read-only git tools for models (``hermclaw.core.interfaces.GitReader``)
* :class:`GitLabClient` – optional merge requests / protected-branch sync (GitLab REST v4)
"""

from hermclaw.gitops.engine import DiffLimits, EngineTimeouts, GitEngine, build_commit_message, ssh_options_from_config
from hermclaw.gitops.gitlab import GitLabClient
from hermclaw.gitops.reader import WorkspaceGitReader
from hermclaw.gitops.registry import RepositoryRegistry
from hermclaw.gitops.runner import GitRunner, GitSshOptions
from hermclaw.gitops.types import (
    BaseStatus,
    CommitResult,
    DiffResult,
    FileChange,
    MergeRequestInfo,
    PushResult,
    RecoveryResult,
    RefusedPath,
    RepositoryInfo,
    StagedPath,
    StageResult,
    StatusEntry,
    UpdateResult,
    WorkspaceInfo,
    WorkspaceStatus,
)

__all__ = [
    "BaseStatus",
    "CommitResult",
    "DiffLimits",
    "DiffResult",
    "EngineTimeouts",
    "FileChange",
    "GitEngine",
    "GitLabClient",
    "GitRunner",
    "GitSshOptions",
    "MergeRequestInfo",
    "PushResult",
    "RecoveryResult",
    "RefusedPath",
    "RepositoryInfo",
    "RepositoryRegistry",
    "StageResult",
    "StagedPath",
    "StatusEntry",
    "UpdateResult",
    "WorkspaceGitReader",
    "WorkspaceInfo",
    "WorkspaceStatus",
    "build_commit_message",
    "ssh_options_from_config",
]
