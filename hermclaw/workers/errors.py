"""Typed worker errors with stable machine-readable codes (P07).

All errors derive from :class:`hermclaw.core.errors.WorkerError` (``WORKER_ERROR``) or
:class:`hermclaw.core.errors.AuthError` so API handlers and the scheduler can treat them uniformly.
"""

from __future__ import annotations

from typing import Any

from hermclaw.core.errors import AuthError, WorkerError


class WorkerAuthError(AuthError):
    """A signed worker request was rejected (bad/missing signature, skew, replay, unknown worker)."""

    code = "WORKER_AUTH_FAILED"


class WorkerUnreachable(WorkerError):
    """The worker API could not be reached (connection refused, DNS, network down)."""

    code = "WORKER_UNREACHABLE"
    http_status = 503


class WorkerTimeout(WorkerError):
    """The worker API did not answer in time."""

    code = "WORKER_TIMEOUT"
    http_status = 504


class WorkerAuthFailed(WorkerError):
    """The worker rejected our credentials (HTTP 401/403)."""

    code = "WORKER_AUTH_FAILED"
    http_status = 502


class WorkerProtocolError(WorkerError):
    """The worker answered with something that does not match the protocol schema."""

    code = "WORKER_PROTOCOL_ERROR"


class WorkerRemoteError(WorkerError):
    """The worker answered with an error status. ``remote_code`` is the worker's own error code."""

    code = "WORKER_REMOTE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        remote_code: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        merged = {"status_code": status_code, "remote_code": remote_code, **(details or {})}
        super().__init__(message, code=code, details=merged)
        self.status_code = status_code
        self.remote_code = remote_code


class WorkerBusy(WorkerRemoteError):
    """The worker has no free execution slot (HTTP 503 ``WORKER_BUSY``)."""

    code = "WORKER_BUSY"


class WorkerNotFound(WorkerError):
    """No worker with this id is registered."""

    code = "WORKER_NOT_FOUND"
    http_status = 404
