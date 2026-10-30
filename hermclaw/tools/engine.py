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
import difflib
import functools
import hashlib
import json
import re
import shlex
import stat
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import Operation, ScopeExpansionRequest
from hermclaw.contracts.tools import (
    MUTATING_TOOLS,
    OPS_TOOLS,
    SALVAGE_DECISION,
    TERMINAL_TOOLS,
    BlockReport,
    CoderAction,
    CompletionReport,
    ToolName,
    ToolResult,
)
from hermclaw.contracts.worker import CommandResult
from hermclaw.core.config import PoliciesConfig
from hermclaw.core.interfaces import CommandExecutor, ExecutionRequest, GitReader, RepoContextProvider, RepoHit, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED, Redactor
from hermclaw.memory.store import FAILURE_SCORE_MARGIN, MemoryHit, failure_matches
from hermclaw.models.protocols import CallContext as ModelCallContext
from hermclaw.research.questions import unanswerable_reason
from hermclaw.scope.expansion import is_test_path
from hermclaw.scope.guard import ScopeGuard
from hermclaw.stagnation.fingerprints import key_error_lines, normalise_text
from hermclaw.tools import errors as E
from hermclaw.tools.classify import CommandClassification, CommandClassifier, mask_scratch_removals, package_install
from hermclaw.tools.context import CallContext, NullCallbacks, ScopeExpansionOutcome, ToolCallbacks, ToolPermissions
from hermclaw.tools.errors import ToolError, ToolProtocolError
from hermclaw.tools.executors import LocalSandboxExecutor
from hermclaw.tools.gitlocal import LocalGit
from hermclaw.tools.locations import source_locations
from hermclaw.tools.ops import OpsCall, OpsResult, OpsToolProvider
from hermclaw.tools.output import clip_head, clip_middle, missing_modules, strip_ansi
from hermclaw.tools.patch import diff_section_path, parse_numstat, parse_patch, rebuild_new_files
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
    RecallArgs,
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
from hermclaw.tools.remote import RemoteSandboxExecutor
from hermclaw.tools.shellread import ShellRead, core_command, shell_read
from hermclaw.tools.snapshot import DEFAULT_GENERATED_GLOBS, AuditOutcome, Decide, WorkspaceTracker
from hermclaw.tools.testparse import TestSummary, parse_counts, summarise
from hermclaw.tools.workspace import (
    WorkspaceFS,
    canonical_path,
    filter_listing,
    immediate_children,
    is_binary,
    regex_is_risky,
    search_files,
)

if TYPE_CHECKING:
    from hermclaw.memory.store import MemoryStore
    from hermclaw.verifier.secrets import SecretFinding

log = get_logger(__name__)

#: install target of Python dependencies inside the sandbox (``worker.execution.sandbox.PYTHON_DEPS_DIR``)
TIMEOUT_HINT = (
    "Commands must exit on their own: a server, watcher or REPL never does. Do not start one in the foreground or "
    "in the background; a test starts the app in-process (port 0) and closes it, or calls the handler directly."
)
# only where the shell knows the option: in a shell without it (dash of Debian bookworm, the Node image) 'set -o pipefail'
# is a failing special builtin and ends the whole script with exit code 2 and no output (live K2, BUG-275)
PIPEFAIL = "(set -o pipefail) 2>/dev/null && set -o pipefail"

READ_REPEAT_WINDOW = 6  # turns; the context shows the files of the last 6 reads (context_builder ``_read_spans``)
READ_VISIBLE_LINES = 400  # the context shows at most this many lines of one read (context_builder ``max_snippet_lines``)
READ_FOLLOW_MIN_LINES = 60  # lines after a repeated read that the coder is given instead of a refusal (BUG-342)
#: files whose size is not a design choice: data, documentation, lock files – everything else gets the split hint
_NOT_SPLIT = (".md", ".txt", ".rst", ".json", ".jsonl", ".csv", ".tsv", ".lock", ".toml", ".yaml", ".yml", ".xml", ".svg", ".ini", ".cfg")
PARSE_FAILURES_SHOWN = 3  # files that do not parse, named after a write or a failed command (BUG-343)
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
# refusals that tell nothing about how to act: the step is over, the budget spent, the knowledge itself unavailable
_NO_KNOWLEDGE = frozenset({E.STEP_FINISHED, E.TURN_BUDGET_EXHAUSTED, E.MEMORY_UNAVAILABLE})
_FAILURE_QUERY_CHARS = 600
_SOURCE_FILE_BYTES = 2_000_000  # larger files are not read for [source] lines


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
    failed_tests: int = 0  # failed tests and errors the output reports although the exit code is 0 (BUG-329)

    @property
    def sandbox_failed(self) -> bool:
        """No result, or the sandbox could not run the command at all (no exit code, not a timeout)."""
        return self.result is None or self.result.did_not_run


Handler = Callable[[Any, CallContext, list[PendingEvent]], Awaitable[ToolResult]]


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# A test that writes its output into the repository is refused on every run: the file is outside the step's paths
# and is removed again. The coder of ladder A9 #4 (rc73) ran its Pester tests five times against `test_output.json`
# without learning where such files belong (BUG-324).
FAILED_TESTS_HINT = (
    "[hint] The exit code is 0, but the output reports {n} failed test(s): this runner does not fail on failing tests "
    "(Invoke-Pester needs -CI; run_test reads the counts). The run counts as failed – fix the failures it names."
)
TEMP_FILES_HINT = (
    "[scope] the command created files outside the step's paths; they were removed and the run is refused while it "
    "does that. Tests write temporary files to a temporary directory, never into the repository: pytest tmp_path, "
    "tempfile.TemporaryDirectory(), Pester $TestDrive, os.tmpdir() in Node, t.TempDir() in Go."
)


def violation_lines(violations: Sequence[Any]) -> list[str]:
    """The ``[scope]`` lines of a run's out-of-scope changes, with the temporary-file hint when it created files."""
    lines = [f"[scope] {v.operation} {v.path}: {v.reason} ({'reverted' if v.reverted else 'NOT reverted'})" for v in violations[:20]]
    if any(v.operation == "create" for v in violations):
        lines.append(TEMP_FILES_HINT)
    return lines


# a path under /tmp that the failed command uses and its output reports missing (BUG-384)
_TMP_MISSING = re.compile(r"(/tmp/[\w.+-]+)[^\n]{0,160}?(?:No such file|not a directory|does not exist|cannot access|not found)", re.I)
TMP_GONE_HINT = (
    "[hint] {path} is gone: every command runs in a new sandbox whose /tmp starts empty, so a build directory or file "
    "an earlier command put under /tmp is not there any more. Create and use it in the same command, e.g. "
    "'cmake -S . -B /tmp/b && cmake --build /tmp/b && ctest --test-dir /tmp/b'."
)


def _tmp_gone(command: str, output: str) -> str | None:
    """The /tmp path a failed command expected from an earlier one: live R3-cpp and a C++ kassenbuch job built into
    /tmp/build and ran the build or tests from it in the next command, where the fresh sandbox had no /tmp/build
    ('/tmp/build is not a directory'), and spent a turn each time (BUG-384)."""
    for m in _TMP_MISSING.finditer(output):
        path = m.group(1).rstrip(".")
        root = "/".join(path.split("/")[:3])
        if root in command:
            return root
    return None

def _isolated(executor: object) -> bool:
    """Whether commands run in a sandbox with a /tmp of its own: never the unisolated subprocess fallback of dev/tests."""
    while hasattr(executor, "inner"):
        executor = executor.inner
    if isinstance(executor, LocalSandboxExecutor):
        return executor.mode == "sandbox"
    return isinstance(executor, RemoteSandboxExecutor)


# an overwrite of a file this long that changes at most this share of its lines gets the replace_text note
_REWRITE_MIN_LINES = 150
_REWRITE_SHARE = 0.15


