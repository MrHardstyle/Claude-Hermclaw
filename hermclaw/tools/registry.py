"""Tool registry: one :class:`ToolSpec` per :class:`~hermclaw.contracts.tools.ToolName` (P17).

Every tool has a strict Pydantic argument model (unknown keys are refused so a model cannot smuggle options),
a prompt description and flags (``mutating`` / ``terminal`` mirror ``MUTATING_TOOLS`` / ``TERMINAL_TOOLS``).
:func:`tool_schemas` renders the JSON schemas for prompts; :func:`validate_args` turns validation errors into a
compact, value-free message (``ARGS_INVALID``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hermclaw.contracts.scope import ScopeExpansionRequest
from hermclaw.contracts.tools import MUTATING_TOOLS, TERMINAL_TOOLS, BlockReport, CoderAction, CompletionReport, ToolName
from hermclaw.tools import errors as E
from hermclaw.tools.errors import ToolError, ToolProtocolError
from hermclaw.tools.testparse import FRAMEWORKS, Framework

MAX_WRITE_CHARS = 1_000_000
MAX_PATCH_CHARS = 1_000_000


class ToolArgs(BaseModel):
    """Strict base for tool arguments. Whitespace is significant (file content), so nothing is stripped."""

    model_config = ConfigDict(extra="forbid")


class NoArgs(ToolArgs):
    pass


class ListFilesArgs(ToolArgs):
    path: str = Field(default=".", max_length=1000, description="directory relative to the repository root")
    recursive: bool = Field(default=True, description="false lists only the direct children (directories end with '/')")
    pattern: str | None = Field(default=None, max_length=200, description="glob filter, e.g. '*.py' or 'src/**/*.ts'")
    max_entries: int = Field(default=500, ge=1, le=5000)


class ReadFileArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=1000)


class ReadRangeArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=1000)
    start: int = Field(ge=1, description="first line (1-based)")
    end: int = Field(ge=1, description="last line (inclusive)")

    @model_validator(mode="after")
    def _order(self) -> ReadRangeArgs:
        if self.end < self.start:
            raise ValueError("end must be >= start")
        return self


class FindTextArgs(ToolArgs):
    pattern: str = Field(min_length=1, max_length=500)
    path: str = Field(default=".", max_length=1000)
    regex: bool = False
    ignore_case: bool = False
    glob: str | None = Field(default=None, max_length=200)
    max_results: int = Field(default=100, ge=1, le=1000)


class SearchRepoArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=500)
    k: int = Field(default=10, ge=1, le=50)


class RecallArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=500, description="what you need to know: the error, tool, format or task")
    k: int = Field(default=5, ge=1, le=10)


class SearchSymbolArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=200)
    k: int = Field(default=10, ge=1, le=50)


class GitDiffArgs(ToolArgs):
    paths: list[str] | None = Field(default=None, max_length=100)


class WriteFileArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=MAX_WRITE_CHARS, description="complete new file content, or the part to append")
    append: bool = Field(default=False, description="add content to the end of the file (created if missing): write large files in parts")


class ReplaceTextArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=1000)
    old: str = Field(min_length=1, max_length=200_000, description="exact text to replace (whitespace significant)")
    new: str = Field(max_length=MAX_WRITE_CHARS)
    count: int = Field(default=1, ge=1, le=1000, description="exact number of occurrences expected and replaced")


class ApplyPatchArgs(ToolArgs):
    patch: str = Field(min_length=1, max_length=MAX_PATCH_CHARS, description="unified diff ('--- a/x' '+++ b/x' '@@ ... @@')")


_TIMEOUT_DOC = "the default is a few minutes; set it higher only for a test suite known to run longer"


class RunCommandArgs(ToolArgs):
    command: str = Field(min_length=1, max_length=8000)
    cwd: str = Field(default=".", max_length=1000, description="working directory relative to the repository root")
    timeout_seconds: int | None = Field(default=None, ge=1, le=7200, description=_TIMEOUT_DOC)


class RunTestArgs(ToolArgs):
    command: str = Field(min_length=1, max_length=8000, description="test command, e.g. 'pytest -q tests/test_x.py'")
    cwd: str = Field(default=".", max_length=1000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=7200, description=_TIMEOUT_DOC)
    framework: Framework | None = Field(default=None, description="output parser; auto-detected when omitted")

    @field_validator("framework", mode="before")
    @classmethod
    def _unknown_framework_is_detected(cls, value: Any) -> Any:
        """A language or tool name ("powershell", "npm") is no refusal: the parser is then detected from the command and
        its output (BUG-327 – ladder A9#4 lost two turns to framework="powershell")."""
        if isinstance(value, str):
            name = value.strip().lower()
            return name if name in FRAMEWORKS else None
        return value


class InstallDependenciesArgs(ToolArgs):
    ecosystem: Literal["python", "node"] = Field(description="python installs a requirements file with pip, node a package.json with npm")
    manifest: str | None = Field(
        default=None,
        max_length=1000,
        description="dependency file relative to the repository root (default requirements.txt / package.json; node: root only)",
    )


class RequestResearchArgs(ToolArgs):
    question: str = Field(min_length=5, max_length=2000)


class RequestReplanArgs(ToolArgs):
    reason: str = Field(min_length=10, max_length=4000)


class CheckpointArgs(ToolArgs):
    notes: str = Field(min_length=1, max_length=4000, description="what is done and what remains (no reasoning transcript)")
    progress: int | None = Field(default=None, ge=0, le=100, description="percent complete")
    next_actions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _short_actions(self) -> CheckpointArgs:
        if any(len(a) > 300 for a in self.next_actions):
            raise ValueError("each next_action must be at most 300 characters")
        return self


# ------------------------------------------------------------------------- operations tools (Bauplan §29–31)
_HOST = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", description="registered host id, e.g. webui-223")
_TARGET = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", description="registered database target name")
_RELPATH = Field(min_length=1, max_length=1000, description="path relative to the repository root")


class SshReadArgs(ToolArgs):
    host: str = _HOST
    command: str = Field(min_length=1, max_length=4000, description="read-only command (classified by the SSH policy)")
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)


