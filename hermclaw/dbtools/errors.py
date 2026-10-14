"""Stable error codes of the database tools (Bauplan §30, P27)."""

from __future__ import annotations

from typing import Any

from hermclaw.core.errors import ConfigError, HermclawError, PolicyViolation

DB_TARGET_UNKNOWN = "DB_TARGET_UNKNOWN"
DB_TARGET_INVALID = "DB_TARGET_INVALID"
DB_TARGET_UNSUPPORTED = "DB_TARGET_UNSUPPORTED"
DB_CONNECT_FAILED = "DB_CONNECT_FAILED"
DB_POLICY_DENIED = "DB_POLICY_DENIED"
DB_CAPABILITY_DENIED = "DB_CAPABILITY_DENIED"
DB_QUERY_REJECTED = "DB_QUERY_REJECTED"
DB_READ_ONLY_VIOLATION = "DB_READ_ONLY_VIOLATION"
DB_QUERY_TIMEOUT = "DB_QUERY_TIMEOUT"
DB_QUERY_FAILED = "DB_QUERY_FAILED"
DB_INTROSPECT_FAILED = "DB_INTROSPECT_FAILED"
DB_PATH_INVALID = "DB_PATH_INVALID"
DB_MIGRATION_FRAMEWORK_UNKNOWN = "DB_MIGRATION_FRAMEWORK_UNKNOWN"
DB_MIGRATION_INVALID = "DB_MIGRATION_INVALID"
DB_MIGRATION_UNSUPPORTED = "DB_MIGRATION_UNSUPPORTED"
DB_MIGRATION_FAILED = "DB_MIGRATION_FAILED"
DB_SANDBOX_UNAVAILABLE = "DB_SANDBOX_UNAVAILABLE"
DB_SANDBOX_FAILED = "DB_SANDBOX_FAILED"
DB_BACKUP_FAILED = "DB_BACKUP_FAILED"
DB_ARTIFACT_UNKNOWN = "DB_ARTIFACT_UNKNOWN"
DB_ARTIFACT_INVALID = "DB_ARTIFACT_INVALID"
DB_RESTORE_FAILED = "DB_RESTORE_FAILED"
DB_RESTORE_MISMATCH = "DB_RESTORE_MISMATCH"
DB_COMMAND_FAILED = "DB_COMMAND_FAILED"
DB_COMMAND_TIMEOUT = "DB_COMMAND_TIMEOUT"
DB_TOOL_ERROR = "DB_TOOL_ERROR"


class DbToolError(HermclawError):
    """A database tool could not do its work (connection, query, command, verification)."""

    code = DB_TOOL_ERROR

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=code, details=details)


class DbPolicyError(PolicyViolation):
    """The database policy refused the call – nothing was executed."""

    code = DB_POLICY_DENIED


class DbConfigError(ConfigError):
    code = DB_TARGET_INVALID


__all__ = [name for name in dir() if name.startswith("DB_")] + ["DbConfigError", "DbPolicyError", "DbToolError"]