def _rewritten_for_little(old: str | None, new: str) -> int | None:
    """The changed lines of an overwrite that repeats a long file to change a few of them, else None: ladder R3 wrote a
    570-line Main.java in full ten times in 21 turns to change a few lines each, ran its tests twice, and two rewrites
    of main.rs broke off at the output limit (BUG-383)."""
    if old is None:
        return None
    a, b = old.splitlines(), new.splitlines()
    if len(a) < _REWRITE_MIN_LINES:
        return None
    blocks = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in blocks if tag != "equal")
    return changed if 0 < changed <= len(a) * _REWRITE_SHARE else None


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
        memory: MemoryStore | None = None,
        memory_scopes: list[str] | None = None,
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
        self.memory = memory  # Hermclaw's knowledge (D-019); without it the recall tool is not offered
        self.memory_scopes = memory_scopes
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
        self._base_tests: dict[str, set[str]] = {}
        self._created: set[str] = set()
        # commands that ran into their timeout, with the file-change count at that time: the same command on an
        # unchanged workspace would only block for the full timeout again (BUG-164)
        self._writes = 0
        self._timed_out: dict[tuple[str, str, int], int] = {}
        # every finished command: (file-change count, turn, outcome). The coder gets a fresh context each turn and
        # re-ran identical commands on unchanged files again and again (analysis 2026-10-16, live job 50784df5)
        # keyed without the timeout: a command that finished is not changed by another timeout (BUG-211)
        self._ran: dict[tuple[str, str], tuple[int, int, str]] = {}
        # the same runs keyed by their core (``core_command``): one re-run with another slice of its output (tail, head,
        # grep, -v) is allowed per file-change count, the next is refused like an identical command (BUG-344)
        self._ran_core: dict[tuple[str, str], tuple[int, int, str]] = {}
        self._sliced: dict[tuple[str, str], int] = {}
        self._hinted: set[tuple[str, str]] = set()  # (tool, error code) whose knowledge this attempt was given (D-019)
        self._followed: dict[str, int] = {}  # read_range spans answered with the lines after them (BUG-342), by write count
        self._code_entries: list[tuple[MemoryHit, list[re.Pattern[str]]]] | None = None  # D-027, loaded at the first write
        self._shown: set[uuid.UUID] = set()  # knowledge entries this attempt was given with a refusal or failed run
        self._looked_up: set[str] = set()  # error lines of failed runs already looked up in the knowledge
        self._ran_output: dict[tuple[str, str], str] = {}  # rendered output of a failed run, repeated in its refusal
        # identical reads: (file-change count, turn). The context of every turn already shows the files read in this
        # attempt; the coder still re-read the same unchanged file turn after turn instead of acting (BUG-179)
        self._reads: dict[tuple[str, str], tuple[int, int, bool]] = {}  # (writes, turn, fully visible)
        # the file lines this turn's context shows, by path ([(start, end)]); ``None``: no context told (engine alone)
        self._context: dict[str, list[tuple[int, int]]] | None = None
        # a clipped read_file: (file-change count, turn, next line). Reading the same unchanged file again continues
        # there – the coder re-read the first 300 lines of an 845-line file four times until the stagnation stop
        # (live G1, BUG-264)
        self._read_pages: dict[str, tuple[int, int, int]] = {}
        # manifest digest of the last successful install per (ecosystem, manifest): reinstalling it changes nothing
        self._installed: dict[tuple[str, str], str] = {}
        # files the correction of this attempt names, and the files this attempt changed: complete_step is refused
        # once while a named file is untouched (BUG-258)
        self._correction_paths: list[str] = []
        self._recheck_commands: list[str] = []  # failed lint/test checks a correction must resolve (BUG-276)
        self._recheck_secrets: list[str] = []  # files whose secret check failed (BUG-279)
        self._changed_now: set[str] = set()
        self._correction_reminded = False
        # test runs of this attempt whose last result failed, by core, and the step's acceptance test commands:
        # complete_step re-runs them and refuses once while one fails (BUG-349, BUG-351)
        self._failed_tests: dict[tuple[str, str], str] = {}
        self._acceptance_tests: list[str] = []
        self._tests_reminded = False
        self._granted_turns = 0  # turns granted once beyond the budget when complete_step found a failing check
        self._handlers: dict[ToolName, Handler] = {
            ToolName.list_files: self._list_files,
            ToolName.read_file: self._read_file,
            ToolName.read_range: self._read_range,
            ToolName.find_text: self._find_text,
            ToolName.search_repo: self._search_repo,
            ToolName.search_symbol: self._search_symbol,
            ToolName.recall: self._recall,
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
        return (self.permissions.max_turns or self.policies.coder.max_turns) + self._granted_turns

    @property
    def granted_turns(self) -> int:
        """Turns granted beyond the budget (BUG-349); the loop runs them."""
        return self._granted_turns

    def _provided(self, tool: ToolName) -> bool:
        if tool is ToolName.recall:
            return self.memory is not None
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
            result = await self._with_parse_failures(self._finalise(result), action.args)
            result = await self._with_code_knowledge(await self._with_failure_knowledge(result, ctx), action.args)
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
            result = await self._handlers[spec.name](args, ctx, events)
            if result.ok and spec.name in MUTATING_TOOLS and result.mutated_paths and action.decision != SALVAGE_DECISION:
                result = await self._lint_after_write(result, ctx, events)
            return result
        except ToolError as exc:
            refused = ToolResult(tool=spec.name, ok=False, output=exc.message, error_code=exc.code, data=exc.data)
            return await self._with_knowledge(refused, ctx)
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

    async def _base_test_names(self, rel: str) -> set[str]:
        """The tests ``rel`` defines at the workspace base: only those count as existing. Measured against the previous
        write, the coder's own new test file swung between two versions for four turns – each rewrite fixed a lint
        finding and dropped a test the one before had added, and the note asked for it back (ladder A10 #5, BUG-337)."""
        if rel not in self._base_tests:
            await self._load_base()
            data = await self.lgit.file_at(self.workspace.base_sha, rel) if self._base_files and rel in self._base_files else None
            self._base_tests[rel] = _test_names(data.decode("utf-8", errors="replace")) if data and not is_binary(data) else set()
        return self._base_tests[rel]

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
                "written back. To replace a masked secret use replace_text: 'old' is the line as shown (marker included), "
                "'new' the line with a placeholder value instead.",
            )

    def _restore_masked(self, new_text: str, existing: str | None) -> tuple[str, int]:
        """``new_text`` with each line that is the masked view of a line of ``existing`` (indentation aside) given that
        line's real content back, and the number of such lines. The coder rewrote whole files from what it had read;
        every line a read showed masked came back with the marker, and each write was refused (live G1 rc90, BUG-355).
        A view that two different lines share stays masked."""
        if REDACTED not in new_text or not existing or REDACTED in existing:
            return new_text, 0
        views: dict[str, str | None] = {}
        for line in existing.splitlines():
            view = self.redactor.text(line).strip()
            if REDACTED in view:
                real = line.strip()
                views[view] = real if views.get(view, real) == real else None
        out: list[str] = []
        restored = 0
        for line in new_text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            original = views.get(body.strip()) if REDACTED in body else None
            if original is None:
                out.append(line)
                continue
            indent = body[: len(body) - len(body.lstrip())]
            out.append(indent + original + line[len(body) :])
            restored += 1
        return "".join(out), restored

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
            self._changed_now.update(str(c["path"]) for c in changes if isinstance(c, dict) and c.get("path"))
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

    def _covering_read(self, path: str, start: int, end: int, turn: int) -> tuple[str, int] | None:
        """A fully visible read of ``path`` in the repeat window, with no file changed since, that already contains lines
        ``start``-``end`` – re-reading a part of it only costs a turn (live G1: 100-160, then 110-160, then 110-145, BUG-244)."""
        if not self._in_context(path, start, end):
            return None
        for (tool, key), (writes, at, visible) in self._reads.items():
            if writes != self._writes or turn - at > READ_REPEAT_WINDOW or not visible:
                continue
            if tool == ToolName.read_file.value and key == path:
                return "the whole file", at
            rpath, _, span = key.rpartition(":")
            lo, _, hi = span.partition("-")
            inside = lo.isdigit() and hi.isdigit() and int(lo) <= start and end <= int(hi)
            if tool == ToolName.read_range.value and rpath == path and inside:
                return f"lines {lo}-{hi}", at
        return None

    def context_shows(self, spans: Iterable[tuple[str, int, int]]) -> None:
        """The file parts the context of the coming turn shows in full (the loop sets them every turn). A read the
        context clipped is no repeat: the guards refused lines 247-338 of proxmox.py as "shown above" while RELEVANT
        CODE showed the file's head only, and the coder spent nine turns rebuilding its own signatures (live G1 rc91,
        BUG-362)."""
        shown: dict[str, list[tuple[int, int]]] = {}
        for path, start, end in spans:
            shown.setdefault(path, []).append((start, end))
        self._context = shown

    def _in_context(self, path: str, start: int, end: int) -> bool:
        if self._context is None:
            return True
        return any(lo <= start and end <= hi for lo, hi in self._context.get(path, ()))

    def _check_read_repeat(self, tool: ToolName, key: str, turn: int, end: int | None = None) -> None:
        """Refuse a read identical to one of the last ``READ_REPEAT_WINDOW`` turns while no file changed: its content is
        already in this turn's context (files read in this attempt), so reading again only costs a turn. A read that was
        clipped or longer than the context shows is allowed again: refusing it claimed content the coder never saw, and
        the coder circled on refused reads of a 624-line file (live G1, BUG-229)."""
        previous = self._reads.get((tool.value, key))
        if previous is None or previous[0] != self._writes or turn - previous[1] > READ_REPEAT_WINDOW or not previous[2]:
            return
        path, _, span = key.rpartition(":") if tool == ToolName.read_range else (key, "", f"1-{end or 1}")
        lo, _, hi = span.partition("-")
        if lo.isdigit() and hi.isdigit() and not self._in_context(path, int(lo), int(hi)):
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
        tf = await asyncio.to_thread(self.fs.read_text, args.path, max_bytes=self.max_read_bytes)
        lines = tf.text.count("\n") + (1 if tf.text and not tf.text.endswith("\n") else 0)
        self._check_read_repeat(ToolName.read_file, key, ctx.turn, end=lines)
        page = self._read_pages.pop(key, None)
        first = page[2] if page is not None and page[0] == self._writes and page[2] <= lines else 1
        body = "".join(tf.text.splitlines(keepends=True)[first - 1 :]) if first > 1 else tf.text
        text, clipped = clip_head(body, self.output_limit)
        shown = text.rsplit("\n…[truncated:", 1)[0].count("\n") + 1 if clipped else lines - first + 1
        last = first + shown - 1
        self._reads[(ToolName.read_file.value, key)] = (self._writes, ctx.turn, first == 1 and not clipped and lines <= READ_VISIBLE_LINES)
        if first > 1:
            assert page is not None
            text = f"[{tf.rel}: lines {first}-{last} of {lines}, continuing your read of turn {page[1]}]\n" + text
        elif clipped:  # every page names its span: the next turn's context shows the read from it (BUG-266)
            text = f"[{tf.rel}: lines {first}-{last} of {lines}]\n" + text
        if clipped:
            self._read_pages[key] = (self._writes, ctx.turn, last + 1)
            text += (
                f"\n[{tf.rel} has {lines} lines; lines {first}-{last} shown. read_file again shows lines {last + 1}-…; "
                "read_range(start, end) shows a specific part]"
            )
        if not tf.text:
            text = "(empty file)"
        data = {"path": tf.rel, "bytes": tf.size, "lines": lines, "first": first, "last": last}
        return self._ok(ToolName.read_file, text, truncated=clipped, data=data)

    async def _read_range(self, args: ReadRangeArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        path = self.fs.resolve(args.path)[0]
        key = f"{path}:{args.start}-{args.end}"
        try:
            self._check_read_repeat(ToolName.read_range, key, ctx.turn)
            repeated: ToolError | None = None
        except ToolError as exc:
            repeated = exc
        rel, lines, total = await asyncio.to_thread(self.fs.read_lines, args.path, args.start, args.end)
        if args.start > total > 0:
            raise ToolError(E.RANGE_INVALID, f"'{rel}' has only {total} lines (start={args.start})", data={"total_lines": total})
        if total == 0:
            self._reads[(ToolName.read_range.value, key)] = (self._writes, ctx.turn, True)
            return self._ok(ToolName.read_range, "(empty file)", data={"path": rel, "start": 0, "end": 0, "total_lines": 0})
        end = min(args.end, total)
        covering = None if repeated is not None else self._covering_read(path, args.start, end, ctx.turn)
        if repeated is not None or covering is not None:
            following = await self._following_lines(args, path, rel, end=end, total=total, turn=ctx.turn)
            if following is not None:
                return following
            if repeated is not None:
                raise repeated
            assert covering is not None
            raise ToolError(
                E.READ_REPEATED,
                f"read refused: lines {args.start}-{end} are part of what you read in turn {covering[1]} ({covering[0]}) "
                "and no file changed since; that content is shown above among the files you read. Act on it now: edit the "
                "code or the test, run a different check, or call complete_step/block_step.",
                data={"previous_turn": covering[1], "repeated": True},
            )
        width = len(str(end))
        body = "\n".join(f"{n:>{width}}| {line}" for n, line in enumerate(lines, start=args.start))
        text, clipped = clip_head(body, self.output_limit)
        visible = not clipped and end - args.start + 1 <= READ_VISIBLE_LINES
        self._reads[(ToolName.read_range.value, key)] = (self._writes, ctx.turn, visible)
        if end < args.end:
            text += f"\n[end of file: {total} lines]"
        return self._ok(
            ToolName.read_range, text, truncated=clipped, data={"path": rel, "start": args.start, "end": end, "total_lines": total}
        )

    async def _following_lines(self, args: ReadRangeArgs, path: str, rel: str, *, end: int, total: int, turn: int) -> ToolResult | None:
        """The lines after a read the coder already has, instead of refusing it again: the method it looked for ran past
        the 160 lines the context showed of app.py, and the coder asked for lines 100-160 six times across two attempts
        until both stagnated (ladder A10 #8, BUG-342). ``None`` when the file ends there or the lines after it were read
        already; the header names the span, so the next turn's context shows it (as for pages of read_file)."""
        asked = f"{path}:{args.start}-{end}"
        if self._followed.get(asked) == self._writes:  # once per range: a loop on it must not page through the file
            return None
        start = end + 1
        while start <= total and (covering := self._covering_read(path, start, start, turn)) is not None:
            if not covering[0].startswith("lines "):  # the whole file is shown
                return None
            start = int(covering[0].rpartition("-")[2]) + 1  # past a visible read that reaches further
        stop = min(total, start - 1 + max(READ_FOLLOW_MIN_LINES, end - args.start + 1), start - 1 + READ_VISIBLE_LINES)
        previous = self._reads.get((ToolName.read_range.value, f"{path}:{start}-{stop}"))
        if start > total or (previous is not None and previous[0] == self._writes):
            return None
        key = f"{path}:{start}-{stop}"
        _, lines, _ = await asyncio.to_thread(self.fs.read_lines, args.path, start, stop)
        width = len(str(stop))
        body = "\n".join(f"{n:>{width}}| {line}" for n, line in enumerate(lines, start=start))
        text, clipped = clip_head(body, self.output_limit)
        self._reads[(ToolName.read_range.value, key)] = (self._writes, turn, not clipped)
        self._followed[asked] = self._writes
        head = f"[{rel}: lines {start}-{stop} of {total}, after lines {args.start}-{end}, which are shown above unchanged]\n"
        tail = f"\n[end of file: {total} lines]" if stop == total else ""
        return self._ok(
            ToolName.read_range,
            head + text + tail,
            truncated=clipped,
            data={"path": rel, "start": start, "end": stop, "total_lines": total, "following": True},
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
        if clipped:
            # the coder raised max_results from 200 to 250 and 300 for the same pattern; the output limit had cut the
            # matches, so each repeat showed the same lines (live G1 rc90, BUG-360)
            shown = text.rsplit("\n…[truncated:", 1)[0].count("\n") + 1
            text = text.rsplit("\n…[truncated:", 1)[0] + (
                f"\n…[{len(outcome.lines) - shown} more matching line(s) not shown: the output limit is reached, a larger "
                "max_results shows no more – narrow 'path', 'glob' or the pattern]"
            )
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

    async def _with_knowledge(self, result: ToolResult, ctx: CallContext) -> ToolResult:
        """What earlier jobs learned about this refusal, added to it once per tool and error code (D-019): the entry
        recorded for exactly this refusal first, then the closest lessons. Models rarely call recall on their own when
        a tool refuses – GLM-4.7-Flash spent a whole A3 run against replace_text (BUG-301)."""
        code, want = result.error_code, self.policies.memory.coder_entries
        key = (result.tool.value, code or "")
        if self.memory is None or not code or want <= 0 or code in _NO_KNOWLEDGE or key in self._hinted:
            return result
        self._hinted.add(key)
        call = ModelCallContext(purpose="memory", job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id)
        try:
            hits = await self.memory.for_signature(f"tool:{key[0]}:{code}", scopes=self.memory_scopes)
            if len(hits) < want:
                query = f"{key[0]} {code} {result.output[:300]}"
                near = await self.memory.search(
                    query, k=want, scopes=self.memory_scopes, kinds=("lesson", "tool_bug", "incident", "convention"), ctx=call
                )
                hits += [h for h in near if h.id not in {x.id for x in hits}]
        except Exception as exc:  # the refusal itself still reaches the model
            log.warning("knowledge for a refusal unavailable", extra={"error_type": type(exc).__name__})
            return result
        if not hits:
            return result
        self._shown.update(h.id for h in hits[:want])
        limit = self.policies.memory.max_entry_chars
        note = "\n\nHermclaw's knowledge about this refusal (from earlier jobs; the files and outputs here decide):\n" + "\n\n".join(
            h.render(limit) for h in hits[:want]
        )
        return result.model_copy(
            update={"output": result.output + note, "data": result.data | {"knowledge": [str(h.id) for h in hits[:want]]}}
        )

    async def _with_parse_failures(self, result: ToolResult, args: Mapping[str, Any]) -> ToolResult:
        """Files that do not parse, named with the line the parser points at (BUG-343): a write of ``db.py`` left an
        unclosed string literal; the lint note after it came while the coder was writing seven more files, and later it
        read it 22 times in four attempts without seeing the quote that was missing. A read of such a
        file says so, as does a write of another file and a failed command while a changed file does not parse."""
        tool = result.tool
        if tool in (ToolName.read_file, ToolName.read_range) and (result.ok or result.error_code == E.READ_REPEATED):
            try:
                paths = [self.fs.resolve(str(args.get("path", "")))[0]]
            except ToolError:
                return result
            head = "this file does not parse as it is now – fix this first, the lines read show it as it is:"
        elif (tool in MUTATING_TOOLS and result.ok) or (tool in (ToolName.run_command, ToolName.run_test) and not result.ok):
            done = set(result.mutated_paths or [])  # the lint after the write names their findings
            paths = [f for f in await self._changed_files() or [] if f not in done]
            head = "changed files that do not parse as they are now – nothing that loads them can run; fix them first:"
        else:
            return result
        found = [f for rel in paths if (f := await asyncio.to_thread(self._parse_failure, rel))]
        if not found:
            return result
        note = self.redactor.text("\n\n" + head + "\n" + "\n".join(found[:PARSE_FAILURES_SHOWN]))
        return result.model_copy(update={"output": result.output + note, "data": result.data | {"parse_failures": len(found)}})

    def _parse_failure(self, rel: str) -> str | None:
        """The verifier's in-process parser on one file (Python, JSON, YAML, TOML): its message and the line it names."""
        from hermclaw.verifier.checks import MAX_SYNTAX_FILE_BYTES
        from hermclaw.verifier.languages import detect_language
        from hermclaw.verifier.syntax import IN_PROCESS

        path = self.fs.root / rel
        try:
            if self._hidden(rel) or not path.is_file() or path.stat().st_size > MAX_SYNTAX_FILE_BYTES:
                return None
            data = path.read_bytes()
        except OSError:
            return None
        language = detect_language(rel, data[:256])
        if language not in IN_PROCESS:
            return None
        text = data.decode("utf-8", errors="replace")
        checked = IN_PROCESS[language](rel, text)
        if checked.status != "fail":
            return None
        line = checked.evidence.get("line")
        lines = text.splitlines()
        if not isinstance(line, int) or not 0 < line <= len(lines):
            return f"{rel}: {checked.message}"
        return f"{rel}:{line}: {checked.message}\n{line:>5}| {lines[line - 1][:200]}"

    async def _with_code_knowledge(self, result: ToolResult, args: Mapping[str, Any]) -> ToolResult:
        """Lessons and conventions whose code patterns (tags ``code:<regex>``) match what a write just put into a file,
        each entry once per attempt (D-027): the convention on SQL LIKE with user text scored 0.25 against the step
        goal, stayed below the recall threshold, and the delivered search treated "100%" as a wildcard (ladder A10 #4
        and #7)."""
        want = self.policies.memory.coder_entries
        if self.memory is None or want <= 0 or not result.ok or not result.mutated_paths:
            return result
        written = _written_text(result.tool, args)
        if not written.strip():
            return result
        if self._code_entries is None:
            try:
                self._code_entries = await self.memory.code_entries(scopes=self.memory_scopes)
            except Exception as exc:  # the write itself stands
                log.warning("knowledge for written code unavailable", extra={"error_type": type(exc).__name__})
                self._code_entries = []
        fresh = [h for h, patterns in self._code_entries if h.id not in self._shown and any(p.search(written) for p in patterns)]
        fresh = fresh[:want]
        if not fresh:
            return result
        self._shown.update(h.id for h in fresh)
        try:
            await self.memory.count_recalls([h.id for h in fresh])
        except Exception as exc:
            log.warning("recall count failed", extra={"error_type": type(exc).__name__})
        limit = self.policies.memory.max_entry_chars
        note = (
            "\n\nHermclaw's knowledge about code like the code just written (from earlier jobs; the files and outputs here "
            "decide):\n" + "\n\n".join(h.render(limit) for h in fresh)
        )
        return result.model_copy(update={"output": result.output + note, "data": result.data | {"knowledge": [str(h.id) for h in fresh]}})

    async def _with_failure_knowledge(self, result: ToolResult, ctx: CallContext) -> ToolResult:
        """Lessons matching the error lines of a command or test run that failed, each entry once per attempt (D-019).
        The knowledge reached the coder only with a refused tool call; a Pester run failing on a missing Windows cmdlet
        (tournament A9) or a server crashing on an import never brought the lesson recorded for it. Added after the
        output was clipped, so a long log does not cut the note off; ``coder_entries`` bounds its size."""
        want = self.policies.memory.coder_entries
        if self.memory is None or want <= 0 or result.error_code not in E.RUN_FAILED:
            return result
        norm = normalise_text(strip_ansi(result.output))
        lines = key_error_lines(norm) or norm.split("\n")[-8:]
        query = "\n".join(lines)[:_FAILURE_QUERY_CHARS].strip()
        if not query or query in self._looked_up:
            return result
        self._looked_up.add(query)
        call = ModelCallContext(purpose="memory", job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id)
        min_score = self.policies.memory.min_score
        try:
            hits = await self.memory.search(
                query,
                k=want + len(self._shown),
                scopes=self.memory_scopes,
                kinds=("lesson", "convention", "research"),
                min_score=max(0.0, min_score - FAILURE_SCORE_MARGIN),
                count=False,
                ctx=call,
            )
            fresh = [h for h in failure_matches(hits, query, min_score) if h.id not in self._shown][:want]
            await self.memory.count_recalls([h.id for h in fresh])
        except Exception as exc:  # the failed run itself still reaches the model
            log.warning("knowledge for a failed run unavailable", extra={"error_type": type(exc).__name__})
            return result
        if not fresh:
            return result
        self._shown.update(h.id for h in fresh)
        limit = self.policies.memory.max_entry_chars
        note = "\n\nHermclaw's knowledge that matches this failure (from earlier jobs; the files and outputs here decide):\n" + (
            "\n\n".join(h.render(limit) for h in fresh)
        )
        return result.model_copy(update={"output": result.output + note, "data": result.data | {"knowledge": [str(h.id) for h in fresh]}})

    async def _recall(self, args: RecallArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        """Active knowledge entries closest to the query (D-019)."""
        if self.memory is None:
            raise ToolError(E.MEMORY_UNAVAILABLE, "recall is not available in this installation")
        call = ModelCallContext(purpose="memory", job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id)
        try:
            hits = await self.memory.search(args.query, k=args.k, scopes=self.memory_scopes, ctx=call)
        except Exception as exc:
            raise ToolError(E.MEMORY_UNAVAILABLE, f"the knowledge store is unavailable: {type(exc).__name__}") from exc
        if not hits:
            return self._ok(
                ToolName.recall,
                "no knowledge entry matches this query; go on with the task (another query with the exact error code or "
                "tool name may find one)",
                data={"hits": 0},
            )
        limit = self.policies.memory.max_entry_chars
        text = "\n\n".join(f"{n}. {h.render(limit)}\n(relevance {h.score:.2f})" for n, h in enumerate(hits, start=1))
        out, clipped = clip_head(text, self.output_limit)
        return self._ok(ToolName.recall, out, truncated=clipped, data={"hits": len(hits), "ids": [str(h.id) for h in hits]})

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
        content, restored = self._restore_masked(args.content, old_text)
        self._check_placeholder(content, old_text)
        data = content.encode("utf-8")
        if args.append and old is not None:
            # large files are written in parts: one answer has an output token limit (BUG-223)
            if old_text is None:
                raise ToolError(E.ARGS_INVALID, f"'{rel}' is a binary file; append works on text files only")
            content = old_text + ("" if not old_text or old_text.endswith("\n") else "\n") + content
            data = content.encode("utf-8")
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        if old == data:
            # a refused no-op, not a success: the coder rewrote the same start script four times in a row while the
            # review findings stayed open, and the "success" cleared LATEST FAILURE each time (live G1, BUG-253)
            raise ToolError(
                E.NO_CHANGE,
                f"write refused: {rel} already has exactly this content, so writing it again changes nothing. If the "
                "correction or LATEST FAILURE asks for a change in this file, change those lines (replace_text) to "
                "different content; if the file is complete, run its check or call complete_step.",
                data={"path": rel, "changed": False, "repeated": True},
            )
        mode = stat.S_IMODE(target.stat().st_mode) if exists else None
        await asyncio.to_thread(self.fs.atomic_write, target, data, mode=mode)
        self._note_created([(rel, op)])
        self._file_changed(ToolName.write_file, [{"path": rel, "operation": op, "bytes": len(data)}], events)
        verb = "created" if op == "create" else ("appended to" if args.append else "overwrote")
        lost = sorted(await self._base_test_names(rel) - _test_names(content)) if exists and not args.append and is_test_path(rel) else []
        note = (
            f"\nnote: this overwrite removed {len(lost)} test(s) the file had: {', '.join(lost[:12])}"
            f"{' …' if len(lost) > 12 else ''}. Existing tests are kept unless the goal asks to remove them – add them "
            "back (write the whole file again with them, or append them)."
            if lost
            else ""
        )
        if restored:
            note += f"\nnote: {restored} masked line(s) you copied unchanged keep their real value"
        rewritten = _rewritten_for_little(old_text, content) if exists and not args.append else None
        if rewritten is not None:
            note += (
                f"\nnote: this overwrite repeated {lines} lines to change {rewritten} – change lines of an existing file "
                "with replace_text (one call per changed block): a whole-file rewrite spends a turn's output on unchanged "
                "lines and breaks off at the output limit"
            )
        if lines > READ_VISIBLE_LINES and not rel.lower().endswith(_NOT_SPLIT):
            # live G1 on rc95 (BUG-367): an 850-line collector.py written in one go was paged through in 13 reads,
            # each pushing an earlier part out of view, and three attempts ended without the config, docs and tests
            note += (
                f"\nnote: {rel} has {lines} lines, more than the {READ_VISIBLE_LINES} the context shows of a file at once – "
                "split it into modules by concern (API client, parsing, server, configuration) before you build on it"
            )
        return self._ok(
            ToolName.write_file,
            f"{verb} {rel} ({len(data)} bytes, {lines} lines){note}",
            mutated_paths=[rel],
            data={"path": rel, "operation": op, "bytes": len(data), "changed": True},
        )

    async def _replace_text(self, args: ReplaceTextArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        self._guard_for_write()
        rel, target = self.fs.resolve_for_write(args.path)
        if not target.exists():
            raise ToolError(E.NOT_FOUND, f"'{rel}' does not exist; use write_file to create it")
        await self._authorise(ToolName.replace_text, [(rel, "modify")], events)
        tf = await asyncio.to_thread(self.fs.read_text, rel, max_bytes=self.max_read_bytes)
        text = tf.text
        if args.old == args.new:
            # the coder sent the same 2 KB replace four times: its fix was already in the file from an earlier write,
            # and the bare "identical" refusal never said so (live G1 S005, BUG-272)
            present = args.old in text or args.old in self.redactor.text(text)
            state = (
                f"{rel} already contains this text, so the change it stands for is already made. Do not send it again: "
                "go on with the next open point of the correction or LATEST FAILURE, run the check, or call complete_step."
                if present
                else f"this text is not in {rel} either; copy the current lines into 'old' and write the changed lines in 'new'."
            )
            if present and REDACTED in args.old:  # the masked value stays as it is (BUG-279)
                state += (
                    f" A {REDACTED} line keeps its real value; to remove a flagged value, put a placeholder where "
                    f"{REDACTED} stands (docs: <your-token> or $AUTH_TOKEN; tests: a value containing 'dummy', 'fake' or "
                    "'example')."
                )
            raise ToolError(
                E.NO_CHANGE,
                f"replace refused: 'old' and 'new' are identical, so nothing would change. {state}",
                data={"path": rel, "changed": False, "repeated": True, "present": present},
            )
        if not (REDACTED in args.old and REDACTED not in text):  # a masked 'old' is resolved by _masked_replace
            self._check_placeholder(args.new, text)
        old, new = args.old, args.new
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\r\n" not in old and "\n" in old:
            crlf_old, crlf_new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            if text.count(crlf_old):
                old, new, count = crlf_old, crlf_new, text.count(crlf_old)
        masked = self._masked_replace(text, old, new) if count == 0 and REDACTED in old and args.count == 1 else None
        if masked is not None:
            updated, first_line, kept_masked = masked
            if updated == text:
                # live M3: the coder re-wrote a masked test token unchanged twice; "masked secret replaced" told it the
                # flagged value was gone while the file stayed the same (BUG-279)
                raise ToolError(
                    E.NO_CHANGE,
                    f"replace refused: nothing would change in {rel} – the line with {REDACTED} keeps its real value when "
                    f"the marker is copied unchanged. To remove a flagged value, put a placeholder where {REDACTED} stands "
                    "(docs: <your-token> or $AUTH_TOKEN; tests: a value containing 'dummy', 'fake' or 'example').",
                    data={"path": rel, "changed": False},
                )
            data = updated.encode("utf-8")
            await asyncio.to_thread(self.fs.atomic_write, target, data, mode=tf.mode)
            self._file_changed(ToolName.replace_text, [{"path": rel, "operation": "modify", "replacements": 1}], events)
            what = f"the {REDACTED} line(s) kept their real value" if kept_masked else "the masked value is replaced by the new text"
            return self._ok(
                ToolName.replace_text,
                f"replaced 1 occurrence(s) in {rel} (first at line {first_line}; {what}); file now has {len(data)} bytes",
                mutated_paths=[rel],
                data={"path": rel, "operation": "modify", "replacements": 1, "first_line": first_line, "kept_masked": kept_masked},
            )
        if count == 0 and REDACTED not in old and "\r\n" not in text:
            loose = _line_end_matches(text, old)
            if loose:
                # live tournament GLM A3: the file's blank lines held the indentation spaces, the coder's 'old' had
                # empty lines; four replaces failed with "indentation must match" while read_range showed no
                # difference (BUG-301). Spaces at the end of a line are invisible, so they do not decide a match.
                return await self._replace_spans(rel, target, text, loose, new=new, want=args.count, mode=tf.mode, events=events)
        if count == 0:
            # the coder's view of the file is wrong: a re-read of it is no repeat (BUG-335)
            self._reads = {
                k: v for k, v in self._reads.items() if (k[1] if k[0] == ToolName.read_file.value else k[1].rpartition(":")[0]) != rel
            }
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

    async def _replace_spans(
        self,
        rel: str,
        target: Path,
        text: str,
        spans: list[tuple[int, int]],
        *,
        new: str,
        want: int,
        mode: int | None,
        events: list[PendingEvent],
    ) -> ToolResult:
        """Replace the matches of an 'old' that differs from the file only in spaces at line ends (BUG-301)."""
        lines = [text.count("\n", 0, a) + 1 for a, _ in spans]
        if len(spans) != want:
            raise ToolError(
                E.TEXT_COUNT_MISMATCH,
                f"'old' occurs {len(spans)} times in {rel} (lines {', '.join(map(str, lines[:20]))}, ignoring spaces at "
                f"line ends) but count={want}; add surrounding context to make it unique or pass count={len(spans)}",
                data={"occurrences": len(spans), "lines": lines[:50]},
            )
        parts, pos = [], 0
        for a, b in spans:
            parts += [text[pos:a], new]
            pos = b
        updated = "".join(parts) + text[pos:]
        data = updated.encode("utf-8")
        await asyncio.to_thread(self.fs.atomic_write, target, data, mode=mode)
        self._file_changed(ToolName.replace_text, [{"path": rel, "operation": "modify", "replacements": len(spans)}], events)
        return self._ok(
            ToolName.replace_text,
            f"replaced {len(spans)} occurrence(s) in {rel} (first at line {lines[0]}; spaces at line ends were ignored "
            f"for the match); file now has {len(data)} bytes",
            mutated_paths=[rel],
            data={"path": rel, "operation": "modify", "replacements": len(spans), "first_line": lines[0], "line_end_spaces": True},
        )

    def _masked_replace(self, text: str, old: str, new: str) -> tuple[str, int, bool] | None:
        """Replace ``old`` as the coder saw it – with a secret masked as the redaction marker – by ``new``: the match
        runs on the redacted view of the file, line by line, and must be unique; the text around it on the matched
        lines must contain no masked part, so nothing masked is written back. The verifier demanded the removal of a
        test fixture secret that every read showed masked, and no tool could edit that line (live G1, BUG-248)."""
        lines = text.splitlines(keepends=True)
        red = [self.redactor.text(ln) for ln in lines]
        view = "".join(red)
        idx = view.find(old)
        if idx < 0 or view.find(old, idx + 1) >= 0:
            return None
        starts, pos = [], 0
        for ln in red:
            starts.append(pos)
            pos += len(ln)
        a = max(i for i, s in enumerate(starts) if s <= idx)
        b = max(i for i, s in enumerate(starts) if s <= idx + max(len(old) - 1, 0))
        block = "".join(red[a : b + 1])
        prefix, suffix = block[: idx - starts[a]], block[idx - starts[a] + len(old) :]
        real = "".join(lines[a : b + 1])
        if REDACTED in prefix or REDACTED in suffix or not real.startswith(prefix) or not real.endswith(suffix):
            return None
        kept_masked = False
        if REDACTED in new:
            # a masked line the coder left as it was keeps its real content: the coder changed a call around a fixture
            # password and every edit was refused for the marker it copied unchanged (live G1 S014, BUG-270)
            shown = old.splitlines(keepends=True)
            actual = real[len(prefix) : len(real) - len(suffix)].splitlines(keepends=True)
            if len(shown) == len(actual):
                kept = {v: r for v, r in zip(shown, actual, strict=True) if REDACTED in v}
                kept_masked = any(ln in kept for ln in new.splitlines(keepends=True))
                new = "".join(kept.get(ln, ln) for ln in new.splitlines(keepends=True))
            self._check_placeholder(new, None)
        return "".join(lines[:a]) + prefix + new + suffix + "".join(lines[b + 1 :]), a + 1, kept_masked

    @staticmethod
    def _not_found_hint(rel: str, text: str, old: str) -> str:
        """Why 'old' is not in the file, with the file's current lines where its first line occurs. The coder's re-read
        of a range it already read is refused (READ_REPEATED), so the lines to copy come with the refusal; "the text
        exists with different whitespace" also stood for text inside a longer line (`updated = db.update_task(…)`), and
        four replaces in a row failed (ladder A10 #4, BUG-335)."""
        msg = f"'old' text not found in {rel}"
        if REDACTED in old:
            return msg + (
                f"; it contains the redaction marker {REDACTED}: copy the masked line exactly as shown (marker included, "
                "unique in the file) to replace the secret with new text that contains no marker"
            )
        lines = text.splitlines()
        old_lines = old.splitlines() or [old]
        first = next((ln.strip() for ln in old_lines if ln.strip()), "")
        hits = [n for n, ln in enumerate(lines, start=1) if first and first in ln]
        if not hits:
            return msg + "; re-read the file (read_range) and copy the exact current text"
        if all(lines[n - 1].strip() == first for n in hits):
            why = "its first line exists with other leading/trailing whitespace (indentation), or the lines after it differ"
        else:
            why = "its first line is part of a longer line"
        span = max(1, min(len(old_lines), 12))
        shown = _numbered_lines(lines, hits[:3], span, limit=1500)
        more = f" (and at line(s) {', '.join(map(str, hits[3:10]))})" if len(hits) > 3 else ""
        return f"{msg}; {why}. Current text at line(s) {', '.join(map(str, hits[:3]))}{more} – copy from here exactly:\n{shown}"

    # ============================================================================================ 17.4 patch
    async def _apply_patch(self, args: ApplyPatchArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        guard = self._guard_for_write()
        text, rebuilt = rebuild_new_files(args.patch, self._is_new_file)
        parsed = parse_patch(text)
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
        patch = text if text.endswith("\n") else text + "\n"
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
        if rebuilt:
            summary += f" (header rebuilt as a new file: {', '.join(rebuilt)}; write_file creates a file more simply)"
        return self._ok(ToolName.apply_patch, f"patch applied: {summary}", mutated_paths=mutated, data={"changes": changes})

    def _is_new_file(self, raw: str) -> bool:
        _, target = self.fs.resolve_for_write(canonical_path(raw))
        return not target.exists()

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
        masked = mask_scratch_removals(command) if cls.kind == "destructive" and _isolated(self.executor) else None
        if masked is not None and self.classifier.classify(masked).kind not in ("forbidden", "destructive"):
            cls = CommandClassification("mutate", "removes only scratch paths below the sandbox /tmp", cls.pattern)
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
        # a pipeline reports its first failing command: 'pytest | tail -80' exited 0 with 28 failing tests, and the
        # runtime took the run as passed and completed the step (live G1, BUG-267)
        full = f"{PIPEFAIL}\n{full}"
        effective_timeout = timeout_seconds if egress and timeout_seconds else self._timeout(timeout_seconds)
        repeat_key = (cwd, command.strip(), effective_timeout)
        if self._timed_out.get(repeat_key) == self._writes:
            raise ToolError(
                E.COMMAND_TIMEOUT,
                f"command refused: it already ran into its {effective_timeout}s timeout and no file changed since, so it "
                f"would block for {effective_timeout}s again. {TIMEOUT_HINT}",
                data={"timeout_seconds": effective_timeout, "repeated": True},
            )
        ran_key = (cwd, command.strip())
        core_key = (cwd, core_command(command))
        previous = self._ran.get(ran_key) if not egress else None
        sliced = False
        if (previous is None or previous[0] != self._writes) and not egress and core_key != ran_key:
            core_previous = self._ran_core.get(core_key)
            if core_previous is not None and core_previous[0] == self._writes:
                if self._sliced.get(core_key) == self._writes:
                    previous, sliced = core_previous, True
                else:
                    self._sliced[core_key] = self._writes
        if previous is not None and previous[0] == self._writes:
            passed = previous[2] == "exit code 0"
            follow_up = (
                "It succeeded: if the STEP GOAL or ACCEPTANCE still needs a change (for example new tests), make that "
                "change now; otherwise call complete_step."
                if passed
                else "Fix the code or the tests its output names, then run it again. Its output was:\n"
                + self._ran_output.get(core_key, "(not kept)")
            )
            same = (
                "the same command with only another slice of its output (tail, head, grep, labels, -v) already ran twice"
                if sliced
                else "it already ran"
            )
            raise ToolError(
                E.COMMAND_REPEATED,
                f"command refused: {same} in turn {previous[1]} and no file changed since ({previous[2]}); running it "
                f"again gives the same result. {follow_up}",
                data={"previous_turn": previous[1], "repeated": True, "passed": passed, "command": command.strip()},
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
        if result is not None and result.did_not_run and error is None:
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
        failed_tests = 0
        if exit_code == 0 and not timed_out and error is None:
            # a runner that does not set the exit code: Invoke-Pester without -CI printed ten failed tests with exit
            # code 0, and the coder completed the step on it (ladder A9 #7, BUG-329)
            counts = parse_counts(f"{stdout}\n{stderr}")
            failed_tests = counts.failed + counts.errors if counts is not None else 0
        if timed_out:
            self._timed_out[repeat_key] = self._writes
        elif result is not None and error is None:
            outcome = f"exit code {exit_code}" + (f", {failed_tests} failed test(s) in the output" if failed_tests else "")
            self._ran[ran_key] = (self._writes, ctx.turn, outcome)
            self._ran_core[core_key] = (self._writes, ctx.turn, outcome)
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
        run = _RunOutcome(result, audit, classification, cwd, duration, error, failed_tests)
        recorded = self._ran.get(ran_key)
        if recorded is not None and recorded[1] == ctx.turn and (exit_code != 0 or failed_tests):
            # the refusal of a repeat carries this output itself: LATEST FAILURE may have been replaced since by another
            # command that succeeded, and the coder re-proposed the refused check five times (live G1, BUG-246)
            self._ran_output[core_key] = self._render_run(command, run, budget=self.output_limit // 2, head_ratio=0.2)[0]
        else:
            self._ran_output.pop(core_key, None)
        return run

    def _render_run(self, command: str, run: _RunOutcome, *, budget: int, head_ratio: float) -> tuple[str, bool]:
        res = run.result
        header = f"$ {command}" + (f"  (cwd: {run.cwd})" if run.cwd != "." else "")
        if res is None or run.sandbox_failed:
            status = f"sandbox error: {run.error}"
        elif res.timed_out:
            status = f"timed out after {run.duration_ms / 1000:.1f}s (killed)"
        else:
            status = f"exit code {res.exit_code} · {run.duration_ms / 1000:.1f}s"
        footer: list[str] = violation_lines(run.audit.violations)
        if run.audit.allowed:
            footer.append("[files] changed: " + ", ".join(f"{c.operation} {c.path}" for c in run.audit.allowed[:30]))
        if run.audit.cleaned:
            footer.append(f"[cleanup] removed {len(run.audit.cleaned)} new generated file(s) that are not git-ignored")
        stdout = strip_ansi(res.stdout) if res else ""
        stderr = strip_ansi(res.stderr) if res else ""
        if res is not None and not res.timed_out and res.exit_code != 0:
            # live M1: ruff named tests/test_app.py:73 and the coder edited the identically built test above it (BUG-294)
            named = source_locations(f"{stdout}\n{stderr}", self._source_lines, cwd=run.cwd)
            if named:
                footer.append("[source] the lines this output names:\n" + "\n".join(named))
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
        if res is not None and not res.timed_out and res.exit_code != 0 and _isolated(self.executor):
            gone = _tmp_gone(command, f"{stdout}\n{stderr}")
            if gone:
                footer.append(TMP_GONE_HINT.format(path=gone))
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

    async def _check_shell_read(self, command: str, cwd: str, turn: int) -> None:
        """Refuse a command that only prints lines of a file a fully visible read of this attempt shows unchanged: after
        READ_REPEATED the coder printed the same file with 'sed -n' and 'cat -n' instead (live G1, BUG-344)."""
        read: ShellRead | None = shell_read(command)
        if read is None:
            return
        raw = read.path if cwd in ("", ".") or read.path.startswith("/") else f"{cwd.rstrip('/')}/{read.path}"
        try:
            path = self.fs.resolve(raw)[0]
            rel, _, total = await asyncio.to_thread(self.fs.read_lines, raw, 1, 1)
        except (ToolError, OSError, ValueError):
            return  # the command reports a missing or unreadable file itself
        start = max(1, total + read.start + 1) if read.start < 0 else read.start
        end = total if read.end is None else min(read.end, total)
        if total == 0 or start > end:
            return
        covering = self._covering_read(path, start, end, turn)
        if covering is None:
            return
        raise ToolError(
            E.READ_REPEATED,
            f"command refused: it only prints lines {start}-{end} of {rel}, which are part of what you read in turn "
            f"{covering[1]} ({covering[0]}), and no file changed since; that content is shown above among the files you "
            "read. Act on it now: edit the code or the test, run a different check, or call complete_step/block_step.",
            data={"previous_turn": covering[1], "repeated": True},
        )

    async def _run_command(self, args: RunCommandArgs, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        if REDACTED in args.command:
            # a here-document of a file the coder had read wrote the masked lines back literally (live G1 rc90, BUG-355)
            raise ToolError(
                E.REDACTED_PLACEHOLDER,
                f"command refused: it contains the redaction marker {REDACTED}, which a command would write literally. "
                "Write file contents with write_file or replace_text: a masked line copied unchanged keeps its real value there.",
            )
        await self._check_shell_read(args.command, args.cwd, ctx.turn)
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
        elif run.failed_tests:
            error_code = E.COMMAND_FAILED
            text += f"\n{FAILED_TESTS_HINT.format(n=run.failed_tests)}"
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

    def _source_lines(self, rel: str) -> list[str] | None:
        """Lines of a readable workspace file for :func:`source_locations` (BUG-294); ``None`` for anything a read
        tool would refuse or that is too large."""
        try:
            rel, target = self.fs.resolve(rel)
            if not target.is_file() or target.stat().st_size > _SOURCE_FILE_BYTES:
                return None
            return target.read_text(encoding="utf-8", errors="replace").splitlines()
        except (ToolError, OSError):
            return None

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
            self._ran_core.clear()
            self._ran_output.clear()
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
        test_key = (run.cwd, core_command(args.command))
        if summary.status == "failed":
            self._failed_tests[test_key] = args.command.strip()
        elif summary.status == "passed":
            self._failed_tests.pop(test_key, None)
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

    def expect_corrections(self, paths: Iterable[str]) -> None:
        """The files a correction attempt is asked to fix (its correction items' paths, BUG-258)."""
        self._correction_paths = list(dict.fromkeys(canonical_path(p) for p in paths if p))

    def expect_rechecks(self, commands: Iterable[str]) -> None:
        """The commands of the lint/test checks whose failure the correction attempt must resolve (BUG-276)."""
        self._recheck_commands = list(dict.fromkeys(c.strip() for c in commands if c and c.strip()))

    def expect_acceptance_tests(self, commands: Iterable[str]) -> None:
        """The test commands of the step's acceptance criteria: complete_step runs them as the verifier does (BUG-351)."""
        self._acceptance_tests = list(dict.fromkeys(c.strip() for c in commands if c and c.strip()))

    def expect_secret_rechecks(self, paths: Iterable[str]) -> None:
        """The files whose secret check failed: complete_step scans their added lines again (BUG-279)."""
        self._recheck_secrets = list(dict.fromkeys(canonical_path(p) for p in paths if p))

    async def _secrets_left(self) -> list[str]:
        """Lines of the flagged files, not in their base version, on which the verifier's scanner still finds a
        secret – as the verifier shows them (masked)."""
        found = await self._secret_findings(self._recheck_secrets)
        return [f"{f.path}:{f.line} ({f.rule}): {f.preview}" for findings in found.values() for f in findings]

    async def _secret_findings(self, paths: Sequence[str]) -> dict[str, list[SecretFinding]]:
        """The verifier's secret findings on the lines of ``paths`` that are not in their base version (the lines the
        verifier scans). A scan that cannot run here finds nothing – the verifier keeps the judgement."""
        from hermclaw.verifier.languages import detect_language, is_lock_or_data_file
        from hermclaw.verifier.secrets import SecretScanner

        scanner = SecretScanner()
        out: dict[str, list[SecretFinding]] = {}
        show_base = getattr(self.git, "show_base_file", None)  # GitReader extension of the real reader
        if show_base is None:
            return out
        for rel in paths:
            if not (self.fs.root / rel).is_file():
                continue
            text = (await asyncio.to_thread(self.fs.read_text, rel, max_bytes=self.max_read_bytes)).text
            try:
                base = await show_base(self.workspace, rel) or ""
            except Exception as exc:  # a scan that cannot run here leaves the judgement to the verifier
                log.warning("secret recheck skipped", extra={"path": rel, "error_type": type(exc).__name__})
                continue
            before = Counter(base.splitlines())  # the base comes redacted: compare redacted lines
            language, noisy = detect_language(rel), is_lock_or_data_file(rel)
            for n, line in enumerate(text.splitlines(), start=1):
                key = self.redactor.text(line)
                if before[key] > 0:
                    before[key] -= 1
                    continue
                hits = scanner.scan_line(rel, n, line[:20_000], language=language, entropy=not noisy)
                if hits:
                    out.setdefault(rel, []).extend(hits)
        return out

    async def _secrets_after_write(self, paths: Sequence[str]) -> list[str]:
        """The secret check of the verifier on the files just written: live G1 (rc88) failed an attempt in the
        verifier on a realistic test token the coder could have replaced while writing the test (BUG-347)."""
        from hermclaw.verifier.checks import secret_remedy

        found = await self._secret_findings(paths)
        return [
            f"{rel}: possible secret(s): " + ", ".join(f"line {f.line} ({f.rule})" for f in findings[:5]) + f". {secret_remedy(rel)}"
            for rel, findings in found.items()
        ]

    async def _lint_after_write(self, result: ToolResult, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        """The verifier's syntax and lint findings in the files just written, added to the write's result: Agents-A1
        (tournament A3) learned of an unused import only at complete_step, fixed one of two findings and spent seven
        turns calling ruff with invented options (BUG-302); a missing "}" in a PowerShell function survived two attempts
        of A9 because nothing parsed the script (BUG-326). A check that cannot run here adds nothing."""
        from hermclaw.verifier.syntax import ESM_HINTS

        if ctx.turn >= self.max_turns:
            return result
        found, syntax = await asyncio.to_thread(self._syntax_after_write, result.mutated_paths or [])
        found += await self._secrets_after_write(result.mutated_paths or [])
        commands = [*syntax, *await self._lint_commands(result.mutated_paths)]
        for command in commands:
            try:
                run = await self._sandbox_run(
                    ToolName.run_command,
                    command=command,
                    cwd_raw=".",
                    timeout_seconds=None,
                    purpose="lint-after-write",
                    ctx=ctx,
                    events=events,
                )
            except ToolError:
                continue
            if run.sandbox_failed or run.result is None or run.result.timed_out or run.result.exit_code in (0, 126, 127):
                continue
            if command in syntax and any(h in run.result.stdout + run.result.stderr for h in ESM_HINTS):
                continue  # node --check on ESM in a CommonJS context: the verifier skips it too
            found.append(self._render_run(command, run, budget=self.output_limit // 2, head_ratio=0.5)[0])
        if not found:
            return result
        note = (
            "\n\nsyntax, lint and secret checks, run as the verifier runs them, find problems in the file(s) just written. "
            "Fix every finding now "
            "(the step cannot complete with them; if you are still writing the file in parts, finish it first):\n"
        )
        return result.model_copy(update={"output": result.output + note + "\n".join(found), "data": result.data | {"lint_findings": True}})

    def _syntax_after_write(self, paths: Sequence[str]) -> tuple[list[str], list[str]]:
        """The verifier's syntax check of written files (BUG-326): the findings of the parsers that run here (JSON, YAML,
        TOML, Python – a language with a lint command is left to the lint) and one sandbox command per language whose
        parser runs in the sandbox image (PHP, JavaScript, shell, PowerShell)."""
        from hermclaw.verifier.checks import MAX_SYNTAX_FILE_BYTES
        from hermclaw.verifier.languages import detect_language
        from hermclaw.verifier.syntax import IN_PROCESS, SANDBOX_TOOLS, javascript_needs_compile_check, sandbox_check_command

        linted = self.policies.verifier.lint_commands or {}
        found: list[str] = []
        sandbox: dict[str, list[str]] = {}
        for rel in paths:
            path = self.fs.root / rel
            if not path.is_file() or path.stat().st_size > MAX_SYNTAX_FILE_BYTES:
                continue
            data = path.read_bytes()
            language = detect_language(rel, data[:256])
            if language is None or language in linted:
                continue
            text = data.decode("utf-8", errors="replace")
            if language in IN_PROCESS:
                checked = IN_PROCESS[language](rel, text)
                if checked.status == "fail":
                    found.append(f"{rel}: {checked.message}")
            elif language in SANDBOX_TOOLS and not (language == "javascript" and javascript_needs_compile_check(rel, text)):
                sandbox.setdefault(language, []).append(rel)
        return found, [sandbox_check_command(lang, files) for lang, files in sorted(sandbox.items())]

    async def _lint_commands(self, paths: list[str] | None = None) -> list[str]:
        """The verifier's lint commands for the files changed now, or for ``paths`` – a review correction on K1 (cycle
        2, rc60) brought a new F841 in and the verifier blocked the step on its last attempt (BUG-283)."""
        from hermclaw.verifier.checks import lint_command
        from hermclaw.verifier.languages import detect_language

        templates = self.policies.verifier.lint_commands
        if not templates:
            return []
        by_language: dict[str, list[str]] = {}
        for rel in paths if paths is not None else await self._changed_files() or []:
            path = self.fs.root / rel
            if not path.is_file():
                continue
            with path.open("rb") as fh:
                language = detect_language(rel, fh.read(256))
            if language in templates:
                by_language.setdefault(language, []).append(rel)
        return [lint_command(templates[lang], files) for lang, files in sorted(by_language.items())]

    async def _still_failing(self, ctx: CallContext, events: list[PendingEvent]) -> tuple[str, str, str] | None:
        """The first failed check of the correction that still fails, a lint finding in the changed files, or – once – an
        acceptance test of the step or a test run of this attempt whose last result failed, run as the verifier runs
        it, with its kind (``check``, ``acceptance``, ``own``); a check that cannot run here is skipped. In the last turn
        the checks run only while the turns of ``policies.coder.grace_turns`` can still be granted (BUG-349)."""
        if ctx.turn >= self.max_turns and (self._granted_turns or not self.policies.coder.grace_turns):
            return None
        lint = [c for c in await self._lint_commands() if c not in self._recheck_commands]
        checks = [(".", c, "check") for c in [*self._recheck_commands, *lint]]
        if not self._tests_reminded:
            # live G1 S007: the step's acceptance ran 'python -m unittest discover' – no tests ran, tests/ was no package –
            # while the coder ran its suite with '-s tests' and completed (BUG-351); a test run failed in turn 15, the
            # coder changed two files and completed without running it again (BUG-349)
            checks += [(".", c, "acceptance") for c in self._acceptance_tests if c not in self._recheck_commands]
            ran = {c for _, c, _ in checks}
            checks += [(cwd, c, "own") for (cwd, _), c in self._failed_tests.items() if c not in ran]
        for cwd, command, kind in checks:
            try:
                run = await self._sandbox_run(
                    ToolName.run_command, command=command, cwd_raw=cwd, timeout_seconds=None, purpose="verifier", ctx=ctx, events=events
                )
            except ToolError as exc:
                previous = self._ran.get((cwd, command.strip()))
                missing_tool = previous is not None and previous[2] in ("exit code 126", "exit code 127")
                if exc.code == E.COMMAND_REPEATED and not exc.data.get("passed") and not missing_tool:
                    return command, self._ran_output.get((cwd, core_command(command)), "(output not kept)"), kind
                continue
            if run.sandbox_failed or run.result is None or run.result.timed_out:
                continue
            if run.result.exit_code in (0, 126, 127) and not run.failed_tests:
                continue  # 126/127: the tool is not in the image, the verifier skips it too
            return command, self._render_run(command, run, budget=self.output_limit, head_ratio=0.3)[0], kind
        left = await self._secrets_left() if self._recheck_secrets else []
        if left:
            # live M3: the coder swapped one README placeholder for another for eleven turns while four other flagged
            # lines stayed, and re-wrote a masked test token unchanged; nothing told it which lines still failed (BUG-279)
            shown = "\n".join(left[:20]) + (f"\n(+{len(left) - 20} more)" if len(left) > 20 else "")
            return (
                "secret scan",
                f"{shown}\nReplace each value (shown as {REDACTED} where it is masked) with an obvious placeholder: in docs "
                "<your-token> or $AUTH_TOKEN, in tests a value containing 'dummy', 'fake' or 'example'.",
                "check",
            )
        return None

    def _untouched_corrections(self, turn: int) -> list[str]:
        """Existing files the correction names that this attempt has not changed – empty once reminded, and on the
        last turn (a refusal there would only exhaust the budget)."""
        if self._correction_reminded or turn >= self.max_turns:
            return []
        return [p for p in self._correction_paths if p not in self._changed_now and (self.fs.root / p).is_file()]

    async def _complete_step(self, args: CompletionReport, ctx: CallContext, events: list[PendingEvent]) -> ToolResult:
        untouched = self._untouched_corrections(ctx.turn)
        if untouched:
            # live G1 S002: the last correction attempt rewrote collector.py, ran its tests and completed – the secret
            # and lint findings in tests/test_collector.py were never touched and the step was blocked (BUG-258)
            self._correction_reminded = True
            raise ToolError(
                E.CORRECTION_OPEN,
                "complete_step refused once: the CORRECTION lists problems in "
                + ", ".join(untouched[:10])
                + ", which this attempt has not changed. Fix them now; if they are already resolved, call complete_step "
                "again.",
                data={"untouched": untouched},
            )
        failing = await self._still_failing(ctx, events)
        if failing is not None:
            # live K1: the correction named ruff findings, the coder re-ran only pytest and the runtime completed the step
            # for it; the verifier failed on the same lint output twice and the plan was replaced (BUG-276)
            command, output, kind = failing
            granted = ""
            if ctx.turn >= self.max_turns:
                self._granted_turns = self.policies.coder.grace_turns
                granted = f" You get {self._granted_turns} more turn(s) for this, once."
            if kind != "check":
                self._tests_reminded = True
                what = (
                    f"the step's acceptance test `{command}`, run as the verifier runs it, fails, so the verifier would "
                    "reject the step. Make it pass as written"
                    if kind == "acceptance"
                    else f"your test run `{command}` failed in this attempt and still fails, so the verifier would most "
                    "likely reject the step. Fix what its output names and run it again"
                )
                raise ToolError(
                    E.CHECK_STILL_FAILING,
                    f"complete_step refused once: {what}; if the failure cannot be fixed within this step, call complete_step "
                    f"again.{granted} Its output:\n{output}",
                    data={"command": command, "test_kind": kind, "granted_turns": self._granted_turns},
                )
            raise ToolError(
                E.CHECK_STILL_FAILING,
                "complete_step refused: a check the verifier runs on this step still fails, so the verifier would reject "
                f"the step. Fix what its output names, then call complete_step.{granted} Its output:\n{output}",
                data={"command": command, "granted_turns": self._granted_turns},
            )
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


def _line_end_matches(text: str, old: str) -> list[tuple[int, int]]:
    """Spans of ``old`` in ``text`` when spaces and tabs before a line break inside ``old`` are not compared – a
    blank line with indentation spaces equals an empty one (BUG-301). The last line of ``old`` is compared as it is,
    so a one-line 'old' never matches more loosely than before."""
    if "\n" not in old:
        return []
    *heads, last = old.split("\n")
    pattern = "".join(re.escape(ln.rstrip(" \t")) + r"[ \t]*\n" for ln in heads) + re.escape(last)
    return [(m.start(), m.end()) for m in re.finditer(pattern, text)]


# test definitions by name: pytest/unittest functions, JS/TS it()/test(), Pester It, Go Test functions
_TEST_NAME_RES = (
    re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test\w*)[ \t]*\(", re.MULTILINE),
    re.compile(r"\b(?:it|test)[ \t]*\([ \t]*(['\"`])(.+?)\1"),
    re.compile(r"^[ \t]*It[ \t]+(['\"])(.+?)\1", re.MULTILINE),
    re.compile(r"^func[ \t]+(Test\w+)[ \t]*\(", re.MULTILINE),
)


def _test_names(text: str) -> set[str]:
    """The names of the tests ``text`` defines. An overwrite of test_api.py dropped three existing tests the job said to
    keep, and nothing told the coder (ladder A10 #4, BUG-336)."""
    names: set[str] = set()
    for rx in _TEST_NAME_RES:
        names.update(m.group(m.lastindex or 0) for m in rx.finditer(text))
    return names


def _written_text(tool: ToolName, args: Mapping[str, Any]) -> str:
    """What a write put into a file: the content, the replacement, or the added lines of a patch."""
    if tool == ToolName.write_file:
        return str(args.get("content") or "")
    if tool == ToolName.replace_text:
        return str(args.get("new") or "")
    if tool == ToolName.apply_patch:
        lines = str(args.get("patch") or "").splitlines()
        return "\n".join(line[1:] for line in lines if line.startswith("+") and not line.startswith("+++"))
    return ""


def _read_path(tool: str, key: str) -> str:
    """The file of a recorded read (read_file keys are paths, read_range keys ``path:start-end``)."""
    return key if tool == ToolName.read_file.value else key.rpartition(":")[0]


def _numbered_lines(lines: list[str], starts: list[int], span: int, *, limit: int) -> str:
    """``span`` lines from each 1-based start as ``N| text`` (the read_range format), blocks separated by ``…``, clipped
    to about ``limit`` characters."""
    blocks = []
    for start in starts:
        end = min(len(lines), start + span - 1)
        width = len(str(end))
        blocks.append("\n".join(f"{n:>{width}}| {lines[n - 1]}" for n in range(start, end + 1)))
    out = "\n…\n".join(blocks)
    return out if len(out) <= limit else out[:limit] + "\n…[clipped]"


def _occurrence_lines(text: str, needle: str) -> list[int]:
    lines: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            return lines
        lines.append(text.count("\n", 0, idx) + 1)
        start = idx + len(needle)