class SshCommandArgs(ToolArgs):
    host: str = _HOST
    command: str = Field(min_length=1, max_length=4000)
    approval_token: str | None = Field(default=None, max_length=200, description="approval token from a previous refusal")
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)


class SshUploadArgs(ToolArgs):
    host: str = _HOST
    path: str = _RELPATH
    remote_path: str = Field(min_length=1, max_length=1000, description="absolute target path on the host")
    mode: str = Field(default="0644", pattern=r"^0[0-7]{3}$")
    approval_token: str | None = Field(default=None, max_length=200)


class SshServiceStatusArgs(ToolArgs):
    host: str = _HOST
    unit: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9@._:-]+$")


class DbTargetArgs(ToolArgs):
    target: str = _TARGET


class DbQueryArgs(ToolArgs):
    target: str = _TARGET
    sql: str = Field(min_length=1, max_length=20_000, description="a single read-only statement")
    params: dict[str, Any] | list[Any] | None = None
    row_limit: int = Field(default=100, ge=1, le=5000)
    timeout_seconds: int = Field(default=30, ge=1, le=600)


class DbMigrationGenerateArgs(ToolArgs):
    description: str = Field(min_length=3, max_length=200)
    path: str = Field(default=".", max_length=1000, description="project directory relative to the repository root")


class DbMigrationApplyArgs(ToolArgs):
    path: str = Field(default=".", max_length=1000)
    target_kind: Literal["postgresql", "sqlite"] = "postgresql"
    seed_schema_from: str | None = Field(default=None, max_length=100, description="target whose schema seeds the sandbox DB")


class DbRestoreTestArgs(ToolArgs):
    artifact_id: str = Field(min_length=8, max_length=64, description="backup artifact id from db_backup")


class ContainerQueryArgs(ToolArgs):
    host: str | None = Field(default=None, max_length=100, description="registered host id; omitted = the execution sandbox host")
    engine: Literal["auto", "podman", "docker"] = "auto"
    all: bool = False


class ContainerInspectArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    host: str | None = Field(default=None, max_length=100)
    engine: Literal["auto", "podman", "docker"] = "auto"


class ComposeArgs(ToolArgs):
    path: str = _RELPATH
    timeout_seconds: int = Field(default=300, ge=10, le=3600)


class ConfigSourceArgs(ToolArgs):
    path: str | None = Field(default=None, max_length=1000, description="config file relative to the repository root")
    config: str | None = Field(default=None, max_length=200_000, description="config text (alternative to path)")

    @model_validator(mode="after")
    def _one(self) -> ConfigSourceArgs:
        if (self.path is None) == (self.config is None):
            raise ValueError("give exactly one of path or config")
        return self


ToolKind = Literal["read", "git", "write", "exec", "request", "control", "ops"]


@dataclass(frozen=True)
class ToolSpec:
    name: ToolName
    description: str
    args_model: type[BaseModel]
    kind: ToolKind
    mutating: bool
    terminal: bool

    def json_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def prompt_entry(self) -> dict[str, Any]:
        return {"name": self.name.value, "description": self.description, "parameters": self.json_schema()}


