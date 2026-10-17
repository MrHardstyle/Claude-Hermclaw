"""ToolEngine – the only way an LLM touches a workspace (Bauplan §19 CODER TOOL LOOP, Phase 17).

One engine instance serves one step attempt. :meth:`ToolEngine.execute` runs exactly one :class:`CoderAction`:

1. policy pre-checks (17.13): step already finished, tool not allowed for the step, turn budget, argument validation;
2. the tool call is persisted (``tool_calls``, arguments redacted) and ``tool.call.started`` is emitted;
3. the handler runs – reads are confined to the workspace (no symlink escape, secrets hidden), writes are checked with
   the step's :class:`~hermclaw.scope.guard.ScopeGuard` and written atomically, commands are classified and executed
   through the :class:`~hermclaw.core.interfaces.CommandExecutor`, their file side effects audited against the scope
   and out-of-scope changes reverted;
4. the output is redacted and budgeted (``policies.coder.tool_output_chars``), the row is finished and
   ``tool.call.finished`` is emitted with the duration.

Calls are serialised per engine (one workspace, one attempt). Git mutations do not exist as tools; commits, branches
and pushes are runtime-controlled (gitops).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import shlex
import stat
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import Operation, ScopeExpansionRequest
from hermclaw.contracts.tools import OPS_TOOLS, TERMINAL_TOOLS, BlockReport, CoderAction, CompletionReport, ToolName, ToolResult
from hermclaw.contracts.worker import CommandResult
from hermclaw.core.config import PoliciesConfig
from hermclaw.core.interfaces import CommandExecutor, ExecutionRequest, GitReader, RepoContextProvider, RepoHit, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED, Redactor
from hermclaw.research.questions import unanswerable_reason
from hermclaw.scope.guard import ScopeGuard
from hermclaw.tools import errors as E
from hermclaw.tools.classify import CommandClassification, CommandClassifier, package_install
from hermclaw.tools.context import CallContext, NullCallbacks, ScopeExpansionOutcome, ToolCallbacks, ToolPermissions
from hermclaw.tools.errors import ToolError, ToolProtocolError
from hermclaw.tools.gitlocal import LocalGit
from hermclaw.tools.ops import OpsCall, OpsResult, OpsToolProvider
from hermclaw.tools.output import clip_head, clip_middle, missing_modules, strip_ansi
from hermclaw.tools.patch import diff_section_path, parse_numstat, parse_patch
from hermclaw.tools.recorder import PendingEvent, ToolRecorder
from hermclaw.tools.registry import (
    ApplyPatchArgs,
    CheckpointArgs,
    FindTextArgs,
    GitDiffArgs,
    InstallDependenciesArgs,
    ListFilesArgs,
    ReadFileArgs,
    ReadRangeArgs,
    ReplaceTextArgs,
    RequestReplanArgs,
    RequestResearchArgs,
    RunCommandArgs,
    RunTestArgs,
    SearchRepoArgs,
    SearchSymbolArgs,
    ToolSpec,
    WriteFileArgs,
    get_spec,
    parse_action,
    tool_schemas,
    validate_args,
)
from hermclaw.tools.snapshot import DEFAULT_GENERATED_GLOBS, AuditOutcome, Decide, WorkspaceTracker
from hermclaw.tools.testparse import TestSummary, summarise
from hermclaw.tools.workspace import (
    WorkspaceFS,
    canonical_path,
    filter_listing,
    immediate_children,
    is_binary,
    regex_is_risky,
    search_files,
)

log = get_logger(__name__)

#: install target of Python dependencies inside the sandbox (``worker.execution.sandbox.PYTHON_DEPS_DIR``)
TIMEOUT_HINT = (
    "Commands must exit on their own: a server, watcher or REPL never does. Do not start one in the foreground or "
    "in the background; a test starts the app in-process (port 0) and closes it, or calls the handler directly."
)
READ_REPEAT_WINDOW = 6  # turns; the context shows the files of the last 6 reads (context_builder ``_read_paths``)
IMAGE_CONSTRAINTS = "/tmp/hermclaw-image-constraints.txt"  # noqa: S108 - path inside the sandbox
PYTHON_DEPS_DIR = "/deps/python"


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def manifest_packages(ecosystem: str, text: str) -> set[str]:
    """Top-level package names declared in a requirements.txt or package.json (best effort, no resolution)."""
    names: set[str] = set()
    if ecosystem == "python":
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith(("-", "git+", "http://", "https://")):
                continue
            m = _REQ_NAME.match(line)
            if m:
                names.add(m.group(1).lower())
        return names
    try:
        doc = json.loads(text)
    except ValueError:
        return names
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        section = doc.get(key) if isinstance(doc, dict) else None
        if isinstance(section, dict):
            names.update(str(n).lower() for n in section)
    return names


def package_mentioned(package: str, job_text: str) -> bool:
    """Whether the job text names ``package`` (case-insensitive; '-', '_' and '.' interchangeable; a scope like
    '@nestjs/core' also matches 'nestjs')."""
    norm = re.sub(r"[-_.]+", "-", job_text.lower())
    candidates = {re.sub(r"[-_.]+", "-", package.lower())}
    if package.startswith("@") and "/" in package:
        candidates.add(package[1:].split("/", 1)[0].lower())
    return any(re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", norm) for c in candidates if c)


def python_install_command(manifest: str) -> str:
    """The Python install command of ``install_dependencies`` (D-014), run with registry-only egress and ``/deps`` writable.

    BUG-161: ``/deps/python`` comes first on ``PYTHONPATH``, so a package the image already provides (its test
    toolchain: pytest and its plugins) must stay at the image's version, or the toolchain breaks. The image's packages
    (listed without ``PYTHONPATH``) are pip constraints; every install starts from an empty directory, so a removed or
    changed requirement does not linger."""
    return (
        f"find {PYTHON_DEPS_DIR} -mindepth 1 -delete"
        f" && PYTHONPATH= python3 -m pip list --disable-pip-version-check --format=freeze > {IMAGE_CONSTRAINTS}.all"
        f" && {{ grep -viE '^(pip|setuptools|wheel)==' {IMAGE_CONSTRAINTS}.all > {IMAGE_CONSTRAINTS} || true; }}"
        f" && python3 -m pip install --disable-pip-version-check --no-input --no-cache-dir --target {PYTHON_DEPS_DIR}"
        f" -c {IMAGE_CONSTRAINTS} -r {shlex.quote(manifest)}"
        f" && python3 -m pip list --disable-pip-version-check --path {PYTHON_DEPS_DIR} --format=freeze"
    )


def installed_packages(ecosystem: str, stdout: str) -> list[str]:
    """``name==version`` (pip freeze) / ``name@version`` (npm ls) lines of an install's listing."""
    out: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if ecosystem == "python":
            if "==" in line and " " not in line:
                out.append(line)
        else:
            entry = line.lstrip("├└─┬│ `+-|").strip()
            if "@" in entry[1:] and " " not in entry:
                out.append(entry)
    return out


# error codes that mean "the runtime refused the call" (vs. "the call ran and failed")
REFUSAL_CODES = frozenset(
    {
        E.ARGS_INVALID, E.UNKNOWN_TOOL, E.ACTION_INVALID, E.TOOL_NOT_ALLOWED, E.STEP_FINISHED, E.TURN_BUDGET_EXHAUSTED,
        E.SCOPE_MISSING, E.SCOPE_VIOLATION, E.PATH_INVALID, E.PATH_OUTSIDE_WORKSPACE, E.PATH_FORBIDDEN, E.SYMLINK_REFUSED,
        E.COMMAND_FORBIDDEN, E.COMMAND_DESTRUCTIVE, E.REDACTED_PLACEHOLDER, E.PATCH_SYMLINK_REFUSED, E.PATTERN_INVALID,
    }
)  # fmt: skip
MAX_READ_BYTES = 2_000_000
MAX_DIFF_BYTES = 400_000
SNIPPET_CHARS = 600
_SLACK = 400  # markers appended after clipping


@dataclass(frozen=True)
class ActionRejected:
    """A raw model action that is not a valid tool call at all (unknown tool / malformed JSON)."""

    code: str
    message: str
    raw_tool: str | None = None
    ok: bool = False
    terminal: bool = False

    @property
    def output(self) -> str:
        return self.message


@dataclass
class _RunOutcome:
    result: CommandResult | None
    audit: AuditOutcome
    classification: CommandClassification
    cwd: str
    duration_ms: int
    error: str | None = None

    @property
    def sandbox_failed(self) -> bool:
        """No result, or the sandbox could not run the command at all (no exit code, not a timeout)."""
        return self.result is None or (self.result.exit_code is None and not self.result.timed_out)


