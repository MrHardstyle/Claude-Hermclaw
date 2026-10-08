"""Error model with stable machine-readable codes (Bauplan §2.5 / P02)."""

from __future__ import annotations

from typing import Any


class HermclawError(Exception):
    """Base error. ``code`` is stable and appears in events and API responses."""

    code = "HERMCLAW_ERROR"
    http_status = 500

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigError(HermclawError):
    code = "CONFIG_INVALID"


class NotFoundError(HermclawError):
    code = "NOT_FOUND"
    http_status = 404


class ConflictError(HermclawError):
    code = "CONFLICT"
    http_status = 409


class ValidationFailed(HermclawError):
    code = "VALIDATION_FAILED"
    http_status = 422


class InvalidTransition(HermclawError):
    code = "INVALID_TRANSITION"
    http_status = 409


class AuthError(HermclawError):
    code = "UNAUTHORIZED"
    http_status = 401


class PolicyViolation(HermclawError):
    code = "POLICY_VIOLATION"
    http_status = 403


class ScopeViolation(PolicyViolation):
    code = "SCOPE_VIOLATION"


class ResourceUnavailable(HermclawError):
    code = "RESOURCE_UNAVAILABLE"
    http_status = 503


class ExternalServiceError(HermclawError):
    code = "EXTERNAL_SERVICE_ERROR"
    http_status = 502


class ModelError(ExternalServiceError):
    code = "MODEL_ERROR"


class ModelTimeout(ModelError):
    code = "MODEL_TIMEOUT"


class ModelOutputInvalid(ModelError):
    code = "MODEL_OUTPUT_INVALID"


class WorkerError(ExternalServiceError):
    code = "WORKER_ERROR"


class GitError(HermclawError):
    code = "GIT_ERROR"


class StaleBaseError(GitError):
    code = "STALE_BASE_SHA"


class MergeConflictError(GitError):
    code = "MERGE_CONFLICT"


class ProtectedBranchError(GitError):
    code = "PROTECTED_BRANCH"
    http_status = 403