def _spec(name: ToolName, model: type[BaseModel], kind: ToolKind, description: str) -> ToolSpec:
    return ToolSpec(name, description, model, kind, name in MUTATING_TOOLS, name in TERMINAL_TOOLS)


_SPECS: list[ToolSpec] = [
    _spec(ToolName.list_files, ListFilesArgs, "read", "List repository files (tracked and untracked, not ignored) below a directory."),
    _spec(ToolName.read_file, ReadFileArgs, "read", "Read a whole UTF-8 text file (output is budgeted; use read_range for large files)."),
    _spec(ToolName.read_range, ReadRangeArgs, "read", "Read lines start..end (1-based, inclusive) of a text file."),
    _spec(
        ToolName.find_text,
        FindTextArgs,
        "read",
        "Search text (literal by default, optional regex) in repository files; returns path:line: text.",
    ),
    _spec(ToolName.search_repo, SearchRepoArgs, "read", "Semantic/lexical repository search via repository intelligence."),
    _spec(ToolName.search_symbol, SearchSymbolArgs, "read", "Find definitions of a symbol (function, class, ...) by name."),
    _spec(
        ToolName.recall,
        RecallArgs,
        "read",
        "Look up Hermclaw's knowledge: lessons from earlier jobs, research findings, known tool problems and conventions. "
        "Use it when a check keeps failing, a tool refuses an action, or before an unfamiliar tool or file format.",
    ),
    _spec(ToolName.git_status, NoArgs, "git", "Show changed/untracked files of the workspace (read-only)."),
    _spec(ToolName.git_diff, GitDiffArgs, "git", "Show the current diff of the workspace, optionally limited to paths (read-only)."),
    _spec(
        ToolName.write_file,
        WriteFileArgs,
        "write",
        "Create or overwrite a file with the complete content, or append a part to it (must be inside the scope).",
    ),
    _spec(
        ToolName.replace_text,
        ReplaceTextArgs,
        "write",
        "Replace exact text in a file; 'old' must occur exactly 'count' times (default 1). Preferred for small edits.",
    ),
    _spec(ToolName.apply_patch, ApplyPatchArgs, "write", "Apply a unified diff to the working tree (all files must be inside the scope)."),
    _spec(
        ToolName.run_command,
        RunCommandArgs,
        "exec",
        "Run a shell command in the sandbox, starting in the repository root (no git mutations, no sudo; out-of-scope file "
        "changes are reverted).",
    ),
    _spec(ToolName.run_test, RunTestArgs, "exec", "Run a test command in the sandbox and get parsed pass/fail counts."),
    _spec(
        ToolName.install_dependencies,
        InstallDependenciesArgs,
        "exec",
        "Install the dependencies declared in the manifest from the allow-listed package registries; later commands and "
        "tests of this job can import them. Declare every dependency in the manifest first.",
    ),
    _spec(
        ToolName.request_scope_expansion,
        ScopeExpansionRequest,
        "request",
        "Ask the runtime to allow additional paths/operations; give a concrete justification.",
    ),
    _spec(ToolName.request_research, RequestResearchArgs, "request", "Ask for web research on a precise technical question."),
    _spec(ToolName.request_replan, RequestReplanArgs, "control", "Stop this step and ask the planner to replan (terminal)."),
    _spec(ToolName.checkpoint, CheckpointArgs, "control", "Persist progress notes so the step can resume later."),
    _spec(ToolName.complete_step, CompletionReport, "control", "Finish the step with a summary, changed files and tests run (terminal)."),
    _spec(ToolName.block_step, BlockReport, "control", "Stop the step because it cannot be completed (terminal)."),
    # operations tools – only listed for steps of the matching capability when the runtime provides them
    _spec(ToolName.ssh_read, SshReadArgs, "ops", "Run a read-only command on a registered host via the SSH policy (audited)."),
    _spec(
        ToolName.ssh_command,
        SshCommandArgs,
        "ops",
        "Run a mutating command on a registered host; mutations may need approval (a refusal returns the approval token).",
    ),
    _spec(ToolName.ssh_upload, SshUploadArgs, "ops", "Upload a workspace file to a registered host (audited, approval policy applies)."),
    _spec(ToolName.ssh_service_status, SshServiceStatusArgs, "ops", "Show systemd status and active state of a unit on a host."),
    _spec(ToolName.db_introspect, DbTargetArgs, "ops", "Describe the schema of a registered database target (tables, columns, keys)."),
    _spec(ToolName.db_query_readonly, DbQueryArgs, "ops", "Run one read-only SQL statement (enforced read-only, row limit, timeout)."),
    _spec(
        ToolName.db_migration_generate,
        DbMigrationGenerateArgs,
        "ops",
        "Create the next migration skeleton of the project's framework (alembic/django/raw SQL) in the workspace.",
    ),
    _spec(
        ToolName.db_migration_apply_sandbox,
        DbMigrationApplyArgs,
        "ops",
        "Apply the project's migrations to a throw-away sandbox database (upgrade, downgrade, upgrade) and report.",
    ),
    _spec(ToolName.db_backup, DbTargetArgs, "ops", "Back up a registered database target into the artifact store (verified checksum)."),
    _spec(
        ToolName.db_restore_test,
        DbRestoreTestArgs,
        "ops",
        "Restore a backup artifact into a sandbox database and verify tables/row counts.",
    ),
    _spec(ToolName.container_list, ContainerQueryArgs, "ops", "List containers (podman/docker) on the sandbox host or a registered host."),
    _spec(ToolName.container_inspect, ContainerInspectArgs, "ops", "Inspect one container (image, state, ports, labels, health)."),
    _spec(ToolName.compose_validate, ComposeArgs, "ops", "Validate a compose file (engine check + structural/security rules)."),
    _spec(
        ToolName.compose_up_sandbox,
        ComposeArgs,
        "ops",
        "Start a compose project in a sandbox (remapped ports, no host network), collect logs, always tear down.",
    ),
    _spec(ToolName.nginx_test, ConfigSourceArgs, "ops", "Test an nginx configuration with nginx -t in a temporary prefix."),
    _spec(
        ToolName.proxy_config_validate,
        ConfigSourceArgs,
        "ops",
        "Check a reverse-proxy config (SSE locations unbuffered, duplicate server/listen, upstream allow-list, TLS sanity).",
    ),
]

