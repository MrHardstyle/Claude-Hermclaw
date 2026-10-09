"""Tool-level errors with stable codes (P17).

A :class:`ToolError` never escapes :meth:`hermclaw.tools.engine.ToolEngine.execute`; the engine converts it into a
``ToolResult(ok=False, error_code=...)`` so the coder model gets a precise, machine-readable refusal.
"""

from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """A refused or failed tool call. ``code`` is stable (see docs/architecture/tools.md)."""

    def __init__(self, code: str, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


class ToolProtocolError(ToolError):
    """The model emitted something that is not a valid tool action (unknown tool, malformed action)."""


# stable error codes ------------------------------------------------------------------------------------------
ARGS_INVALID = "ARGS_INVALID"
UNKNOWN_TOOL = "UNKNOWN_TOOL"
ACTION_INVALID = "ACTION_INVALID"
TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
STEP_FINISHED = "STEP_FINISHED"
TURN_BUDGET_EXHAUSTED = "TURN_BUDGET_EXHAUSTED"
SCOPE_MISSING = "SCOPE_MISSING"
SCOPE_VIOLATION = "SCOPE_VIOLATION"
PATH_INVALID = "PATH_INVALID"
PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
PATH_FORBIDDEN = "PATH_FORBIDDEN"
NOT_FOUND = "NOT_FOUND"
NOT_A_FILE = "NOT_A_FILE"
NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
SYMLINK_REFUSED = "SYMLINK_REFUSED"
BINARY_FILE = "BINARY_FILE"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
RANGE_INVALID = "RANGE_INVALID"
PATTERN_INVALID = "PATTERN_INVALID"
TEXT_NOT_FOUND = "TEXT_NOT_FOUND"
TEXT_COUNT_MISMATCH = "TEXT_COUNT_MISMATCH"
NO_CHANGE = "NO_CHANGE"
REDACTED_PLACEHOLDER = "REDACTED_PLACEHOLDER"
PATCH_INVALID = "PATCH_INVALID"
PATCH_FAILED = "PATCH_FAILED"
PATCH_SYMLINK_REFUSED = "PATCH_SYMLINK_REFUSED"
COMMAND_FORBIDDEN = "COMMAND_FORBIDDEN"
COMMAND_DESTRUCTIVE = "COMMAND_DESTRUCTIVE"
COMMAND_FAILED = "COMMAND_FAILED"
COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
COMMAND_SCOPE_VIOLATION = "COMMAND_SCOPE_VIOLATION"
SANDBOX_ERROR = "SANDBOX_ERROR"
TESTS_FAILED = "TESTS_FAILED"
TEST_ERROR = "TEST_ERROR"
GIT_UNAVAILABLE = "GIT_UNAVAILABLE"
REPO_UNAVAILABLE = "REPO_UNAVAILABLE"
RESEARCH_FAILED = "RESEARCH_FAILED"
SCOPE_EXPANSION_DENIED = "SCOPE_EXPANSION_DENIED"
SCOPE_EXPANSION_FAILED = "SCOPE_EXPANSION_FAILED"
REPLAN_FAILED = "REPLAN_FAILED"
STEP_REQUIRED = "STEP_REQUIRED"
STEP_NOT_FOUND = "STEP_NOT_FOUND"
TOOL_INTERNAL_ERROR = "TOOL_INTERNAL_ERROR"
