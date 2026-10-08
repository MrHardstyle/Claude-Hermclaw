"""Git-engine specific errors (stable codes, see hermclaw.core.errors for the base hierarchy).

``StaleBaseError``, ``MergeConflictError`` and ``ProtectedBranchError`` live in :mod:`hermclaw.core.errors`
because the scheduler and API map them as well; they are re-exported here for convenience.
"""

from __future__ import annotations

from hermclaw.core.errors import (
    ConfigError,
    ExternalServiceError,
    GitError,
    MergeConflictError,
    NotFoundError,
    PolicyViolation,
    ProtectedBranchError,
    ScopeViolation,
    StaleBaseError,
)

__all__ = [
    "BaseBranchNotFound",
    "CommitNotVerifiedError",
    "GitCommandError",
    "GitError",
    "GitLabError",
    "GitLockTimeout",
    "GitTimeoutError",
    "InvalidRemoteUrl",
    "MergeConflictError",
    "NothingToCommitError",
    "NothingToPushError",
    "ProtectedBranchError",
    "PushRejectedError",
    "ScopeViolation",
    "SecretRefError",
    "StaleBaseError",
    "WorkspaceNotFound",
    "WorkspacePathViolation",
    "WorkspaceStateError",
]


class GitCommandError(GitError):
    """A git subprocess exited with an unexpected status. ``details`` carry redacted args/stderr."""

    code = "GIT_COMMAND_FAILED"


class GitTimeoutError(GitCommandError):
    code = "GIT_TIMEOUT"


class GitLockTimeout(GitError):
    code = "GIT_LOCK_TIMEOUT"
    http_status = 503


class BaseBranchNotFound(GitError):
    code = "BASE_BRANCH_NOT_FOUND"
    http_status = 404


class InvalidRemoteUrl(GitError):
    code = "INVALID_REMOTE_URL"
    http_status = 422


class WorkspaceNotFound(NotFoundError):
    code = "WORKSPACE_NOT_FOUND"


class WorkspaceStateError(GitError):
    """Workspace is missing on disk, cleaned, on the wrong branch or otherwise unusable."""

    code = "WORKSPACE_STATE"
    http_status = 409


class WorkspacePathViolation(PolicyViolation):
    """A workspace path points outside the configured workspace root (tampered row)."""

    code = "WORKSPACE_PATH_VIOLATION"


class CommitNotVerifiedError(GitError):
    """``commit_verified`` was called without a matching, passed verification run."""

    code = "COMMIT_NOT_VERIFIED"
    http_status = 409


class NothingToCommitError(GitError):
    code = "NOTHING_TO_COMMIT"
    http_status = 409


class NothingToPushError(GitError):
    code = "NOTHING_TO_PUSH"
    http_status = 409


class PushRejectedError(GitError):
    code = "PUSH_REJECTED"
    http_status = 409


class GitLabError(ExternalServiceError):
    code = "GITLAB_ERROR"


class SecretRefError(ConfigError):
    code = "SECRET_REF_INVALID"
