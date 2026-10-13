"""Tool protocol between the runtime and LLM workers (Bauplan §19, P17)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from hermclaw.contracts.common import Contract


class ToolName(StrEnum):
    list_files = "list_files"
    read_file = "read_file"
    read_range = "read_range"
    find_text = "find_text"
    search_repo = "search_repo"
    search_symbol = "search_symbol"
    git_status = "git_status"
    git_diff = "git_diff"
    write_file = "write_file"
    replace_text = "replace_text"
    apply_patch = "apply_patch"
    run_command = "run_command"
    run_test = "run_test"
    request_scope_expansion = "request_scope_expansion"
    request_research = "request_research"
    request_replan = "request_replan"
    checkpoint = "checkpoint"
    complete_step = "complete_step"
    block_step = "block_step"
    # runtime operations tools (Bauplan §29–31): offered only to steps of the matching capability and only when the
    # runtime provides them; they act on registered hosts/databases/containers, never via the coder shell
    ssh_read = "ssh_read"
    ssh_command = "ssh_command"
    ssh_upload = "ssh_upload"
    ssh_service_status = "ssh_service_status"
    db_introspect = "db_introspect"
    db_query_readonly = "db_query_readonly"
    db_migration_generate = "db_migration_generate"
    db_migration_apply_sandbox = "db_migration_apply_sandbox"
    db_backup = "db_backup"
    db_restore_test = "db_restore_test"
    container_list = "container_list"
    container_inspect = "container_inspect"
    compose_validate = "compose_validate"
    compose_up_sandbox = "compose_up_sandbox"
    nginx_test = "nginx_test"
    proxy_config_validate = "proxy_config_validate"


MUTATING_TOOLS = frozenset({ToolName.write_file, ToolName.replace_text, ToolName.apply_patch})
TERMINAL_TOOLS = frozenset({ToolName.complete_step, ToolName.block_step, ToolName.request_replan})

SSH_TOOLS = frozenset({ToolName.ssh_read, ToolName.ssh_command, ToolName.ssh_upload, ToolName.ssh_service_status})
DB_TOOLS = frozenset(
    {
        ToolName.db_introspect,
        ToolName.db_query_readonly,
        ToolName.db_migration_generate,
        ToolName.db_migration_apply_sandbox,
        ToolName.db_backup,
        ToolName.db_restore_test,
    }
)
CONTAINER_TOOLS = frozenset(
    {
        ToolName.container_list,
        ToolName.container_inspect,
        ToolName.compose_validate,
        ToolName.compose_up_sandbox,
        ToolName.nginx_test,
        ToolName.proxy_config_validate,
    }
)
OPS_TOOLS = SSH_TOOLS | DB_TOOLS | CONTAINER_TOOLS
# which step capabilities may use which operations tools (Bauplan §26.8: SSH only for ssh/deploy)
OPS_TOOLS_BY_CAPABILITY: dict[str, frozenset[ToolName]] = {
    "ssh": SSH_TOOLS,
    "deploy": SSH_TOOLS | CONTAINER_TOOLS,
    "database": DB_TOOLS,
    "docker": CONTAINER_TOOLS,
}
# operations tools that change a remote system (approval policy of the provider applies)
REMOTE_MUTATING_TOOLS = frozenset({ToolName.ssh_command, ToolName.ssh_upload})


class CoderAction(Contract):
    """Exactly one action per turn. ``status`` is a short, user-visible progress note (no reasoning)."""

    tool: ToolName
    args: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="", max_length=300)
    decision: str = Field(default="", max_length=120, description="short decision label, e.g. 'fix-import'")


class ToolCall(Contract):
    turn: int = Field(ge=0)
    tool: ToolName
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResult(Contract):
    tool: ToolName
    ok: bool
    output: str = ""
    error_code: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    mutated_paths: list[str] = Field(default_factory=list)
    terminal: bool = False


class CompletionReport(Contract):
    summary: str = Field(min_length=3, max_length=4000)
    changed_files: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4000)


class BlockReport(Contract):
    reason_code: Literal["scope_unavailable", "missing_dependency", "requirement_unclear", "external_failure", "test_conflict", "other"]
    message: str = Field(min_length=3, max_length=4000)