TOOL_SPECS: dict[ToolName, ToolSpec] = {s.name: s for s in _SPECS}


def get_spec(name: ToolName | str) -> ToolSpec:
    try:
        return TOOL_SPECS[ToolName(name)]
    except (ValueError, KeyError) as exc:
        raise ToolProtocolError(
            E.UNKNOWN_TOOL, f"unknown tool '{str(name)[:64]}'; valid tools: {', '.join(t.value for t in ToolName)}"
        ) from exc


def tool_schemas(allowed: Iterable[ToolName] | None = None) -> list[dict[str, Any]]:
    names = set(allowed) if allowed is not None else set(TOOL_SPECS)
    return [s.prompt_entry() for s in _SPECS if s.name in names]


def render_tool_catalog(allowed: Iterable[ToolName] | None = None) -> str:
    """Compact text catalogue for prompts: one line per tool with its parameters."""
    lines = []
    for entry in tool_schemas(allowed):
        params = entry["parameters"].get("properties", {})
        required = set(entry["parameters"].get("required", []))
        sig = ", ".join(f"{k}{'' if k in required else '?'}" for k in params)
        lines.append(f"- {entry['name']}({sig}): {entry['description']}")
    return "\n".join(lines)


def format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(x) for x in err.get("loc", ())) or "args"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts[:10])


def validate_args(spec: ToolSpec, args: Mapping[str, Any]) -> BaseModel:
    try:
        return spec.args_model.model_validate(dict(args))
    except ValidationError as exc:
        raise ToolError(
            E.ARGS_INVALID,
            f"invalid arguments for {spec.name.value}: {format_validation_error(exc)}",
            data={"schema": spec.json_schema()},
        ) from exc


def parse_action(payload: Mapping[str, Any] | str) -> CoderAction:
    """Parse a raw model action (dict or JSON text) into a :class:`CoderAction`.

    Raises :class:`ToolProtocolError` with ``UNKNOWN_TOOL`` (with the list of valid tools) or ``ACTION_INVALID``.
    """
    data: Any = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ToolProtocolError(E.ACTION_INVALID, f"action is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise ToolProtocolError(E.ACTION_INVALID, "action must be a JSON object with 'tool' and 'args'")
    tool = data.get("tool")
    if not isinstance(tool, str) or tool not in ToolName.__members__.values():
        get_spec(str(tool))  # raises UNKNOWN_TOOL with the valid names
    try:
        return CoderAction.model_validate(dict(data))
    except ValidationError as exc:
        raise ToolProtocolError(E.ACTION_INVALID, f"invalid action: {format_validation_error(exc)}") from exc