Handler = Callable[[Any, CallContext, list[PendingEvent]], Awaitable[ToolResult]]


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class ToolEngine:
    def __init__(  # noqa: PLR0917 - positional order is the documented component contract
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        policies: PoliciesConfig,
        workspace: WorkspaceHandle,
        scope_guard: ScopeGuard | None,
        executor: CommandExecutor,
        git: GitReader,
        repo: RepoContextProvider,
        callbacks: ToolCallbacks | None = None,
        *,
        permissions: ToolPermissions | None = None,
        redactor: Redactor | None = None,
        worker_id: str | None = None,
        execution_target: str = "sandbox",
        max_read_bytes: int = MAX_READ_BYTES,
        ops: OpsToolProvider | None = None,
        capability: str | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        self.policies = policies
        self.workspace = workspace
        self._guard = scope_guard
        self.executor = executor
        self.git = git
        self.repo = repo
        self.callbacks: ToolCallbacks = callbacks or NullCallbacks()
        self.permissions = permissions or ToolPermissions()
        self.redactor = redactor or DEFAULT_REDACTOR
        self.worker_id = worker_id
        self.execution_target = execution_target
        self.max_read_bytes = max_read_bytes
        self.ops = ops
        self.capability = capability
        self.artifacts_dir = artifacts_dir  # ops tool outputs are stored as ``ops_output`` artifacts (acceptance evidence)
        self._sm = sessionmaker
        self.output_limit = max(500, policies.coder.tool_output_chars)
        self.fs = WorkspaceFS(workspace.path, read_forbidden=policies.scope.always_forbidden)
        self.lgit = LocalGit(self.fs.root)
        self.tracker = WorkspaceTracker(
            self.fs, self.lgit, generated_globs=(*DEFAULT_GENERATED_GLOBS, *policies.verifier.generated_file_globs)
        )
        self.classifier = CommandClassifier(policies.commands)
        self.recorder = ToolRecorder(sessionmaker, self.redactor)
        self._lock = asyncio.Lock()
        self._finished_by: ToolName | None = None
        self._research_requests = 0
        self._scope_requests = 0
        self._base_files: set[str] | None = None
        self._base_loaded = False
        self._created: set[str] = set()
        # commands that ran into their timeout, with the file-change count at that time: the same command on an
        # unchanged workspace would only block for the full timeout again (BUG-164)
        self._writes = 0
        self._timed_out: dict[tuple[str, str, int], int] = {}
        # every finished command: (file-change count, turn, outcome). The coder gets a fresh context each turn and
        # re-ran identical commands on unchanged files again and again (analysis 2026-10-16, live job 50784df5)
        self._ran: dict[tuple[str, str, int], tuple[int, int, str]] = {}
        # identical reads: (file-change count, turn). The context of every turn already shows the files read in this
        # attempt; the coder still re-read the same unchanged file turn after turn instead of acting (BUG-179)
        self._reads: dict[tuple[str, str], tuple[int, int]] = {}
        # manifest digest of the last successful install per (ecosystem, manifest): reinstalling it changes nothing
        self._installed: dict[tuple[str, str], str] = {}
        self._handlers: dict[ToolName, Handler] = {
            ToolName.list_files: self._list_files,
            ToolName.read_file: self._read_file,
            ToolName.read_range: self._read_range,
            ToolName.find_text: self._find_text,
            ToolName.search_repo: self._search_repo,
            ToolName.search_symbol: self._search_symbol,
            ToolName.git_status: self._git_status,
            ToolName.git_diff: self._git_diff,
            ToolName.write_file: self._write_file,
            ToolName.replace_text: self._replace_text,
            ToolName.apply_patch: self._apply_patch,
            ToolName.run_command: self._run_command,
            ToolName.run_test: self._run_test,
            ToolName.install_dependencies: self._install_dependencies,
            ToolName.request_scope_expansion: self._request_scope_expansion,
            ToolName.request_research: self._request_research,
            ToolName.request_replan: self._request_replan,
            ToolName.checkpoint: self._checkpoint,
            ToolName.complete_step: self._complete_step,
            ToolName.block_step: self._block_step,
        }
        for op_tool in OPS_TOOLS:
            self._handlers[op_tool] = functools.partial(self._ops_tool, op_tool)

    # ============================================================================================ public API
    @property
    def scope_guard(self) -> ScopeGuard | None:
        return self._guard

    @property
    def finished(self) -> bool:
        return self._finished_by is not None

    @property
    def finished_by(self) -> ToolName | None:
        return self._finished_by

    @property
    def max_turns(self) -> int:
        return self.permissions.max_turns or self.policies.coder.max_turns

    def _provided(self, tool: ToolName) -> bool:
        return tool not in OPS_TOOLS or (self.ops is not None and self.ops.provides(tool))

    def available_tools(self) -> list[ToolName]:
        return [t for t in ToolName if self.permissions.allows(t) and self._provided(t)]

    def tool_catalog(self) -> list[dict[str, Any]]:
        """JSON schemas of the tools this step may use (for the coder prompt)."""
        return tool_schemas(self.available_tools())

    async def execute_payload(
        self,
        payload: Mapping[str, Any] | str,
        *,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        attempt_id: uuid.UUID | None = None,
        turn: int = 0,
    ) -> ToolResult | ActionRejected:
        """Parse a raw model action and execute it; unknown tools / malformed actions are persisted and rejected."""
        try:
            action = parse_action(payload)
        except ToolProtocolError as exc:
            raw_tool = self._raw_tool_name(payload)
            await self._record_rejection(raw_tool or "<invalid>", exc, job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn)
            return ActionRejected(exc.code, exc.message, raw_tool)
        return await self.execute(action, job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn)

    async def execute(
        self,
        action: CoderAction,
        *,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        attempt_id: uuid.UUID | None = None,
        turn: int = 0,
    ) -> ToolResult:
        spec = get_spec(action.tool)  # ToolProtocolError only for actions built without validation
        async with self._lock:
            ctx = CallContext(job_id, step_id, attempt_id, turn, uuid.uuid4())
            started = time.monotonic()
            await self.recorder.start(ctx, spec.name.value, dict(action.args), self._started_event(spec, action, ctx))
            events: list[PendingEvent] = []
            try:
                result = await self._run(spec, action, ctx, events)
            except asyncio.CancelledError:
                await asyncio.shield(
                    self.recorder.finish(
                        ctx,
                        status="cancelled",
                        summary="cancelled",
                        error_code="CANCELLED",
                        duration_ms=_duration_ms(started),
                        events=[*events, self._finished_event(spec.name, None, "CANCELLED")],
                    )
                )
                raise
            result = self._finalise(result)
            if result.ok and result.terminal:
                self._finished_by = spec.name
            duration = _duration_ms(started)
            await self.recorder.finish(
                ctx,
                status=self._status(result),
                summary=self._summary(result),
                error_code=result.error_code,
                duration_ms=duration,
                events=[*events, self._finished_event(spec.name, result, result.error_code)],
            )
            return result

    # ============================================================================================ dispatch
    async def _run(self, spec: ToolSpec, action: CoderAction, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        try:
            self._precheck(spec, ctx)
            args = validate_args(spec, action.args)
            return await self._handlers[spec.name](args, ctx, events)
        except ToolError as exc:
            return ToolResult(tool=spec.name, ok=False, output=exc.message, error_code=exc.code, data=exc.data)
        except Exception as exc:  # never leak a traceback to the model; keep the runtime alive
            log.exception("tool handler failed", extra={"tool": spec.name.value, "error_type": type(exc).__name__})
            return ToolResult(
                tool=spec.name,
                ok=False,
                output=f"internal error in {spec.name.value}: {type(exc).__name__}",
                error_code=E.TOOL_INTERNAL_ERROR,
            )

    def _precheck(self, spec: ToolSpec, ctx: CallContext) -> None:
        if self._finished_by is not None:
            raise ToolError(
                E.STEP_FINISHED, f"the step was already finished with {self._finished_by.value}; no further tool calls are accepted"
            )
        if not self.permissions.allows(spec.name):
            allowed = ", ".join(t.value for t in self.available_tools())
            raise ToolError(E.TOOL_NOT_ALLOWED, f"tool '{spec.name.value}' is not allowed for this step; allowed: {allowed}")
        if not self._provided(spec.name):
            raise ToolError(E.OPS_TOOL_UNAVAILABLE, f"tool '{spec.name.value}' is not available on this runtime")
        if ctx.turn > self.max_turns and spec.name not in TERMINAL_TOOLS:
            raise ToolError(
                E.TURN_BUDGET_EXHAUSTED,
                f"turn budget of {self.max_turns} exhausted; finish with complete_step, block_step or request_replan",
            )

    def _finalise(self, result: ToolResult) -> ToolResult:
        output = self.redactor.text(result.output)
        truncated = result.truncated
        if len(output) > self.output_limit + _SLACK:
            output, _ = clip_head(output, self.output_limit)
            truncated = True
        data = self.redactor.obj(result.data)
        spec = get_spec(result.tool)
        return result.model_copy(
            update={
                "output": output,
                "truncated": truncated,
                "data": data if isinstance(data, dict) else {},
                "terminal": spec.terminal and result.ok,
            }
        )

    @staticmethod
    def _status(result: ToolResult) -> str:
        if result.ok:
            return "succeeded"
        return "refused" if result.error_code in REFUSAL_CODES else "failed"

    @staticmethod
    def _summary(result: ToolResult) -> str:
        head = "ok" if result.ok else f"error {result.error_code}"
        return f"{head}: {result.output[:600]}"

    def _started_event(self, spec: ToolSpec, action: CoderAction, ctx: CallContext) -> PendingEvent:
        redacted = self.redactor.obj(dict(action.args))  # redact before clipping so no secret fragment survives
        preview = {k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v) for k, v in redacted.items()}
        return PendingEvent(
            EventType.TOOL_CALL_STARTED,
            {
                "tool": spec.name.value,
                "turn": ctx.turn,
                "status": action.status,
                "decision": action.decision,
                "mutating": spec.mutating,
                "args": json.loads(json.dumps(preview, default=str)),
            },
        )

    def _finished_event(self, tool: ToolName, result: ToolResult | None, error_code: str | None) -> PendingEvent:
        payload: dict[str, Any] = {"tool": tool.value, "ok": bool(result and result.ok), "error_code": error_code}
        if result is not None:
            payload |= {
                "terminal": result.terminal,
                "truncated": result.truncated,
                "mutated_paths": result.mutated_paths[:100],
                "summary": result.output[:300],
            }
        severity = Severity.info if result is not None and result.ok else Severity.warning
        return PendingEvent(EventType.TOOL_CALL_FINISHED, payload, severity, with_duration=True)

    @staticmethod
    def _raw_tool_name(payload: Mapping[str, Any] | str) -> str | None:
        data: Any = payload
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return None
        if isinstance(data, Mapping):
            tool = data.get("tool")
            return str(tool)[:64] if tool is not None else None
        return None

    async def _record_rejection(
        self,
        raw_tool: str,
        exc: ToolProtocolError,
        *,
        job_id: uuid.UUID | None,
        step_id: uuid.UUID | None,
        attempt_id: uuid.UUID | None,
        turn: int,
    ) -> None:
        ctx = CallContext(job_id, step_id, attempt_id, turn, uuid.uuid4())
        started = PendingEvent(EventType.TOOL_CALL_STARTED, {"tool": raw_tool, "turn": turn, "rejected": True})
        async with self._lock:
            await self.recorder.start(ctx, raw_tool, {}, started)
            await self.recorder.finish(
                ctx,
                status="refused",
                summary=f"error {exc.code}: {exc.message}",
                error_code=exc.code,
                duration_ms=0,
                events=[
                    PendingEvent(
                        EventType.TOOL_CALL_FINISHED,
                        {"tool": raw_tool, "ok": False, "error_code": exc.code, "summary": exc.message[:300]},
                        Severity.warning,
                    )
                ],
            )

    # ============================================================================================ helpers
    def _clip_redacted(self, text: str, limit: int) -> str:
        return self.redactor.text(text)[:limit]

    def _ok(self, tool: ToolName, output: str, **fields: Any) -> ToolResult:
        return ToolResult(tool=tool, ok=True, output=output, **fields)

    def _hidden(self, raw: str) -> bool:
        try:
            rel = canonical_path(raw)
        except ToolError:
            return True
        return rel != "." and self.fs.is_hidden(rel)

    async def _inventory(self) -> list[str]:
        try:
            if await self.lgit.is_repo_root():
                files = [f for f in await self.lgit.list_files() if not f.endswith("/")]
            else:
                files = await asyncio.to_thread(self.fs.walk_files)
        except RuntimeError as exc:
            raise ToolError(E.GIT_UNAVAILABLE, f"cannot list workspace files: {exc}") from exc
        return await asyncio.to_thread(self.fs.visible, files)

    def _guard_for_write(self) -> ScopeGuard:
        if self._guard is None:
            raise ToolError(E.SCOPE_MISSING, "this step has no scope contract; file changes are not permitted")
        return self._guard

    async def _load_base(self) -> None:
        """Files of the workspace base commit: scope operations are judged relative to the base (like the verifier)."""
        if not self._base_loaded:
            self._base_files = await self.lgit.tree_files(self.workspace.base_sha) if await self.lgit.is_repo_root() else None
            self._base_loaded = True

    def _effective(self, rel: str, op: Operation) -> Operation:
        """A file that does not exist at the base is a *creation* of this step, also when it is edited or removed again."""
        if op == "create":
            return op
        in_base = rel in self._base_files if self._base_files is not None else rel not in self._created
        return op if in_base else "create"

    def _note_created(self, changes: list[tuple[str, Operation]]) -> None:
        self._created.update(p for p, op in changes if op == "create")

    async def _authorise(self, tool: ToolName, changes: list[tuple[str, Operation]], events: list[PendingEvent]) -> None:
        guard = self._guard_for_write()
        await self._load_base()
        violations = guard.audit([(p, self._effective(p, op)) for p, op in changes])
        if violations:
            events.append(
                PendingEvent(
                    EventType.SCOPE_VIOLATION,
                    {"tool": tool.value, "source": "tool", "scope_version": guard.contract.version, "violations": violations},
                    Severity.warning,
                )
            )
            reasons = "; ".join(v["reason"] for v in violations[:5])
            raise ToolError(
                E.SCOPE_VIOLATION,
                f"refused by scope v{guard.contract.version}: {reasons}. Use request_scope_expansion with a justification "
                "if the change is required.",
                data={"violations": violations, "scope": self._scope_view()},
            )

    def _scope_view(self) -> dict[str, Any]:
        if self._guard is None:
            return {}
        c = self._guard.contract
        return {
            "version": c.version,
            "target_paths": c.target_paths,
            "allowed_new_paths": c.allowed_new_paths,
            "allowed_operations": list(c.allowed_operations),
        }

    def _decide(self) -> Decide:
        guard = self._guard
        if guard is None:
            return lambda _path, _op: (False, "this step has no scope contract; file changes are not permitted")
        return lambda path, op: guard.decide(path, self._effective(path, op))

    @staticmethod
    def _check_placeholder(new_text: str, existing: str | None) -> None:
        if REDACTED in new_text and (existing is None or REDACTED not in existing):
            raise ToolError(
                E.REDACTED_PLACEHOLDER,
                f"the content contains the redaction marker {REDACTED}; secrets are masked in tool output and must not be "
                "written back. Leave lines with secrets untouched (edit around them with replace_text).",
            )

    def _check_patch_placeholder(self, modified: list[str]) -> None:
        """Added patch lines may carry the redaction marker only if a modified file already contains it literally."""
        for rel in modified:
            try:
                if REDACTED.encode() in (self.fs.root / rel).read_bytes():
                    return
            except OSError:
                continue
        self._check_placeholder(REDACTED, None)

    def _file_changed(self, tool: ToolName, changes: list[dict[str, Any]], events: list[PendingEvent]) -> None:
        if changes:
            self._writes += 1
            events.append(PendingEvent(EventType.FILE_CHANGED, {"tool": tool.value, "changes": changes[:200], "count": len(changes)}))

    # ============================================================================================ 17.1 read tools
    async def _list_files(self, args: ListFilesArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        rel, path = self.fs.resolve(args.path, allow_root=True)
        if not path.exists():
            raise ToolError(E.NOT_FOUND, f"'{rel}' does not exist")
        if not path.is_dir():
            raise ToolError(E.NOT_A_DIRECTORY, f"'{rel}' is a file; use read_file")
        files = await self._inventory()
        selected = filter_listing(files, rel, args.pattern)
        entries = selected if args.recursive else immediate_children(selected, rel)
        shown = entries[: args.max_entries]
        text, clipped = clip_head("\n".join(shown) if shown else "(no files)", self.output_limit)
        more = len(entries) - len(shown)
        if more > 0:
            text += f"\n…[{more} more entries; narrow 'path' or 'pattern']"
        return self._ok(
            ToolName.list_files,
            text,
            truncated=clipped or more > 0,
            data={"path": rel, "count": len(entries), "shown": len(shown), "recursive": args.recursive},
        )

    def _check_read_repeat(self, tool: ToolName, key: str, turn: int) -> None:
        """Refuse a read identical to one of the last ``READ_REPEAT_WINDOW`` turns while no file changed: its content is
        already in this turn's context (files read in this attempt), so reading again only costs a turn."""
        previous = self._reads.get((tool.value, key))
        if previous is None or previous[0] != self._writes or turn - previous[1] > READ_REPEAT_WINDOW:
            return
        raise ToolError(
            E.READ_REPEATED,
            f"read refused: you already read exactly this in turn {previous[1]} and no file changed since; its content is "
            "shown above among the files you read (read_range shows lines beyond those). Act on it now: edit the code or "
            "the test, run a different check, or call complete_step/block_step.",
            data={"previous_turn": previous[1], "repeated": True},
        )

    async def _read_file(self, args: ReadFileArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        key = self.fs.resolve(args.path)[0]
        self._check_read_repeat(ToolName.read_file, key, ctx.turn)
        tf = await asyncio.to_thread(self.fs.read_text, args.path, max_bytes=self.max_read_bytes)
        self._reads[(ToolName.read_file.value, key)] = (self._writes, ctx.turn)
        lines = tf.text.count("\n") + (1 if tf.text and not tf.text.endswith("\n") else 0)
        text, clipped = clip_head(tf.text, self.output_limit)
        if clipped:
            text += f"\n[{tf.rel} has {lines} lines; use read_range(start, end) for the rest]"
        if not tf.text:
            text = "(empty file)"
        return self._ok(ToolName.read_file, text, truncated=clipped, data={"path": tf.rel, "bytes": tf.size, "lines": lines})

    async def _read_range(self, args: ReadRangeArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        key = f"{self.fs.resolve(args.path)[0]}:{args.start}-{args.end}"
        self._check_read_repeat(ToolName.read_range, key, ctx.turn)
        rel, lines, total = await asyncio.to_thread(self.fs.read_lines, args.path, args.start, args.end)
        if args.start > total > 0:
            raise ToolError(E.RANGE_INVALID, f"'{rel}' has only {total} lines (start={args.start})", data={"total_lines": total})
        self._reads[(ToolName.read_range.value, key)] = (self._writes, ctx.turn)
        if total == 0:
            return self._ok(ToolName.read_range, "(empty file)", data={"path": rel, "start": 0, "end": 0, "total_lines": 0})
        end = min(args.end, total)
        width = len(str(end))
        body = "\n".join(f"{n:>{width}}| {line}" for n, line in enumerate(lines, start=args.start))
        text, clipped = clip_head(body, self.output_limit)
        if end < args.end:
            text += f"\n[end of file: {total} lines]"
        return self._ok(
            ToolName.read_range, text, truncated=clipped, data={"path": rel, "start": args.start, "end": end, "total_lines": total}
        )

    async def _find_text(self, args: FindTextArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        if args.regex and regex_is_risky(args.pattern):
            raise ToolError(E.PATTERN_INVALID, "regex has nested quantifiers (catastrophic backtracking risk); simplify the pattern")
        rel, path = self.fs.resolve(args.path, allow_root=True)
        if not path.exists():
            raise ToolError(E.NOT_FOUND, f"'{rel}' does not exist")
        files = await self._inventory()
        candidates = [rel] if path.is_file() and rel in files else filter_listing(files, rel, args.glob)
        outcome = await asyncio.to_thread(
            search_files, self.fs, candidates, args.pattern, regex=args.regex, ignore_case=args.ignore_case, max_results=args.max_results
        )
        body = "\n".join(outcome.lines) if outcome.lines else f"no matches in {outcome.files_searched} files"
        text, clipped = clip_head(body, self.output_limit)
        if outcome.limit_hit:
            text += f"\n…[result limit {args.max_results} reached; refine the pattern]"
        if outcome.time_budget_hit:
            text += "\n…[search time budget exhausted; narrow 'path' or 'glob']"
        return self._ok(
            ToolName.find_text,
            text,
            truncated=clipped or outcome.limit_hit or outcome.time_budget_hit,
            data={"matches": outcome.matches, "files_searched": outcome.files_searched, "files_matched": outcome.files_matched},
        )

    def _format_hits(self, hits: list[RepoHit]) -> tuple[str, int]:
        visible = [h for h in hits if not self._hidden(h.path)]
        blocks = []
        for h in visible:
            snippet = clip_head(h.snippet.strip("\n"), SNIPPET_CHARS)[0]
            indented = "\n".join("    " + line for line in snippet.splitlines()) if snippet else ""
            blocks.append(f"{h.path}:{h.start_line}-{h.end_line} (score {h.score:.3f})" + (f"\n{indented}" if indented else ""))
        return ("\n".join(blocks) if blocks else "no results"), len(visible)

    async def _repo_call(self, what: str, call: Awaitable[list[RepoHit]]) -> list[RepoHit]:
        try:
            return await call
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(E.REPO_UNAVAILABLE, f"{what} is unavailable: {type(exc).__name__}") from exc

    async def _search_repo(self, args: SearchRepoArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        hits = await self._repo_call("repository search", self.repo.search(self.workspace, args.query, k=args.k))
        text, n = self._format_hits(hits)
        out, clipped = clip_head(text, self.output_limit)
        return self._ok(
            ToolName.search_repo, out, truncated=clipped, data={"hits": n, "paths": [h.path for h in hits if not self._hidden(h.path)]}
        )

    async def _search_symbol(self, args: SearchSymbolArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        hits = await self._repo_call("symbol search", self.repo.find_symbol(self.workspace, args.name, k=args.k))
        text, n = self._format_hits(hits)
        out, clipped = clip_head(text, self.output_limit)
        return self._ok(
            ToolName.search_symbol, out, truncated=clipped, data={"hits": n, "paths": [h.path for h in hits if not self._hidden(h.path)]}
        )

    # ============================================================================================ 17.2 git read
    async def _git_status(self, args: BaseModel, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        try:
            entries = await self.git.status(self.workspace)
        except Exception as exc:
            raise ToolError(E.GIT_UNAVAILABLE, f"git status failed: {type(exc).__name__}: {exc}") from exc
        lines = [f"{e.status} {e.orig_path} -> {e.path}" if e.orig_path else f"{e.status} {e.path}" for e in entries]
        text, clipped = clip_head("\n".join(lines) if lines else "working tree clean (no changes against HEAD)", self.output_limit)
        return self._ok(
            ToolName.git_status,
            text,
            truncated=clipped,
            data={
                "entries": [{"path": e.path, "status": e.status, "orig_path": e.orig_path} for e in entries[:500]],
                "count": len(entries),
            },
        )

    def _filter_diff(self, diff: str) -> tuple[str, list[str]]:
        out: list[str] = []
        hidden: list[str] = []
        skip = False
        for line in diff.splitlines(keepends=True):
            if line.startswith("diff --git "):
                path = diff_section_path(line.rstrip("\n"))
                skip = path is not None and self._hidden(path)
                if skip and path is not None:
                    hidden.append(path)
            if not skip:
                out.append(line)
        return "".join(out), hidden

    async def _git_diff(self, args: GitDiffArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        paths: list[str] | None = None
        if args.paths:
            paths = []
            for raw in args.paths:
                rel = canonical_path(raw)
                if rel == ".":
                    paths = None
                    break
                if self.fs.is_hidden(rel):
                    raise ToolError(E.PATH_FORBIDDEN, f"diff of '{rel}' is not permitted (git internals or secret file)")
                paths.append(rel)
        try:
            diff = await self.git.diff(self.workspace, paths, max_bytes=MAX_DIFF_BYTES)
        except Exception as exc:
            raise ToolError(E.GIT_UNAVAILABLE, f"git diff failed: {type(exc).__name__}: {exc}") from exc
        diff, hidden = self._filter_diff(diff)
        body = diff if diff.strip() else "no changes"
        if hidden:
            body += f"\n[{len(hidden)} protected file(s) omitted]"
        text, clipped = clip_head(body, self.output_limit)
        if clipped:
            text += "\n[diff truncated; pass 'paths' to see individual files]"
        return self._ok(ToolName.git_diff, text, truncated=clipped, data={"bytes": len(diff), "omitted": len(hidden)})

    # ============================================================================================ 17.3 file writes
    async def _write_file(self, args: WriteFileArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        self._guard_for_write()
        rel, target = self.fs.resolve_for_write(args.path)
        exists = target.exists()
        op: Operation = "modify" if exists else "create"
        await self._authorise(ToolName.write_file, [(rel, op)], events)
        try:
            data = args.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ToolError(E.ARGS_INVALID, f"content is not valid UTF-8 text: {exc.reason}") from exc
        old = await asyncio.to_thread(target.read_bytes) if exists else None
        old_text = old.decode("utf-8", errors="replace") if old is not None and not is_binary(old) else None
        self._check_placeholder(args.content, old_text)
        lines = args.content.count("\n") + (1 if args.content and not args.content.endswith("\n") else 0)
        if old == data:
            return self._ok(ToolName.write_file, f"{rel} already has this content; nothing written", data={"path": rel, "changed": False})
        mode = stat.S_IMODE(target.stat().st_mode) if exists else None
        await asyncio.to_thread(self.fs.atomic_write, target, data, mode=mode)
        self._note_created([(rel, op)])
        self._file_changed(ToolName.write_file, [{"path": rel, "operation": op, "bytes": len(data)}], events)
        verb = "created" if op == "create" else "overwrote"
        return self._ok(
            ToolName.write_file,
            f"{verb} {rel} ({len(data)} bytes, {lines} lines)",
            mutated_paths=[rel],
            data={"path": rel, "operation": op, "bytes": len(data), "changed": True},
        )

    async def _replace_text(self, args: ReplaceTextArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        self._guard_for_write()
        rel, target = self.fs.resolve_for_write(args.path)
        if not target.exists():
            raise ToolError(E.NOT_FOUND, f"'{rel}' does not exist; use write_file to create it")
        await self._authorise(ToolName.replace_text, [(rel, "modify")], events)
        if args.old == args.new:
            raise ToolError(E.NO_CHANGE, "'old' and 'new' are identical")
        tf = await asyncio.to_thread(self.fs.read_text, rel, max_bytes=self.max_read_bytes)
        text = tf.text
        self._check_placeholder(args.new, text)
        old, new = args.old, args.new
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\r\n" not in old and "\n" in old:
            crlf_old, crlf_new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            if text.count(crlf_old):
                old, new, count = crlf_old, crlf_new, text.count(crlf_old)
        if count == 0:
            raise ToolError(E.TEXT_NOT_FOUND, self._not_found_hint(rel, text, old))
        if count != args.count:
            lines = _occurrence_lines(text, old)
            raise ToolError(
                E.TEXT_COUNT_MISMATCH,
                f"'old' occurs {count} times in {rel} (lines {', '.join(map(str, lines[:20]))}) but count={args.count}; "
                f"add surrounding context to make it unique or pass count={count}",
                data={"occurrences": count, "lines": lines[:50]},
            )
        first_line = _occurrence_lines(text, old)[0]
        updated = text.replace(old, new)
        data = updated.encode("utf-8")
        await asyncio.to_thread(self.fs.atomic_write, target, data, mode=tf.mode)
        self._file_changed(ToolName.replace_text, [{"path": rel, "operation": "modify", "replacements": count}], events)
        return self._ok(
            ToolName.replace_text,
            f"replaced {count} occurrence(s) in {rel} (first at line {first_line}); file now has {len(data)} bytes",
            mutated_paths=[rel],
            data={"path": rel, "operation": "modify", "replacements": count, "first_line": first_line},
        )

    @staticmethod
    def _not_found_hint(rel: str, text: str, old: str) -> str:
        msg = f"'old' text not found in {rel}"
        if REDACTED in old:
            return msg + f"; it contains the redaction marker {REDACTED} – masked secrets cannot be matched, edit around them"
        if old.strip() and old.strip() in text:
            return msg + "; the text exists with different leading/trailing whitespace – copy it exactly (read_range shows it)"
        first = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
        if first:
            hits = [n for n, ln in enumerate(text.splitlines(), start=1) if first in ln]
            if hits:
                return (
                    msg
                    + f"; its first line appears at line(s) {', '.join(map(str, hits[:10]))} – re-read that range, indentation must match"
                )
        return msg + "; re-read the file (read_range) and copy the exact current text"

    # ============================================================================================ 17.4 patch
    async def _apply_patch(self, args: ApplyPatchArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        guard = self._guard_for_write()
        parsed = parse_patch(args.patch)
        if any(f.binary for f in parsed.files):
            raise ToolError(E.PATCH_INVALID, "binary patches are not supported; use write_file for text files")
        targets: list[tuple[str, Operation]] = []
        for t in parsed.targets():
            rel = canonical_path(t.path)
            if rel == ".":
                raise ToolError(E.PATCH_INVALID, "patch header without a file path")
            rel, _target = self.fs.resolve_for_write(rel)
            targets.append((rel, t.operation))
        await self._authorise(ToolName.apply_patch, targets, events)
        added = [ln[1:] for ln in args.patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
        if any(REDACTED in ln for ln in added):
            await asyncio.to_thread(self._check_patch_placeholder, [p for p, op in targets if op == "modify"])
        patch = args.patch if args.patch.endswith("\n") else args.patch + "\n"
        data = patch.encode("utf-8")
        numstat = await self.lgit.apply_numstat(data, parsed.strip)
        if not numstat.ok:
            raise ToolError(E.PATCH_INVALID, f"git apply cannot parse the patch: {numstat.err_text(1500)}")
        declared = {p for p, _ in targets}
        touched = parse_numstat(numstat.stdout)
        undeclared = sorted(p for p in touched if canonical_path(p) not in declared)
        if undeclared:
            raise ToolError(E.PATCH_INVALID, f"patch touches files not declared in its headers: {', '.join(undeclared[:10])}")
        check = await self.lgit.apply_check(data, parsed.strip)
        if not check.ok:
            raise ToolError(
                E.PATCH_FAILED,
                f"patch does not apply: {check.err_text(2000)}. Re-read the affected lines; context lines must match exactly "
                "(replace_text is often simpler for small edits).",
            )
        allowed_ops = set(targets)

        def decide(path: str, op: Operation) -> tuple[bool, str]:
            if (path, op) not in allowed_ops:
                return False, "change not declared by the patch"
            return guard.decide(path, self._effective(path, op))

        before = await self.tracker.snapshot()
        res = await self.lgit.apply(data, parsed.strip)
        audit = await self.tracker.audit(before, decide)
        if not res.ok:
            raise ToolError(E.PATCH_FAILED, f"git apply failed: {res.err_text(2000)}")
        if audit.violations:
            self._scope_violation_event(ToolName.apply_patch, audit, events)
            raise ToolError(
                E.PATCH_FAILED,
                "patch produced undeclared changes; they were reverted",
                data={"violations": [v.to_dict() for v in audit.violations]},
            )
        self._note_created(targets)
        changes = [{"path": p, "operation": op} for p, op in sorted(targets)]
        self._file_changed(ToolName.apply_patch, changes, events)
        mutated = sorted(declared)
        summary = ", ".join(f"{op} {p}" for p, op in sorted(targets))
        return self._ok(ToolName.apply_patch, f"patch applied: {summary}", mutated_paths=mutated, data={"changes": changes})

    # ============================================================================================ 17.5/17.6 commands
    def _scope_violation_event(self, tool: ToolName, audit: AuditOutcome, events: list[PendingEvent]) -> None:
        events.append(
            PendingEvent(
                EventType.SCOPE_VIOLATION,
                {
                    "tool": tool.value,
                    "source": "command_side_effect" if tool in (ToolName.run_command, ToolName.run_test) else "tool",
                    "scope_version": self._guard.contract.version if self._guard else None,
                    "violations": [v.to_dict() for v in audit.violations[:100]],
                },
                Severity.warning,
            )
        )

    def _classify(self, command: str, *, install: bool = False) -> CommandClassification:
        cls = self.classifier.classify(command)
        manager = None if install or self.permissions.network else package_install(command)
        if manager is not None and cls.kind != "forbidden":
            hint = (
                "add the package to requirements.txt / package.json, then call install_dependencies"
                if ToolName.install_dependencies in (self.permissions.allowed_tools or set())
                else "use the standard library, or block_step naming the missing dependency"
            )
            raise ToolError(
                E.PACKAGE_INSTALL_REFUSED,
                f"command refused: '{manager}' needs network, and commands run without network – {hint}",
                data={"classification": cls.kind, "package_manager": manager},
            )
        if cls.kind == "forbidden":
            raise ToolError(
                E.COMMAND_FORBIDDEN, f"command refused: {cls.reason}", data={"classification": cls.kind, "pattern": cls.pattern}
            )
        if cls.kind == "destructive" and not self.permissions.allow_destructive_commands:
            raise ToolError(
                E.COMMAND_DESTRUCTIVE,
                "command refused: destructive commands are not allowed for this step (edit files with the file tools instead)",
                data={"classification": cls.kind, "pattern": cls.pattern},
            )
        return cls

    def _resolve_cwd(self, raw: str) -> str:
        cwd, path = self.fs.resolve(raw, allow_root=True)
        if not path.is_dir():
            raise ToolError(E.NOT_A_DIRECTORY, f"cwd '{cwd}' is not a directory")
        return cwd

    def _timeout(self, requested: int | None) -> int:
        limit = self.permissions.max_command_timeout_seconds or self.policies.sandbox.default_timeout_seconds
        default = min(self.policies.coder.default_command_timeout_seconds, limit)
        return max(1, min(requested or default, limit))

    async def _sandbox_run(
        self,
        tool: ToolName,
        *,
        command: str,
        cwd_raw: str,
        timeout_seconds: int | None,
        purpose: str,
        ctx: CallContext,
        events: list[PendingEvent],
        egress: bool = False,
        image: str | None = None,
    ) -> _RunOutcome:
        classification = self._classify(command, install=egress)
        cwd = self._resolve_cwd(cwd_raw)
        full = command if cwd == "." else f"cd -- {shlex.quote(cwd)} || exit 97\n{command}"
        effective_timeout = timeout_seconds if egress and timeout_seconds else self._timeout(timeout_seconds)
        repeat_key = (cwd, command.strip(), effective_timeout)
        if self._timed_out.get(repeat_key) == self._writes:
            raise ToolError(
                E.COMMAND_TIMEOUT,
                f"command refused: it already ran into its {effective_timeout}s timeout and no file changed since, so it "
                f"would block for {effective_timeout}s again. {TIMEOUT_HINT}",
                data={"timeout_seconds": effective_timeout, "repeated": True},
            )
        previous = self._ran.get(repeat_key) if not egress else None
        if previous is not None and previous[0] == self._writes:
            passed = previous[2] == "exit code 0"
            follow_up = (
                "It succeeded: if the STEP GOAL or ACCEPTANCE still needs a change (for example new tests), make that "
                "change now; otherwise call complete_step."
                if passed
                else "Its exact output is in LATEST FAILURE: fix the code or the tests it names, then run it again."
            )
            raise ToolError(
                E.COMMAND_REPEATED,
                f"command refused: it already ran in turn {previous[1]} and no file changed since ({previous[2]}); running it "
                f"again gives the same result. {follow_up}",
                data={"previous_turn": previous[1], "repeated": True},
            )
        req = ExecutionRequest(
            command=full,
            timeout_seconds=effective_timeout,
            network=False if egress else self.permissions.network,
            egress=egress,
            image=image or self.permissions.image,
            job_id=ctx.job_id,
            step_id=ctx.step_id,
            attempt_id=ctx.attempt_id,
            purpose=purpose,
        )
        await self._load_base()
        before = await self.tracker.snapshot()
        started = time.monotonic()
        result: CommandResult | None = None
        error: str | None = None
        try:
            result = await self.executor.run(self.workspace, req)
        except Exception as exc:  # the command may have partially run: the audit below still applies
            error = f"{type(exc).__name__}: {exc}"
            log.warning("sandbox execution failed", extra={"tool": tool.value, "error_type": type(exc).__name__})
        if result is not None and result.exit_code is None and not result.timed_out and error is None:
            error = result.error or "the sandbox returned no exit code"
        duration = result.duration_ms if result is not None and result.duration_ms else _duration_ms(started)
        audit = await self.tracker.audit(before, self._decide())
        if audit.violations:
            self._scope_violation_event(tool, audit, events)
        changed = [{"path": c.path, "operation": c.operation} for c in audit.allowed]
        self._note_created([(c.path, c.operation) for c in audit.allowed])
        self._file_changed(tool, changed, events)
        stdout = strip_ansi(result.stdout) if result else ""
        stderr = strip_ansi(result.stderr) if result else ""
        if error:
            stderr = f"{stderr}\n[sandbox] {error}" if stderr else f"[sandbox] {error}"
        exit_code = result.exit_code if result else None
        timed_out = bool(result and result.timed_out)
        if timed_out:
            self._timed_out[repeat_key] = self._writes
        elif result is not None and error is None:
            outcome = f"exit code {exit_code}"
            self._ran[repeat_key] = (self._writes, ctx.turn, outcome)
        await self.recorder.command_run(
            ctx,
            command=command,
            cwd=cwd,
            classification=classification.kind,
            exit_code=exit_code,
            timed_out=timed_out,
            network=req.network,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            target=self.execution_target,
            worker_id=self.worker_id,
            events=[
                PendingEvent(
                    EventType.COMMAND_RUN,
                    {
                        "tool": tool.value,
                        "command": self._clip_redacted(command, 1000),
                        "cwd": cwd,
                        "classification": classification.kind,
                        "exit_code": exit_code,
                        "timed_out": timed_out,
                        "network": req.network,
                        "egress": req.egress,
                        "purpose": purpose,
                        "sandbox": result.sandbox if result else None,
                        "error": error,
                        "reverted": len(audit.violations),
                        "changed_files": len(changed),
                    },
                    Severity.info if exit_code == 0 and not timed_out and not error else Severity.warning,
                )
            ],
        )
        return _RunOutcome(result, audit, classification, cwd, duration, error)

    def _render_run(self, command: str, run: _RunOutcome, *, budget: int, head_ratio: float) -> tuple[str, bool]:
        res = run.result
        header = f"$ {command}" + (f"  (cwd: {run.cwd})" if run.cwd != "." else "")
        if res is None or run.sandbox_failed:
            status = f"sandbox error: {run.error}"
        elif res.timed_out:
            status = f"timed out after {run.duration_ms / 1000:.1f}s (killed)"
        else:
            status = f"exit code {res.exit_code} · {run.duration_ms / 1000:.1f}s"
        footer: list[str] = []
        for v in run.audit.violations[:20]:
            state = "reverted" if v.reverted else "NOT reverted"
            footer.append(f"[scope] {v.operation} {v.path}: {v.reason} ({state})")
        if run.audit.allowed:
            footer.append("[files] changed: " + ", ".join(f"{c.operation} {c.path}" for c in run.audit.allowed[:30]))
        if run.audit.cleaned:
            footer.append(f"[cleanup] removed {len(run.audit.cleaned)} new generated file(s) that are not git-ignored")
        stdout = strip_ansi(res.stdout) if res else ""
        stderr = strip_ansi(res.stderr) if res else ""
        fixed = len(header) + len(status) + sum(len(f) + 1 for f in footer) + 40
        room = max(200, budget - fixed)
        truncated = bool(res and (res.stdout_truncated or res.stderr_truncated))
        if stdout and stderr:
            share_out = min(len(stdout), max(room // 3, room - min(len(stderr), room // 2)))
            out_txt, t1 = clip_middle(stdout, share_out, head_ratio=head_ratio)
            err_txt, t2 = clip_middle(stderr, max(100, room - share_out), head_ratio=head_ratio)
        else:
            out_txt, t1 = clip_middle(stdout, room, head_ratio=head_ratio)
            err_txt, t2 = clip_middle(stderr, room, head_ratio=head_ratio)
        parts = [header, status]
        if out_txt:
            parts += ["--- stdout ---", out_txt.rstrip("\n")]
        if err_txt:
            parts += ["--- stderr ---", err_txt.rstrip("\n")]
        if not out_txt and not err_txt and res is not None:
            parts.append("(no output)")
        if res is not None and res.timed_out:
            footer.append(f"[hint] {TIMEOUT_HINT}")
        missing = self._missing_module_hint(f"{res.stdout}\n{res.stderr}") if res is not None else None
        if missing:
            footer.append(missing)
        parts += footer
        return "\n".join(parts), truncated or t1 or t2

    async def _ops_tool(self, tool: ToolName, args: BaseModel, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        """Operations tool via the runtime provider; workspace side effects are audited against the scope."""
        assert self.ops is not None  # _precheck guarantees a provider
        await self._load_base()
        before = await self.tracker.snapshot()
        call = OpsCall(tool, args, self.workspace, self.capability, ctx.job_id, ctx.step_id, ctx.attempt_id, ctx.turn)
        try:
            res = await self.ops.call(call)
        except Exception as exc:  # the provider may have partially run: the audit below still applies
            log.warning("operations tool failed", extra={"tool": tool.value, "error_type": type(exc).__name__})
            res = OpsResult(False, f"{tool.value} failed: {type(exc).__name__}: {exc}", error_code=E.OPS_TOOL_FAILED)
        audit = await self.tracker.audit(before, self._decide())
        if audit.violations:
            self._scope_violation_event(tool, audit, events)
        changed = [{"path": c.path, "operation": c.operation} for c in audit.allowed]
        self._note_created([(c.path, c.operation) for c in audit.allowed])
        self._file_changed(tool, changed, events)
        error_code = E.COMMAND_SCOPE_VIOLATION if audit.violations else res.error_code
        data = dict(res.data)
        if audit.violations:
            data["violations"] = [v.to_dict() for v in audit.violations[:50]]
        elif res.ok:
            artifact = await self._record_ops_output(tool, res, ctx)
            if artifact is not None:
                data["artifact"] = artifact
        return ToolResult(
            tool=tool,
            ok=res.ok and not audit.violations,
            output=res.output,
            error_code=None if res.ok and not audit.violations else (error_code or E.OPS_TOOL_FAILED),
            mutated_paths=sorted({c.path for c in audit.allowed}),
            data=data,
        )

    async def _record_ops_output(self, tool: ToolName, res: OpsResult, ctx: CallContext) -> str | None:
        """Successful operations tool output → artifact ``ops_output`` / ``<tool>-t<turn>.txt`` of the step (redacted).

        Remote steps (``ssh``/``deploy``) change nothing in the workspace; their machine-checkable acceptance is this
        recorded evidence (``{"type": "artifact", "kind": "ops_output", "name_glob": "ssh_service_status*"}``)."""
        if self.artifacts_dir is None or ctx.job_id is None:
            return None
        from hermclaw.persistence.models import Artifact  # local: keeps the tool engine importable without the ORM

        body = self.redactor.text(res.output).encode("utf-8", errors="replace")
        name = f"{tool.value}-t{ctx.turn}.txt"
        target = Path(self.artifacts_dir) / str(ctx.job_id) / str(ctx.step_id or "job") / "ops" / name

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

        try:
            await asyncio.to_thread(_write)
            async with self._sm() as s:
                s.add(
                    Artifact(
                        job_id=ctx.job_id,
                        step_id=ctx.step_id,
                        kind="ops_output",
                        name=name,
                        path=str(target),
                        media_type="text/plain",
                        size_bytes=len(body),
                        sha256=hashlib.sha256(body).hexdigest(),
                        metadata_={"tool": tool.value, "capability": self.capability, "turn": ctx.turn, "attempt_id": str(ctx.attempt_id)},
                    )
                )
                await s.commit()
        except Exception as exc:  # evidence recording must not turn a successful remote call into a failure
            log.warning("ops output artifact not recorded", extra={"tool": tool.value, "error_type": type(exc).__name__})
            return None
        return name

    async def _run_command(self, args: RunCommandArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        run = await self._sandbox_run(
            ToolName.run_command,
            command=args.command,
            cwd_raw=args.cwd,
            timeout_seconds=args.timeout_seconds,
            purpose="tool",
            ctx=ctx,
            events=events,
        )
        text, truncated = self._render_run(args.command, run, budget=self.output_limit, head_ratio=0.3)
        res = run.result
        error_code: str | None = None
        if run.audit.violations:
            error_code = E.COMMAND_SCOPE_VIOLATION
        elif res is None or run.sandbox_failed:
            error_code = E.SANDBOX_ERROR
        elif res.timed_out:
            error_code = E.COMMAND_TIMEOUT
        elif res.exit_code != 0:
            error_code = E.COMMAND_FAILED
        return ToolResult(
            tool=ToolName.run_command,
            ok=error_code is None,
            output=text,
            error_code=error_code,
            truncated=truncated,
            mutated_paths=sorted({c.path for c in run.audit.allowed}),
            data={
                "exit_code": res.exit_code if res else None,
                "timed_out": bool(res and res.timed_out),
                "classification": run.classification.kind,
                "duration_ms": run.duration_ms,
                "violations": [v.to_dict() for v in run.audit.violations[:50]],
                "cleaned": run.audit.cleaned[:50],
            },
        )

    def _check_requested_packages(self, ecosystem: str, manifest: str) -> None:
        """A repository that had no dependency manifest gets third-party packages only when the job asks for one of
        them by name. Live jobs reached for frameworks (FastAPI, Express) for jobs the standard library solves; every
        dependency added install, pin and test-client failures unrelated to the job (analysis 2026-10-16, U2)."""
        job = self.permissions.job_text
        if job is None or (self._base_files is not None and manifest in self._base_files):
            return
        text = self.fs.read_text(manifest, max_bytes=1_000_000).text
        packages = manifest_packages(ecosystem, text)
        if not packages or any(package_mentioned(p, job) for p in packages):
            return
        raise ToolError(
            E.DEPENDENCY_NOT_REQUESTED,
            f"install refused: the job does not ask for any of {', '.join(sorted(packages)[:8])}, and the repository had "
            f"no {manifest}. Solve it with the standard library and the tools the sandbox already has (e.g. http.server, "
            "unittest/pytest, node:http, node --test); remove the manifest and the imports. Only a job that names a "
            "package gets it installed.",
            data={"packages": sorted(packages)[:50], "manifest": manifest},
        )

    def _missing_module_hint(self, output: str) -> str | None:
        """A run that failed on an import of a third-party module: say how it gets into the sandbox (BUG-165)."""
        local = {name.removesuffix(".py") for name in self._local_roots()}
        mods = [m for m in missing_modules(output) if m not in local]
        if not mods:
            return None
        names = ", ".join(f"'{m}'" for m in mods)
        if ToolName.install_dependencies in (self.permissions.allowed_tools or set()):
            return (
                f"[hint] not installed: {names}. Add the package that provides it to requirements.txt / package.json, "
                "then call install_dependencies – reinstalling an unchanged manifest does not add it."
            )
        return f"[hint] not installed: {names}. Without install_dependencies use the standard library, or block_step naming it."

    def _local_roots(self) -> set[str]:
        """Entries at the repository root and under ``src/``: a missing module with such a name is the project's own."""
        names: set[str] = set()
        for base in (self.fs.root, self.fs.root / "src"):
            try:
                names.update(p.name for p in base.iterdir())
            except OSError:
                continue
        return names

    def _install_command(self, args: InstallDependenciesArgs) -> tuple[str, str, str]:
        """``(manifest, command, digest)`` of a dependency install; the runtime builds the command, never the model (D-014)."""
        default = "requirements.txt" if args.ecosystem == "python" else "package.json"
        tf = self.fs.read_text(args.manifest or default, max_bytes=1_000_000)
        manifest = tf.rel
        digest = hashlib.sha256(tf.text.encode("utf-8")).hexdigest()
        if args.ecosystem == "python":
            command = python_install_command(manifest)
            return manifest, command, digest
        if manifest != "package.json":
            raise ToolError(E.ARGS_INVALID, "node dependencies are installed from the package.json at the repository root")
        command = (
            "npm install --ignore-scripts --no-audit --no-fund --no-save --package-lock=false --cache /tmp/npm-cache && npm ls --depth=0"
        )
        return manifest, command, digest

    async def _install_dependencies(self, args: InstallDependenciesArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        manifest, command, digest = await asyncio.to_thread(self._install_command, args)
        await self._load_base()
        self._check_requested_packages(args.ecosystem, manifest)
        if self._installed.get((args.ecosystem, manifest)) == digest:
            raise ToolError(
                E.INSTALL_UNCHANGED,
                f"install refused: {manifest} is unchanged since its last successful install in this attempt, so all its "
                "packages are already available and installing again changes nothing. If a module is still missing, add "
                f"the package that provides it to {manifest} first; any other failure is in the code or the tests.",
                data={"manifest": manifest, "repeated": True},
            )
        run = await self._sandbox_run(
            ToolName.install_dependencies,
            command=command,
            cwd_raw=".",
            timeout_seconds=self.policies.sandbox.egress.install_timeout_seconds,
            purpose="install",
            ctx=ctx,
            events=events,
            egress=True,
            # compiled wheels must match the interpreter of later commands: always the ecosystem's toolchain image
            image=args.ecosystem if args.ecosystem in self.policies.sandbox.images else None,
        )
        text, truncated = self._render_run(command, run, budget=self.output_limit, head_ratio=0.2)
        res = run.result
        error_code: str | None = None
        if run.audit.violations:
            error_code = E.COMMAND_SCOPE_VIOLATION
        elif res is None or run.sandbox_failed:
            error_code = E.SANDBOX_ERROR
        elif res.timed_out:
            error_code = E.COMMAND_TIMEOUT
        elif res.exit_code != 0:
            error_code = E.COMMAND_FAILED
        installed = installed_packages(args.ecosystem, res.stdout if res else "") if error_code is None else []
        if error_code is None:
            self._installed[(args.ecosystem, manifest)] = digest
            # new packages change what commands can do although no workspace file changed: earlier results are stale
            self._ran.clear()
            self._timed_out.clear()
            text += f"\n[dependencies] {len(installed)} package(s) available to later commands and tests of this job"
        elif error_code == E.COMMAND_FAILED and args.ecosystem == "python":
            text += (
                "\n[dependencies] packages the sandbox image already provides (its test toolchain, e.g. pytest and its plugins) "
                "are fixed to the image's versions: relax or remove a conflicting pin in the manifest, then install again"
            )
        return ToolResult(
            tool=ToolName.install_dependencies,
            ok=error_code is None,
            output=text,
            error_code=error_code,
            truncated=truncated,
            data={
                "ecosystem": args.ecosystem,
                "manifest": manifest,
                "packages": installed[:500],
                "exit_code": res.exit_code if res else None,
            },
        )

    async def _run_test(self, args: RunTestArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        self._classify(args.command)  # refuse before announcing the test run
        self._resolve_cwd(args.cwd)
        await self.recorder.emit(
            ctx,
            [
                PendingEvent(
                    EventType.TEST_STARTED,
                    {"command": self._clip_redacted(args.command, 1000), "cwd": args.cwd, "framework": args.framework},
                )
            ],
        )
        run = await self._sandbox_run(
            ToolName.run_test,
            command=args.command,
            cwd_raw=args.cwd,
            timeout_seconds=args.timeout_seconds,
            purpose="test",
            ctx=ctx,
            events=events,
        )
        res = run.result
        combined = f"{strip_ansi(res.stdout)}\n{strip_ansi(res.stderr)}" if res else (run.error or "")
        summary: TestSummary = summarise(
            combined, res.exit_code if res else None, timed_out=bool(res and res.timed_out), framework=args.framework
        )
        if run.sandbox_failed:
            summary = TestSummary(summary.framework, "error", 0, 0, 0, 0, None, f"sandbox error: {run.error}")
        test_event = EventType.TEST_PASSED if summary.status == "passed" else EventType.TEST_FAILED
        await self.recorder.test_run(
            ctx,
            command=args.command,
            framework=summary.framework,
            status=summary.status,
            passed=summary.passed,
            failed=summary.failed,
            errors=summary.errors,
            skipped=summary.skipped,
            output=combined,
            duration_ms=run.duration_ms,
            events=[
                PendingEvent(
                    test_event,
                    {
                        "command": self._clip_redacted(args.command, 1000),
                        "framework": summary.framework,
                        "status": summary.status,
                        "passed": summary.passed,
                        "failed": summary.failed,
                        "errors": summary.errors,
                        "skipped": summary.skipped,
                        "exit_code": summary.exit_code,
                        "note": summary.note,
                    },
                    Severity.info if summary.status == "passed" else Severity.warning,
                )
            ],
        )
        body, truncated = self._render_run(args.command, run, budget=self.output_limit - 200, head_ratio=0.15)
        error_code: str | None = None
        if run.audit.violations:
            error_code = E.COMMAND_SCOPE_VIOLATION
        elif summary.status == "failed":
            error_code = E.TESTS_FAILED
        elif summary.status == "error":
            error_code = E.TEST_ERROR
        return ToolResult(
            tool=ToolName.run_test,
            ok=error_code is None,
            output=f"{summary.line()}\n{body}",
            error_code=error_code,
            truncated=truncated,
            mutated_paths=sorted({c.path for c in run.audit.allowed}),
            data={
                "framework": summary.framework,
                "status": summary.status,
                "passed": summary.passed,
                "failed": summary.failed,
                "errors": summary.errors,
                "skipped": summary.skipped,
                "exit_code": summary.exit_code,
                "timed_out": bool(res and res.timed_out),
                "duration_ms": run.duration_ms,
                "violations": [v.to_dict() for v in run.audit.violations[:50]],
                "cleaned": run.audit.cleaned[:50],
            },
        )

    # ============================================================================================ 17.7-17.9 requests
    async def _request_research(self, args: RequestResearchArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        why = unanswerable_reason(args.question)
        if why:
            raise ToolError(E.RESEARCH_REFUSED, f"research refused: web research cannot answer this – {why}")
        if self._research_requests >= self.permissions.max_research_requests:
            raise ToolError(
                E.RESEARCH_FAILED, f"research request limit ({self.permissions.max_research_requests}) reached for this attempt"
            )
        self._research_requests += 1
        try:
            answer = await self.callbacks.on_research(args.question)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(E.RESEARCH_FAILED, f"research failed: {type(exc).__name__}") from exc
        text, clipped = clip_head(answer.strip() or "(research returned no summary)", self.output_limit)
        return self._ok(ToolName.request_research, text, truncated=clipped, data={"requests_used": self._research_requests})

    async def _request_scope_expansion(self, args: ScopeExpansionRequest, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        for raw in args.paths:
            if self._hidden(raw):
                raise ToolError(E.PATH_FORBIDDEN, f"'{raw}' can never be in scope (git internals or secret file)")
        if self._scope_requests >= self.permissions.max_scope_requests:
            raise ToolError(
                E.SCOPE_EXPANSION_DENIED, f"scope expansion limit ({self.permissions.max_scope_requests}) reached for this attempt"
            )
        self._scope_requests += 1
        try:
            outcome = await self.callbacks.on_scope_expansion(args)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(E.SCOPE_EXPANSION_FAILED, f"scope expansion failed: {type(exc).__name__}") from exc
        if not isinstance(outcome, ScopeExpansionOutcome):
            raise ToolError(E.SCOPE_EXPANSION_FAILED, "scope expansion handler returned an invalid decision")
        swapped = False
        if outcome.granted and outcome.contract is not None:
            current = self._guard.contract.version if self._guard is not None else 0
            if outcome.contract.version >= current:
                policy = self._guard.policy if self._guard is not None else self.policies.scope
                self._guard = ScopeGuard(outcome.contract, policy)
                swapped = True
            else:
                log.warning("ignoring stale scope contract", extra={"current": current, "offered": outcome.contract.version})
        scope = self._scope_view()
        lines = [outcome.message.strip() or ("granted" if outcome.granted else "denied")]
        if scope:
            lines.append(
                f"active scope v{scope['version']}: target_paths={scope['target_paths']} allowed_new_paths={scope['allowed_new_paths']} "
                f"operations={scope['allowed_operations']}"
            )
        data = {"granted": outcome.granted, "scope_swapped": swapped, "scope": scope, **outcome.data}
        if not outcome.granted:
            return ToolResult(
                tool=ToolName.request_scope_expansion, ok=False, output="\n".join(lines), error_code=E.SCOPE_EXPANSION_DENIED, data=data
            )
        return self._ok(ToolName.request_scope_expansion, "\n".join(lines), data=data)

    async def _request_replan(self, args: RequestReplanArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        try:
            await self.callbacks.on_replan(args.reason)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(E.REPLAN_FAILED, f"replan request failed: {type(exc).__name__}") from exc
        return self._ok(ToolName.request_replan, "replan requested; this attempt ends now", terminal=True, data={"reason": args.reason})

    # ============================================================================================ 17.10-17.12 control
    async def _changed_files(self) -> list[str] | None:
        try:
            files = await self.git.changed_files(self.workspace)
        except Exception as exc:
            log.warning("changed_files unavailable", extra={"error_type": type(exc).__name__})
            return None
        return sorted({f for f in files if not self._hidden(f)})

    async def _checkpoint(self, args: CheckpointArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        if ctx.step_id is None:
            raise ToolError(E.STEP_REQUIRED, "checkpoint needs a step context")
        changed = await self._changed_files()
        checkpoint = {
            "notes": args.notes,
            "progress": args.progress,
            "next_actions": args.next_actions,
            "turn": ctx.turn,
            "attempt_id": str(ctx.attempt_id) if ctx.attempt_id else None,
            "tool_call_id": str(ctx.tool_call_id),
            "changed_files": changed or [],
            "scope_version": self._guard.contract.version if self._guard else None,
            "created_at": datetime.now(UTC).isoformat(),
        }
        event = PendingEvent(
            EventType.CHECKPOINT_CREATED,
            {
                "progress": args.progress,
                "notes": self._clip_redacted(args.notes, 500),
                "next_actions": len(args.next_actions),
                "changed_files": len(changed or []),
            },
        )
        if not await self.recorder.save_checkpoint(ctx, checkpoint, [event]):
            raise ToolError(E.STEP_NOT_FOUND, f"step {ctx.step_id} does not exist")
        progress = f" ({args.progress}%)" if args.progress is not None else ""
        return self._ok(ToolName.checkpoint, f"checkpoint saved{progress}", data={"checkpoint": checkpoint})

    async def _complete_step(self, args: CompletionReport, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        reported: list[str] = []
        for raw in args.changed_files:
            rel = canonical_path(raw)
            if rel != "." and rel not in reported:
                reported.append(rel)
        actual = await self._changed_files()
        data: dict[str, Any] = {"report": args.model_dump() | {"changed_files": reported}, "actual_changed_files": actual}
        lines = ["step completion reported; the runtime now verifies the acceptance criteria"]
        if actual is not None:
            unreported = sorted(set(actual) - set(reported))
            missing = sorted(set(reported) - set(actual))
            data |= {"unreported_changes": unreported, "reported_but_unchanged": missing}
            if unreported:
                lines.append("note: changed but not reported: " + ", ".join(unreported[:20]))
            if missing:
                lines.append("note: reported but unchanged: " + ", ".join(missing[:20]))
        return self._ok(ToolName.complete_step, "\n".join(lines), terminal=True, data=data)

    async def _block_step(self, args: BlockReport, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        return self._ok(
            ToolName.block_step,
            f"step blocked ({args.reason_code}); the runtime decides how to continue",
            terminal=True,
            data={"report": args.model_dump()},
        )


def _occurrence_lines(text: str, needle: str) -> list[int]:
    lines: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            return lines
        lines.append(text.count("\n", 0, idx) + 1)
        start = idx + len(needle)
