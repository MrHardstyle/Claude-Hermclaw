"""Runtime-controlled scope expansion (Bauplan §17 "Scope-Erweiterung", P15 15.7).

A worker can only *request* more scope (``request_scope_expansion`` tool -> :class:`ScopeExpansionRequest`); it can
never widen its contract itself. The runtime handles every request here::

    worker requests scope expansion
    -> validate paths (normalised, explicit, not forbidden, existing or creatable)
    -> repository evidence (RepoContextProvider.read of the current scope files)
    -> classify: mechanical | semantic
    -> mechanical: runtime grants a new scope version (source runtime_expansion, scope.expanded)
       semantic:   NeedsReplan decision – the planner/replanner decides
    -> scope.expansion.requested is emitted for every request, whatever the outcome

*Mechanical* means every requested path is (a) a test of a current scope file (name relation or the test imports
it), (b) a direct import/dependency of a current scope file (an import line of that file references the path), or
(c) in the same directory and of the same language as a current scope file. Deletions are always semantic. The
number of granted expansions per step is limited (``ScopeEngineSettings.max_expansions_per_step``, default 3).
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import Operation, ScopeContract, ScopeExpansionRequest
from hermclaw.core.config import HermclawConfig, ScopePolicy
from hermclaw.core.errors import ConflictError
from hermclaw.core.interfaces import RepoContextProvider, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import redact
from hermclaw.events.store import append_event
from hermclaw.persistence.models import ScopeContractRow
from hermclaw.scope.engine import (
    CLOSED_STEP_STATUSES,
    SOURCE_TYPE,
    ScopeEngineSettings,
    WorkspaceFiles,
    canonical_path,
    contract_event_payload,
    contract_from_row,
    count_runtime_expansions,
    current_scope_row,
    deny_all_contract,
    designated_deletes,
    glob_escape,
    implausible_new_path,
    list_workspace_files,
    literal_path,
    load_step,
    merged_forbidden,
    now_iso,
    persist_scope_version,
)
from hermclaw.scope.guard import ScopeGuard

log = get_logger(__name__)

SOURCE_ID = "scope_expansion"
ExpansionOutcome = Literal["granted", "needs_replan", "rejected"]
Classification = Literal["mechanical", "semantic"]
PathStatus = Literal["pending", "already_allowed", "invalid", "forbidden"]

_OPERATION_ORDER: tuple[Operation, ...] = ("create", "modify", "delete")
#: requested paths are literal files; "[" / "]" are legal file-name characters (escaped into the contract), but
#: "*" and "?" only ever mean globs, which an expansion request must not carry
_WILDCARDS = frozenset("*?")
_MAX_REQUEST_RECORDS = 20
_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    **dict.fromkeys(("py", "pyi", "pyx"), "python"),
    **dict.fromkeys(("js", "mjs", "cjs", "jsx", "ts", "tsx", "mts", "cts"), "javascript"),
    **dict.fromkeys(("php", "phtml", "inc"), "php"),
    **dict.fromkeys(("rb", "erb"), "ruby"),
    **dict.fromkeys(("c", "h"), "c"),
    **dict.fromkeys(("cc", "cpp", "cxx", "hpp", "hh"), "cpp"),
    **dict.fromkeys(("kt", "kts"), "kotlin"),
    **dict.fromkeys(("sh", "bash", "zsh"), "shell"),
    **dict.fromkeys(("yaml", "yml"), "yaml"),
    **dict.fromkeys(("md", "mdx", "rst", "adoc", "txt"), "docs"),
    **dict.fromkeys(("html", "htm", "twig", "j2", "jinja", "jinja2", "tpl", "mustache", "hbs"), "template"),
    **dict.fromkeys(("css", "scss", "sass", "less"), "stylesheet"),
}
_SOURCE_EXTENSIONS = frozenset(
    {*(_LANGUAGE_BY_EXTENSION.keys()), "go", "rs", "java", "scala", "cs", "swift", "json", "toml", "sql", "vue", "svelte"}
)
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})
_TEST_STEM_RE = re.compile(r"^(?:test_(?P<prefixed>.+)|(?P<suffixed>.+?)(?:_test|_tests|\.test|\.spec|_spec|Test|Tests))$")
_PACKAGE_STEMS = frozenset({"__init__", "index", "mod", "main"})

# ---------------------------------------------------------------------------------------------- import evidence
_IMPORT_LINE_RE = re.compile(
    r"^\s*(?:from|import|export|#\s*include|@import|@use|use|mod|pub\s+mod|require|require_once|include|include_once|"
    r"require_relative)\b|\brequire\s*\(|\bimport\s*\("
)
_PY_FROM_RE = re.compile(r"^\s*from\s+(?P<module>\.*[\w.]*)\s+import\s+(?P<names>.+)$")
_PY_IMPORT_RE = re.compile(r"^\s*import\s+(?P<names>[\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)\s*;?\s*$")
_QUOTED_RE = re.compile(r"""["'`]([^"'`\s]+)["'`]""")
_KEYWORD_MODULE_RE = re.compile(r"^\s*(?:pub\s+)?(?:use|mod|import)\s+(?:static\s+)?(?P<module>[\w:\\.]+)")


def language_of(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix.lower().lstrip(".")
    if not suffix:
        return None
    return _LANGUAGE_BY_EXTENSION.get(suffix, suffix)


def _strip_extension(path: str) -> str:
    pp = PurePosixPath(path)
    if pp.suffix.lower().lstrip(".") in _SOURCE_EXTENSIONS:
        return str(pp.with_suffix(""))
    return path


def is_test_path(path: str) -> bool:
    pp = PurePosixPath(path)
    if any(part in _TEST_DIRS for part in pp.parts[:-1]):
        return True
    return _TEST_STEM_RE.match(PurePosixPath(_strip_extension(pp.name)).name) is not None


def subject_of_test(path: str) -> str | None:
    """``tests/test_app.py`` -> ``app``; ``src/app.spec.ts`` -> ``app``; ``AppTest.java`` -> ``app``."""
    stem = PurePosixPath(_strip_extension(PurePosixPath(path).name)).name
    m = _TEST_STEM_RE.match(stem)
    if m is None:
        return None
    subject = m.group("prefixed") or m.group("suffixed") or ""
    return subject.lower() or None


def _subject_stem(path: str) -> str:
    pp = PurePosixPath(_strip_extension(path))
    if pp.name in _PACKAGE_STEMS and str(pp.parent) not in ("", "."):
        return pp.parent.name.lower()
    return pp.name.lower()


def _module_keys(path: str) -> set[str]:
    no_ext = _strip_extension(path)
    keys = {path, no_ext}
    pp = PurePosixPath(no_ext)
    if pp.name in _PACKAGE_STEMS and str(pp.parent) not in ("", "."):
        keys.add(str(pp.parent))
    return keys


def _dirname(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _join(base: str, rel: str) -> str:
    joined = posixpath.normpath(posixpath.join(base or ".", rel))
    return "" if joined == "." or joined.startswith("..") else joined


def _python_module_paths(module: str, importer_dir: str) -> list[str]:
    dots = len(module) - len(module.lstrip("."))
    rest = module.lstrip(".").replace(".", "/")
    if dots == 0:
        return [rest] if rest else []
    base = importer_dir
    for _ in range(dots - 1):
        base = _dirname(base) if base else ""
    target = _join(base, rest) if rest else base
    return [target] if target else []


def import_references(line: str, importer: str) -> list[str]:
    """Candidate repository paths (without extension) an import line of ``importer`` refers to."""
    importer_dir = _dirname(importer)
    stripped = line.strip()
    if stripped.startswith("export") and " from " not in stripped:
        return []
    refs: list[str] = []
    m = _PY_FROM_RE.match(line)
    if m:
        modules = _python_module_paths(m.group("module"), importer_dir)
        names = [n.strip().split(" as ")[0].strip("() ") for n in m.group("names").split(",")]
        for base in modules or [importer_dir]:
            if base:
                refs.append(base)
            refs.extend(f"{base}/{n}" if base else n for n in names if n and n != "*")
        return refs
    m = _PY_IMPORT_RE.match(line)
    if m:
        for part in m.group("names").split(","):
            refs.extend(_python_module_paths(part.strip().split(" as ")[0].strip(), importer_dir))
    for q in _QUOTED_RE.findall(line):
        refs.append(_join(importer_dir, q))  # relative specifier ("./x", "x.h") or sibling file
        if not q.startswith(("./", "../")):
            refs.append(q.lstrip("/"))  # package/absolute specifier
    km = _KEYWORD_MODULE_RE.match(line)
    if km and not m:
        module = km.group("module").rstrip(";").replace("::", "/").replace("\\", "/")
        if "/" not in module:
            module = module.replace(".", "/")
        for prefix in ("crate/", "self/", "super/"):
            module = module.removeprefix(prefix)
        module = module.strip("/")
        refs.append(module)
        if "/" in module:  # `use a::b::Item;` / `use App\\Models\\User;` – the item may name a symbol, not a file
            refs.append(module.rsplit("/", 1)[0])
    return [r for r in dict.fromkeys(_strip_extension(r) for r in refs) if r]


def _reference_matches(candidate: str, keys: set[str], *, same_dir: bool) -> bool:
    cand = candidate.strip("/").lower()
    if not cand:
        return False
    for key in (k.lower() for k in keys):
        if cand == key:
            return True
        if key.endswith("/" + cand) and ("/" in cand or same_dir):
            return True
    return False


def find_import_of(importer: str, content: str, imported: str) -> str | None:
    """The first import line of ``importer`` referring to ``imported`` (clipped), else ``None``."""
    keys = _module_keys(imported)
    same_dir = _dirname(importer) == _dirname(imported)
    for raw in content.splitlines():
        if len(raw) > 500 or not _IMPORT_LINE_RE.search(raw):
            continue
        for ref in import_references(raw, importer):
            if _reference_matches(ref, keys, same_dir=same_dir):
                return raw.strip()[:200]
    return None


# ---------------------------------------------------------------------------------------------- decisions
@dataclass(frozen=True)
class PathAssessment:
    path: str
    operation: Operation | None
    status: PathStatus
    reason: str
    classification: Classification | None = None
    signals: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "operation": self.operation,
            "status": self.status,
            "reason": self.reason,
            "classification": self.classification,
            "signals": self.signals,
        }


@dataclass(frozen=True)
class ExpansionDecision:
    """Outcome of a scope expansion request.

    ``granted`` – the runtime issued ``version`` (or the paths were already in scope, ``changed=False``);
    ``needs_replan`` – semantic expansion (or limit/caps reached): the replanner decides, the worker continues
    within its current scope or blocks; ``rejected`` – invalid/forbidden paths or no active scope.
    """

    outcome: ExpansionOutcome
    reason_code: str
    reason: str
    classification: Classification | None
    contract: ScopeContract | None
    version: int | None
    previous_version: int | None
    changed: bool
    paths: list[PathAssessment]

    @property
    def granted(self) -> bool:
        return self.outcome == "granted"

    @property
    def needs_replan(self) -> bool:
        return self.outcome == "needs_replan"

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "classification": self.classification,
            "version": self.version,
            "previous_version": self.previous_version,
            "changed": self.changed,
            "paths": [p.to_dict() for p in self.paths],
        }


class _ScopeMoved(Exception):
    """The active scope version changed between evaluation and commit – evaluate again."""


@dataclass(frozen=True)
class _Snapshot:
    job_id: uuid.UUID
    step_key: str
    contract: ScopeContract | None
    version: int | None
    status: str | None
    granted_expansions: int
    step_closed: str | None = None  # "superseded" / "completed" / "cancelled" when the step can no longer run
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------- handler
class ScopeExpansionHandler:
    """Evaluates worker scope expansion requests; only this runtime component issues expanded versions."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig,
        repo: RepoContextProvider,
        *,
        settings: ScopeEngineSettings | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.config = config
        self.repo = repo
        self.settings = settings or ScopeEngineSettings()

    @property
    def policy(self) -> ScopePolicy:
        return self.config.policies.scope

    async def handle(
        self,
        step_id: uuid.UUID,
        request: ScopeExpansionRequest,
        workspace: WorkspaceHandle,
        *,
        attempt_id: uuid.UUID | None = None,
        requested_by: str = "worker",
    ) -> ExpansionDecision:
        for _ in range(3):
            snapshot = await self._snapshot(step_id)
            if snapshot.job_id != workspace.job_id:
                raise ConflictError("workspace belongs to a different job than the step", code="scope_workspace_mismatch")
            files = await list_workspace_files(workspace.path, limit_seconds=self.settings.list_timeout_seconds)
            on_disk = await asyncio.to_thread(_paths_on_disk, workspace.path, request.paths)
            decision, new_contract, added = await self._evaluate(snapshot, request, workspace, files, on_disk)
            try:
                return await self._commit(
                    step_id,
                    snapshot,
                    request=request,
                    decision=decision,
                    new_contract=new_contract,
                    added=added,
                    attempt_id=attempt_id,
                    requested_by=requested_by,
                )
            except _ScopeMoved:
                continue
        raise ConflictError("scope changed concurrently while handling the expansion request", code="scope_expansion_conflict")

    # ------------------------------------------------------------------------------------------ evaluation
    async def _snapshot(self, step_id: uuid.UUID) -> _Snapshot:
        async with self.sessionmaker() as session:
            step = await load_step(session, step_id)
            row = await current_scope_row(session, step)
            granted = await count_runtime_expansions(session, step_id)
            contract = contract_from_row(row) if row is not None and row.status == "active" else None
            closed = "superseded" if step.superseded else (str(step.status) if str(step.status) in CLOSED_STEP_STATUSES else None)
            return _Snapshot(
                job_id=step.job_id,
                step_key=step.step_key,
                contract=contract,
                version=row.version if row is not None else None,
                status=row.status if row is not None else None,
                granted_expansions=granted,
                step_closed=closed,
                evidence=dict(row.evidence or {}) if row is not None else {},
            )

    def assess_paths(
        self, request: ScopeExpansionRequest, contract: ScopeContract, files: WorkspaceFiles, on_disk: set[str] | None = None
    ) -> list[PathAssessment]:
        """Validate every requested path against the workspace and the current scope (no classification yet)."""
        guard = ScopeGuard(contract, self.policy)
        out: list[PathAssessment] = []
        seen: set[tuple[str, str]] = set()
        disk = on_disk or set()
        for raw in request.paths:
            try:
                p = canonical_path(raw)
            except ValueError as exc:
                out.append(PathAssessment(path=str(raw)[:300], operation=None, status="invalid", reason=str(exc)))
                continue
            if any(c in _WILDCARDS for c in p) or p.endswith("/"):
                out.append(PathAssessment(p, None, "invalid", "expansion requests need explicit file paths (no globs/directories)"))
                continue
            if guard.is_forbidden(p):
                out.append(PathAssessment(p, None, "forbidden", f"path '{p}' is forbidden"))
                continue
            if files.is_dir(p):
                out.append(PathAssessment(p, None, "invalid", "is a directory; request explicit files"))
                continue
            ops, note = self._operations_for(p, request.operations, files, disk)
            if not ops:
                out.append(PathAssessment(p, None, "invalid", note))
                continue
            for op in ops:
                if (p, op) in seen:
                    continue
                seen.add((p, op))
                ok, why = guard.decide(p, op)  # the literal file path; contract entries are (escaped) patterns
                if ok:
                    out.append(PathAssessment(p, op, "already_allowed", f"already allowed ({why})"))
                else:
                    out.append(PathAssessment(p, op, "pending", note or why))
        return out

    @staticmethod
    def _operations_for(path: str, requested: Sequence[Operation], files: WorkspaceFiles, on_disk: set[str]) -> tuple[list[Operation], str]:
        if files.exists(path):
            ops: list[Operation] = [o for o in requested if o != "create"]
            note = "create requested for an existing file: treated as modify" if not ops else ""
            return ops or ["modify"], note
        if path in on_disk:
            return [], "path exists on disk but is ignored or not a regular workspace file"
        if set(requested) == {"delete"}:
            return [], "cannot delete a path that does not exist"
        if implausible_new_path(path):
            return [], "not a plausible file path (location suffix or control characters)"
        ok, why = files.creatable(path)
        if not ok:
            return [], f"cannot be created: {why}"
        note = "" if "create" in requested else "path does not exist: treated as create"
        return ["create"], note

    async def _evaluate(
        self,
        snapshot: _Snapshot,
        request: ScopeExpansionRequest,
        workspace: WorkspaceHandle,
        files: WorkspaceFiles,
        on_disk: set[str],
    ) -> tuple[ExpansionDecision, ScopeContract | None, dict[str, list[str]]]:
        no_added: dict[str, list[str]] = {}
        forbidden, _ = merged_forbidden(self.policy, [])
        current = snapshot.contract or deny_all_contract(forbidden=forbidden, version=snapshot.version or 1)
        assessments = self.assess_paths(request, current, files, on_disk)

        def decide(
            outcome: ExpansionOutcome, code: str, reason: str, cls: Classification | None, paths: list[PathAssessment]
        ) -> ExpansionDecision:
            return ExpansionDecision(
                outcome=outcome,
                reason_code=code,
                reason=reason,
                classification=cls,
                contract=snapshot.contract,
                version=snapshot.version if snapshot.contract is not None else None,
                previous_version=snapshot.version,
                changed=False,
                paths=paths,
            )

        if snapshot.step_closed is not None:
            why = f"step {snapshot.step_key} is {snapshot.step_closed}; its scope can no longer change"
            return decide("rejected", "step_closed", why, None, assessments), None, no_added
        if snapshot.contract is None:
            why = f"step has no active scope (current status: {snapshot.status or 'none'}); a worker cannot create scope"
            return decide("rejected", "no_active_scope", why, None, assessments), None, no_added
        bad = [a for a in assessments if a.status in ("invalid", "forbidden")]
        if bad:
            code = "forbidden_paths" if any(a.status == "forbidden" for a in bad) else "invalid_paths"
            why = "; ".join(f"{a.path}: {a.reason}" for a in bad)[:1000]
            return decide("rejected", code, why, None, assessments), None, no_added
        pending = [a for a in assessments if a.status == "pending"]
        if not pending:
            return decide("granted", "already_in_scope", "all requested paths are already in scope", None, assessments), None, no_added
        if snapshot.granted_expansions >= self.settings.max_expansions_per_step:
            why = f"step already received {snapshot.granted_expansions} runtime expansions (max {self.settings.max_expansions_per_step})"
            return decide("needs_replan", "expansion_limit_reached", why, "semantic", assessments), None, no_added

        classified = await self._classify(pending, snapshot.contract, workspace, files)
        assessments = [classified.get((a.path, a.operation), a) for a in assessments]
        pending = [a for a in assessments if a.status == "pending"]
        if any(a.classification == "semantic" for a in pending):
            semantic = [a.path for a in pending if a.classification == "semantic"]
            why = f"semantic expansion ({', '.join(semantic[:10])}): no mechanical relation to the current scope; replanner decides"
            return decide("needs_replan", "semantic_expansion", why, "semantic", assessments), None, no_added

        contract = snapshot.contract
        # requested paths are literal files: escape "[" so ScopeGuard matches exactly that file
        add_targets = [
            glob_escape(a.path) for a in pending if a.operation in ("modify", "delete") and glob_escape(a.path) not in contract.target_paths
        ]
        add_new = [
            glob_escape(a.path) for a in pending if a.operation == "create" and glob_escape(a.path) not in contract.allowed_new_paths
        ]
        ops = set(contract.allowed_operations) | {a.operation for a in pending if a.operation is not None}
        targets = [*contract.target_paths, *dict.fromkeys(add_targets)]
        new_paths = [*contract.allowed_new_paths, *dict.fromkeys(add_new)]
        if len(targets) > self.policy.max_target_paths or len(new_paths) > self.policy.max_new_paths:
            why = (
                f"expanded scope would exceed policy caps ({len(targets)}/{self.policy.max_target_paths} targets, "
                f"{len(new_paths)}/{self.policy.max_new_paths} new paths)"
            )
            return decide("needs_replan", "scope_caps_exceeded", why, "mechanical", assessments), None, no_added
        new_contract = ScopeContract(
            source="runtime_expansion",
            version=(snapshot.version or 0) + 1,
            strict_target_paths=contract.strict_target_paths,
            target_paths=targets,
            allowed_new_paths=new_paths,
            forbidden_paths=contract.forbidden_paths,
            allowed_operations=[o for o in _OPERATION_ORDER if o in ops],
            reason=f"runtime expansion of v{snapshot.version}: {redact(request.justification)[:300]}",
        )
        added_ops = [o for o in _OPERATION_ORDER if o in ops and o not in contract.allowed_operations]
        added: dict[str, list[str]] = {
            "target_paths": list(dict.fromkeys(add_targets)),
            "allowed_new_paths": list(dict.fromkeys(add_new)),
            "operations": [str(o) for o in added_ops],
        }
        decision = ExpansionDecision(
            outcome="granted",
            reason_code="mechanical_expansion",
            reason="mechanical expansion granted by the runtime",
            classification="mechanical",
            contract=new_contract,
            version=None,  # assigned on commit
            previous_version=snapshot.version,
            changed=True,
            paths=assessments,
        )
        return decision, new_contract, added

    async def _classify(
        self, pending: list[PathAssessment], contract: ScopeContract, workspace: WorkspaceHandle, files: WorkspaceFiles
    ) -> dict[tuple[str, Operation | None], PathAssessment]:
        # concrete files of the current scope (escaped literals are unescaped, globs/directories are skipped)
        literals = (literal_path(p) for p in dict.fromkeys([*contract.target_paths, *contract.allowed_new_paths]))
        scope_files = [p for p in dict.fromkeys(literals) if p is not None]
        contents = _ContentCache(self.repo, workspace, files, self.settings.read_max_chars, self.settings.repo_timeout_seconds)
        out: dict[tuple[str, Operation | None], PathAssessment] = {}
        for a in pending:
            signals: dict[str, str] = {}
            if a.operation == "delete":
                cls: Classification = "semantic"
                reason = "deleting files is always a semantic scope change"
            else:
                signals = await self._mechanical_signals(a.path, scope_files, contents)
                cls = "mechanical" if signals else "semantic"
                if signals:
                    relation = next(iter(signals))
                    reason = f"mechanical: {relation.replace('_', ' ')} {signals[relation]}"
                else:
                    reason = "semantic: no test/import/same-directory relation to the current scope files"
            if a.reason:
                reason = f"{reason} ({a.reason})"
            out[(a.path, a.operation)] = PathAssessment(a.path, a.operation, a.status, reason, cls, signals)
        return out

    async def _mechanical_signals(self, path: str, scope_files: Sequence[str], contents: _ContentCache) -> dict[str, str]:
        signals: dict[str, str] = {}
        if is_test_path(path):
            subject = subject_of_test(path)
            for t in scope_files:
                if subject is not None and subject == _subject_stem(t) and not is_test_path(t):
                    signals["test_of"] = t
                    break
            if "test_of" not in signals:
                content = await contents.get(path)
                if content:
                    for t in scope_files:
                        line = find_import_of(path, content, t)
                        if line:
                            signals["test_imports"] = t
                            signals["evidence"] = line
                            break
            if signals:
                return signals
        for t in scope_files:
            content = await contents.get(t)
            if not content:
                continue
            line = find_import_of(t, content, path)
            if line:
                signals["imported_by"] = t
                signals["evidence"] = line
                return signals
        lang = language_of(path)
        if lang is not None:
            for t in scope_files:
                if _dirname(t) == _dirname(path) and language_of(t) == lang:
                    signals["same_directory_as"] = t
                    signals["language"] = lang
                    return signals
        return signals

    # ------------------------------------------------------------------------------------------ commit
    async def _commit(
        self,
        step_id: uuid.UUID,
        snapshot: _Snapshot,
        *,
        request: ScopeExpansionRequest,
        decision: ExpansionDecision,
        new_contract: ScopeContract | None,
        added: dict[str, list[str]],
        attempt_id: uuid.UUID | None,
        requested_by: str,
    ) -> ExpansionDecision:
        async with self.sessionmaker() as session, session.begin():
            step = await load_step(session, step_id, for_update=True)
            if step.current_scope_version != snapshot.version:
                raise _ScopeMoved
            row = await current_scope_row(session, step, for_update=True)
            if row is not None and (row.status == "active") != (snapshot.contract is not None):
                raise _ScopeMoved
            record = _request_record(request, decision, attempt_id, requested_by)
            final_decision = decision
            if new_contract is not None:
                _row, final, previous = await persist_scope_version(
                    session,
                    step,
                    new_contract,
                    status="active",
                    evidence=_expansion_evidence(snapshot, record, added),
                    reason=new_contract.reason,
                )
                record["granted_version"] = final.version
                final_decision = ExpansionDecision(
                    outcome="granted",
                    reason_code=decision.reason_code,
                    reason=f"{decision.reason}: v{previous} -> v{final.version}",
                    classification=decision.classification,
                    contract=final,
                    version=final.version,
                    previous_version=previous,
                    changed=True,
                    paths=decision.paths,
                )
            if row is not None:
                _append_request(row, record)
            await append_event(
                session,
                EventType.SCOPE_EXPANSION_REQUESTED,
                source_type=SOURCE_TYPE,
                source_id=SOURCE_ID,
                job_id=step.job_id,
                step_id=step.id,
                attempt_id=attempt_id,
                severity=Severity.warning if final_decision.outcome == "rejected" else Severity.info,
                payload={"step_key": step.step_key, "requested_by": requested_by, **record},
            )
            if final_decision.changed and final_decision.contract is not None:
                await append_event(
                    session,
                    EventType.SCOPE_EXPANDED,
                    source_type=SOURCE_TYPE,
                    source_id=SOURCE_ID,
                    job_id=step.job_id,
                    step_id=step.id,
                    attempt_id=attempt_id,
                    payload={
                        **contract_event_payload(
                            final_decision.contract, status="active", previous_version=final_decision.previous_version
                        ),
                        "step_key": step.step_key,
                        "added": added,
                        "classification": "mechanical",
                    },
                )
        log.info(
            "scope expansion handled",
            extra={"step_id": str(step_id), "outcome": final_decision.outcome, "reason_code": final_decision.reason_code},
        )
        return final_decision


def _expansion_evidence(snapshot: _Snapshot, record: dict[str, Any], added: dict[str, list[str]]) -> dict[str, Any]:
    evidence: dict[str, Any] = {"expanded_from": snapshot.version, "request": record, "added": added}
    deletes = designated_deletes(snapshot.evidence)
    if deletes is not None:  # expansions never grant deletions (always semantic): keep the generated designation
        evidence["delete_paths"] = [{"path": p, "sources": [f"carried_from:v{snapshot.version}"]} for p in deletes]
    return evidence


def _request_record(
    request: ScopeExpansionRequest, decision: ExpansionDecision, attempt_id: uuid.UUID | None, requested_by: str
) -> dict[str, Any]:
    return {
        "at": now_iso(),
        "requested_by": requested_by[:100],
        "attempt_id": str(attempt_id) if attempt_id else None,
        "paths": [str(p)[:300] for p in request.paths],
        "operations": list(request.operations),
        "justification": redact(request.justification)[:500],
        "outcome": decision.outcome,
        "reason_code": decision.reason_code,
        "reason": decision.reason[:1000],
        "classification": decision.classification,
        "assessments": [a.to_dict() for a in decision.paths][:40],
    }


def _append_request(row: ScopeContractRow, record: dict[str, Any]) -> None:
    evidence = dict(row.evidence or {})
    history = list(evidence.get("expansion_requests") or [])
    history.append(redact(record))
    evidence["expansion_requests"] = history[-_MAX_REQUEST_RECORDS:]
    row.evidence = evidence  # reassign: JSONB columns are not mutation-tracked


def _paths_on_disk(root: Path, raw_paths: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for raw in raw_paths:
        try:
            p = canonical_path(raw)
        except ValueError:
            continue
        if any(c in _WILDCARDS for c in p):
            continue
        try:
            if (root / p).exists() or (root / p).is_symlink():
                out.add(p)
        except OSError:
            continue
    return out


class _ContentCache:
    """Reads scope files through the RepoContextProvider once per request (missing/unreadable -> empty)."""

    def __init__(
        self, repo: RepoContextProvider, workspace: WorkspaceHandle, files: WorkspaceFiles, max_chars: int, timeout: float
    ) -> None:
        self._timeout = timeout
        self._repo = repo
        self._workspace = workspace
        self._files = files
        self._max_chars = max_chars
        self._cache: dict[str, str] = {}

    async def get(self, path: str) -> str:
        if path in self._cache:
            return self._cache[path]
        content = ""
        if self._files.exists(path):
            try:
                content = await asyncio.wait_for(self._repo.read(self._workspace, path, 1, None, max_chars=self._max_chars), self._timeout)
            except Exception as exc:  # unreadable file = no evidence, never a crash
                log.debug("scope expansion could not read %s: %s", path, type(exc).__name__)
                content = ""
        self._cache[path] = content
        return content
