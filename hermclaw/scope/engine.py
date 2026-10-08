"""ScopeEngine – runtime generation and versioning of explicit write scopes (Bauplan §17, P15 15.1-15.6, 15.8).

The runtime – never a worker – turns planner hints plus repository-intelligence evidence into a versioned
``ScopeContract``:

1. **hint intake (15.1)** – ``step.repo_hints``, ``step.allowed_new_paths``, ``step.forbidden_paths``,
   ``step.acceptance`` and ``step.constraints`` are read from the persisted step.
2. **repository evidence (15.2)** – every hint is classified as glob / directory / path / symbol / free text.
   Paths and globs are resolved against the real workspace file list (``git ls-files --cached --others
   --exclude-standard`` filtered by actual existence, symlinks escaping the workspace are dropped). Symbols and
   text are located through :class:`~hermclaw.core.interfaces.RepoContextProvider`; only high-confidence hits
   become targets and every score is kept as evidence.
3. **policy merge (15.3) / forbidden paths (15.5)** – ``policies.scope.always_forbidden`` + step forbidden paths
   are merged into the contract; forbidden candidates are excluded *with evidence*. The caps
   ``max_target_paths`` / ``max_new_paths`` are never truncated silently: exceeding them makes the scope
   unavailable.
4. **generation (15.4)** – strict contract (``source=planner_and_repo_intelligence``) whose operations are derived
   from the step: ``create`` iff new paths exist, ``modify`` iff targets exist, ``delete`` only when acceptance
   demands the absence of an existing path or a constraint explicitly asks to delete it.
5. **versioning (15.6)** – ``scope_contracts`` rows ``1..n`` per step, the previous active version becomes
   ``superseded``, ``steps.current_scope_version`` points at the newest version.
6. **unavailable (15.8)** – nothing resolvable (or a cap exceeded) yields a persisted ``unavailable`` row with a
   deny-all contract and full evidence plus ``scope.unavailable``; the caller must block or replan. Nothing is
   restored, committed or pushed here.

Expansion (15.7) lives in :mod:`hermclaw.scope.expansion`, auditing (15.9) in :mod:`hermclaw.scope.audit`; both
reuse the persistence helpers of this module. All path permission decisions are delegated to
:class:`~hermclaw.scope.guard.ScopeGuard` / :func:`~hermclaw.scope.guard.path_matches`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.acceptance import AbsenceEvidence, AcceptanceCriterion, PresenceEvidence
from hermclaw.contracts.common import MUTATING_STEP_KINDS, Severity, StepKind, StepStatus
from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import Operation, ScopeContract, normalise_path
from hermclaw.core.config import HermclawConfig, ScopePolicy
from hermclaw.core.errors import ConflictError, HermclawError, NotFoundError
from hermclaw.core.interfaces import RepoContextProvider, RepoHit, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import redact
from hermclaw.events.store import append_event
from hermclaw.persistence.models import ScopeContractRow, Step
from hermclaw.scope.guard import ScopeGuard, any_match, path_matches

log = get_logger(__name__)

SOURCE_TYPE = "runtime"
SOURCE_ID = "scope_engine"

ScopeStatus = Literal["active", "unavailable"]
HintKind = Literal["glob", "directory", "path", "symbol", "text", "invalid"]
HintStatus = Literal["resolved", "new_path", "unresolved", "ambiguous", "invalid", "error"]

#: kinds that write files inside the job workspace; ssh/deploy mutate remote hosts and are governed by the SSH /
#: deployment policies, their workspace scope is read-only.
WORKSPACE_WRITE_KINDS = frozenset(k.value for k in MUTATING_STEP_KINDS) - {StepKind.ssh.value, StepKind.deploy.value}
#: kinds that may create new files (hinted paths that do not exist yet become allowed new paths)
CREATING_KINDS = WORKSPACE_WRITE_KINDS
_CLOSED_STEP_STATUSES = frozenset({StepStatus.completed.value, StepStatus.cancelled.value})

GLOB_CHARS = frozenset("*?[")
_SYMBOL_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::|#|->|\\)[A-Za-z_$][\w$]*)*(?:\(\))?$")
_SYMBOL_SPLIT_RE = re.compile(r"\.|::|#|->|\\")
_EXTENSION_LIST = """py pyi pyx ipynb js mjs cjs jsx ts tsx mts cts vue svelte astro php phtml inc rb erb go rs java kt kts scala
    groovy c h cc cpp cxx hpp hh cs fs swift m mm sh bash zsh fish ps1 bat cmd sql html htm css scss sass less
    json jsonc json5 yaml yml toml ini cfg conf env xml md mdx rst txt adoc lock gradle properties tf tfvars hcl
    j2 jinja jinja2 twig tpl mustache hbs service timer socket dockerfile containerfile csv tsv svg png jpg jpeg
    gif webp ico proto graphql gql mod sum dist pem crt key pub log patch diff"""
_FILE_EXTENSIONS = frozenset(_EXTENSION_LIST.split())
_EVIDENCE_HITS = 10
_EVENT_PATHS = 50

_ACCEPTANCE_ADAPTER: TypeAdapter[AcceptanceCriterion] = TypeAdapter(AcceptanceCriterion)

# constraint phrases that explicitly ask for the deletion of a path ("delete old/x.py", "x.py löschen")
_PATH_PART = r"(?<![\w./\-])[`'\"]?(?P<path>[A-Za-z0-9_.\-/*?\[\]]+?)[`'\"]?(?=$|[\s,;:)])"
_DELETE_BEFORE_RE = re.compile(
    r"(?i)\b(?:delete|remove|rm|unlink|lösche|loesche|entferne)\s+"
    r"(?:(?:the|die|das|den)\s+)?(?:(?:file|files|datei|dateien|directory|dir|folder|ordner|verzeichnis)\s+)?" + _PATH_PART
)
_DELETE_AFTER_RE = re.compile(r"(?i)" + _PATH_PART + r"\s+(?:löschen|loeschen|entfernen)\b")
_NEGATION_RE = re.compile(r"(?i)\b(?:not|never|no|don'?t|without|nicht|niemals|keine?n?)\b")
_CLAUSE_SPLIT_RE = re.compile(r"(?:[;\n]|\.(?=\s|$))+\s*")
_LINE_SUFFIX_RE = re.compile(r"^(?P<path>.+?):\d+(?:-\d+)?$")


class ScopeEngineError(HermclawError):
    code = "scope_engine_error"


# ============================================================================================= settings
@dataclass(frozen=True)
class ScopeEngineSettings:
    """Tunables of the scope engine (scores are expected normalised to ``[0, 1]``; larger values are clamped)."""

    min_symbol_score: float = 0.75
    min_search_score: float = 0.85
    relative_score_floor: float = 0.8  # a hit must reach this fraction of the best hit for the same hint
    max_files_per_hint: int = 5  # more confident files than this -> hint is ambiguous, nothing is taken
    search_k: int = 20
    max_expansions_per_step: int = 3
    read_max_chars: int = 40_000
    list_timeout_seconds: float = 30.0


# ============================================================================================= workspace files
@dataclass(frozen=True)
class WorkspaceFiles:
    """Regular files of a workspace (tracked + untracked, not ignored, existing on disk, inside the workspace)."""

    root: Path
    files: frozenset[str]
    dirs: frozenset[str]
    source: Literal["git", "walk"]

    def exists(self, path: str) -> bool:
        return path in self.files

    def is_dir(self, path: str) -> bool:
        return path.rstrip("/") in self.dirs

    def match(self, pattern: str) -> list[str]:
        return sorted(f for f in self.files if path_matches(f, pattern))

    def under(self, directory: str) -> list[str]:
        return self.match(directory.rstrip("/") + "/")

    def creatable(self, path: str) -> tuple[bool, str]:
        """A path can be created when it is no existing file/directory and no parent component is a file."""
        if path in self.files:
            return False, "already exists"
        if path.rstrip("/") in self.dirs or path.endswith("/"):
            return False, "is a directory"
        parts = PurePosixPath(path).parts
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if parent in self.files:
                return False, f"parent '{parent}' is a file"
        return True, "creatable"


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    return env


async def _git_ls_files(root: Path, limit_seconds: float) -> list[str] | None:
    """Tracked + untracked (non-ignored) paths, or ``None`` when ``root`` is no git work tree."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
    except FileNotFoundError:  # git binary missing
        return None
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), limit_seconds)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise ScopeEngineError(f"listing workspace files timed out after {limit_seconds:.0f}s", code="scope_listing_timeout") from exc
    if proc.returncode != 0:
        return None
    return [p for p in out.decode("utf-8", errors="surrogateescape").split("\0") if p]


def _walk_files(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        rel_dir = Path(dirpath).relative_to(root)
        for name in filenames:
            out.append((rel_dir / name).as_posix())
    return out


def _filter_existing(root: Path, raw: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    resolved_root = root.resolve()
    files: set[str] = set()
    for rel in raw:
        try:
            p = normalise_path(rel)
        except ValueError:
            continue
        if p == ".git" or p.startswith(".git/") or "/.git/" in f"/{p}":
            continue
        candidate = root / p
        try:
            if candidate.is_symlink():
                target = candidate.resolve()
                if not target.is_relative_to(resolved_root) or not target.is_file():
                    continue
            elif not candidate.is_file():
                continue
        except OSError:
            continue
        files.add(p)
    dirs: set[str] = set()
    for f in files:
        parts = PurePosixPath(f).parts
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return frozenset(files), frozenset(dirs)


async def list_workspace_files(root: Path, *, limit_seconds: float = 30.0) -> WorkspaceFiles:
    """List regular files of a workspace: ``git ls-files`` (+ untracked) when it is a git work tree, else a walk."""
    raw = await _git_ls_files(root, limit_seconds)
    source: Literal["git", "walk"] = "git"
    if raw is None:
        if not await asyncio.to_thread(root.is_dir):
            raise ScopeEngineError(f"workspace path does not exist: {root}", code="scope_workspace_missing")
        raw = await asyncio.to_thread(_walk_files, root)
        source = "walk"
    files, dirs = await asyncio.to_thread(_filter_existing, root, raw)
    return WorkspaceFiles(root=root, files=files, dirs=dirs, source=source)


# ============================================================================================= literal paths
_ESCAPED_CHAR_RE = re.compile(r"\[([\[*?])\]")


def glob_escape(path: str) -> str:
    """Escape glob characters of a literal file name so ScopeGuard matches exactly that file (``[`` -> ``[[]``)."""
    return re.sub(r"[\[*?]", lambda m: f"[{m.group(0)}]", path)


def literal_path(pattern: str) -> str | None:
    """The literal path a scope entry denotes (undoing :func:`glob_escape`), ``None`` for real globs/directories."""
    if pattern.endswith("/") or any(c in GLOB_CHARS for c in _ESCAPED_CHAR_RE.sub("", pattern)):
        return None
    return _ESCAPED_CHAR_RE.sub(r"\1", pattern)


# ============================================================================================= hint classification
def _strip_line_suffix(hint: str, files: WorkspaceFiles) -> str:
    """``src/app.py:12`` / ``src/app.py:12-40`` -> ``src/app.py`` when that file exists."""
    m = _LINE_SUFFIX_RE.match(hint)
    if m is None:
        return hint
    try:
        base = normalise_path(m.group("path"))
    except ValueError:
        return hint
    return base if files.exists(base) else hint


def _has_file_extension(path: str) -> bool:
    name = PurePosixPath(path).name
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS


def classify_hint(hint: str, files: WorkspaceFiles) -> HintKind:
    """Generic classification of a planner repo hint (no project-specific rules)."""
    h = hint.strip()
    if not h:
        return "invalid"
    try:
        p: str | None = normalise_path(h)
    except ValueError:
        p = None
    if p is not None:  # an existing literal path wins (file names may contain "[", e.g. "routes/[id].tsx")
        if files.exists(p):
            return "path"
        if files.is_dir(p):
            return "directory"
    if any(ch.isspace() for ch in h):
        return "text"  # paths/globs/symbols never contain whitespace; free text goes to repository search
    if any(c in GLOB_CHARS for c in h):
        return "glob" if p is not None else "invalid"
    if h.endswith("/"):
        return "directory" if p is not None else "invalid"
    if "/" in h:
        return "path" if p is not None else "invalid"
    if p is not None and (_has_file_extension(p) or h.startswith(".")):
        return "path"
    if _SYMBOL_RE.match(h):
        return "symbol"
    if "\\" in h:
        return "path" if p is not None else "invalid"
    return "text"


# ============================================================================================= evidence records
@dataclass
class HintEvidence:
    hint: str
    kind: HintKind
    status: HintStatus = "unresolved"
    targets: list[str] = field(default_factory=list)
    new_paths: list[str] = field(default_factory=list)
    hits: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "hint": self.hint[:300],
            "kind": self.kind,
            "status": self.status,
            "targets": self.targets,
            "new_paths": self.new_paths,
            "hits": self.hits,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ConfidentHits:
    accepted: list[str]
    ambiguous: bool
    records: list[dict[str, Any]]


def select_confident_hits(
    hits: Sequence[RepoHit], files: WorkspaceFiles, *, min_score: float, relative_floor: float, max_files: int
) -> ConfidentHits:
    """Keep only high-confidence hits on existing workspace files; too many confident files means ambiguity."""
    best: dict[str, RepoHit] = {}
    records: list[dict[str, Any]] = []
    for hit in hits:
        try:
            p = normalise_path(hit.path)
        except ValueError:
            records.append({"path": hit.path[:300], "score": round(_clamp(hit.score), 4), "accepted": False, "note": "invalid path"})
            continue
        if not files.exists(p):
            records.append({"path": p, "score": round(_clamp(hit.score), 4), "accepted": False, "note": "not in workspace"})
            continue
        if p not in best or _clamp(hit.score) > _clamp(best[p].score):
            best[p] = hit
    top = max((_clamp(h.score) for h in best.values()), default=0.0)
    accepted = sorted(
        (p for p, h in best.items() if _clamp(h.score) >= min_score and _clamp(h.score) >= top * relative_floor),
        key=lambda p: (-_clamp(best[p].score), p),
    )
    ambiguous = len(accepted) > max_files
    for p, h in sorted(best.items(), key=lambda kv: (-_clamp(kv[1].score), kv[0])):
        records.append(
            {
                "path": p,
                "score": round(_clamp(h.score), 4),
                "lines": [h.start_line, h.end_line],
                "signals": {k: round(float(v), 4) for k, v in list(h.signals.items())[:8]},
                "accepted": p in accepted and not ambiguous,
            }
        )
    records.sort(key=lambda r: (not r["accepted"], -float(r["score"])))
    return ConfidentHits(accepted=[] if ambiguous else accepted, ambiguous=ambiguous, records=records[:_EVIDENCE_HITS])


def _clamp(score: float) -> float:
    return max(0.0, min(1.0, float(score)))


def parse_acceptance(raw: Iterable[Any]) -> list[AcceptanceCriterion]:
    """Validate persisted acceptance items; invalid entries are skipped (the planner validator owns them)."""
    out: list[AcceptanceCriterion] = []
    for item in raw:
        try:
            out.append(_ACCEPTANCE_ADAPTER.validate_python(item))
        except ValidationError:
            continue
    return out


def constraint_delete_tokens(constraints: Iterable[Any]) -> list[str]:
    """Path tokens that a non-negated constraint explicitly asks to delete."""
    tokens: list[str] = []
    for constraint in constraints:
        if not isinstance(constraint, str):
            continue
        for clause in _CLAUSE_SPLIT_RE.split(constraint):
            for regex in (_DELETE_BEFORE_RE, _DELETE_AFTER_RE):
                for m in regex.finditer(clause):
                    prefix = clause[max(0, m.start() - 30) : m.start()]
                    if _NEGATION_RE.search(prefix):
                        continue
                    tokens.append(m.group("path"))
    out: list[str] = []
    for t in tokens:
        try:
            p = normalise_path(t.rstrip(".,:"))
        except ValueError:
            continue
        if p not in out:
            out.append(p)
    return out


# ============================================================================================= decisions
@dataclass(frozen=True)
class ScopeDecision:
    """Result of :meth:`ScopeEngine.create_scope`. ``contract`` is ``None`` when the scope is unavailable."""

    status: ScopeStatus
    contract: ScopeContract | None
    version: int
    scope_id: uuid.UUID
    reason: str
    reason_code: str | None
    evidence: dict[str, Any]

    @property
    def runnable(self) -> bool:
        return self.status == "active"


# ============================================================================================= persistence helpers
def merged_forbidden(policy: ScopePolicy, step_forbidden: Iterable[str]) -> tuple[list[str], list[dict[str, str]]]:
    """Merge ``always_forbidden`` with step forbidden paths (normalised, de-duplicated); returns (merged, rejected)."""
    merged: list[str] = []
    rejected: list[dict[str, str]] = []
    for raw in [*policy.always_forbidden, *step_forbidden]:
        if not isinstance(raw, str):
            continue
        try:
            p = normalise_path(raw)
        except ValueError as exc:
            rejected.append({"path": str(raw)[:300], "reason": str(exc)})
            continue
        if p not in merged:
            merged.append(p)
    return merged, rejected


def deny_all_contract(*, version: int = 1, forbidden: Sequence[str] = (), reason: str = "", source: str = "") -> ScopeContract:
    """A contract that allows nothing – persisted for unavailable scopes and used when no scope exists."""
    return ScopeContract.model_validate(
        {
            "source": source or "planner_and_repo_intelligence",
            "version": version,
            "strict_target_paths": True,
            "target_paths": [],
            "allowed_new_paths": [],
            "forbidden_paths": list(forbidden),
            "allowed_operations": [],
            "reason": reason[:2000],
        }
    )


async def load_step(session: AsyncSession, step_id: uuid.UUID, *, for_update: bool = False) -> Step:
    stmt = select(Step).where(Step.id == step_id)
    if for_update:
        stmt = stmt.with_for_update()
    step = (await session.execute(stmt)).scalar_one_or_none()
    if step is None:
        raise NotFoundError(f"step {step_id} not found", code="step_not_found")
    return step


async def current_scope_row(session: AsyncSession, step: Step, *, for_update: bool = False) -> ScopeContractRow | None:
    """The row ``steps.current_scope_version`` points at (any status), or ``None``."""
    if step.current_scope_version is None:
        return None
    stmt = select(ScopeContractRow).where(ScopeContractRow.step_id == step.id, ScopeContractRow.version == step.current_scope_version)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def contract_from_row(row: ScopeContractRow) -> ScopeContract:
    return ScopeContract.model_validate(row.contract)


async def count_runtime_expansions(session: AsyncSession, step_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(
        ScopeContractRow.step_id == step_id,
        func.jsonb_extract_path_text(ScopeContractRow.contract, "source") == "runtime_expansion",
    )
    return int((await session.execute(stmt)).scalar_one())


async def persist_scope_version(
    session: AsyncSession,
    step: Step,
    contract: ScopeContract,
    *,
    status: ScopeStatus,
    evidence: dict[str, Any],
    reason: str,
) -> tuple[ScopeContractRow, ScopeContract, int | None]:
    """Insert the next scope version for ``step`` (caller holds the step row lock).

    Supersedes the previous *active* version and moves ``steps.current_scope_version``. Returns the row, the
    contract with its final version number and the previous current version.
    """
    max_version = (await session.execute(select(func.max(ScopeContractRow.version)).where(ScopeContractRow.step_id == step.id))).scalar()
    version = int(max_version or 0) + 1
    final = ScopeContract.model_validate({**contract.model_dump(mode="json"), "version": version})
    await session.execute(
        update(ScopeContractRow).where(ScopeContractRow.step_id == step.id, ScopeContractRow.status == "active").values(status="superseded")
    )
    row = ScopeContractRow(
        job_id=step.job_id,
        step_id=step.id,
        version=version,
        status=status,
        contract=final.model_dump(mode="json"),
        evidence=redact(evidence),
        reason=reason[:4000],
    )
    session.add(row)
    previous = step.current_scope_version
    step.current_scope_version = version
    await session.flush()
    return row, final, previous


def contract_event_payload(contract: ScopeContract, *, status: str, previous_version: int | None) -> dict[str, Any]:
    return {
        "version": contract.version,
        "previous_version": previous_version,
        "status": status,
        "source": contract.source,
        "strict_target_paths": contract.strict_target_paths,
        "allowed_operations": list(contract.allowed_operations),
        "target_count": len(contract.target_paths),
        "new_path_count": len(contract.allowed_new_paths),
        "target_paths": contract.target_paths[:_EVENT_PATHS],
        "allowed_new_paths": contract.allowed_new_paths[:_EVENT_PATHS],
        "forbidden_paths": contract.forbidden_paths[:_EVENT_PATHS],
    }


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ============================================================================================= engine
@dataclass
class _Draft:
    """Mutable scope candidate assembled during generation (ordered, de-duplicated, with provenance)."""

    targets: dict[str, list[str]] = field(default_factory=dict)
    new_paths: dict[str, list[str]] = field(default_factory=dict)
    delete_paths: dict[str, list[str]] = field(default_factory=dict)
    excluded: list[dict[str, str]] = field(default_factory=list)

    @staticmethod
    def _add(bucket: dict[str, list[str]], path: str, source: str) -> None:
        sources = bucket.setdefault(path, [])
        if source not in sources:
            sources.append(source)

    def target(self, path: str, source: str) -> None:
        self._add(self.targets, path, source)

    def new_path(self, path: str, source: str) -> None:
        self._add(self.new_paths, path, source)

    def delete(self, path: str, source: str) -> None:
        self._add(self.delete_paths, path, source)
        self.target(path, source)

    def exclude(self, path: str, reason: str, *, bucket: str) -> None:
        self.excluded.append({"path": path, "reason": reason, "list": bucket})


class ScopeEngine:
    """Creates explicit, versioned write scopes for steps (the worker never does)."""

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

    # ------------------------------------------------------------------------------------------ public API
    async def create_scope(self, step_id: uuid.UUID, workspace: WorkspaceHandle) -> ScopeDecision:
        """Generate, persist and announce the next scope version for ``step_id``.

        Never raises for "no scope possible" – that is an ``unavailable`` decision the caller must honour by
        blocking or replanning the step. Raises ``NotFoundError`` / ``ConflictError`` for unknown or closed steps.
        """
        async with self.sessionmaker() as session:
            step = await load_step(session, step_id)
            snapshot = _StepSnapshot.of(step)
        if snapshot.superseded or snapshot.status in _CLOSED_STEP_STATUSES:
            raise ConflictError(
                f"step {snapshot.step_key} is {'superseded' if snapshot.superseded else snapshot.status}; no new scope",
                code="scope_step_closed",
            )
        if snapshot.job_id != workspace.job_id:
            raise ConflictError("workspace belongs to a different job than the step", code="scope_workspace_mismatch")

        files = await list_workspace_files(workspace.path, limit_seconds=self.settings.list_timeout_seconds)
        status, contract, evidence, reason, reason_code = await self._generate(snapshot, workspace, files)

        async with self.sessionmaker() as session, session.begin():
            step = await load_step(session, step_id, for_update=True)
            row, final, previous = await persist_scope_version(session, step, contract, status=status, evidence=evidence, reason=reason)
            payload = contract_event_payload(final, status=status, previous_version=previous)
            if status == "active":
                await append_event(
                    session,
                    EventType.SCOPE_CREATED,
                    source_type=SOURCE_TYPE,
                    source_id=SOURCE_ID,
                    job_id=step.job_id,
                    step_id=step.id,
                    payload={**payload, "step_key": step.step_key, "read_only": evidence.get("read_only", False)},
                )
            else:
                await append_event(
                    session,
                    EventType.SCOPE_UNAVAILABLE,
                    source_type=SOURCE_TYPE,
                    source_id=SOURCE_ID,
                    job_id=step.job_id,
                    step_id=step.id,
                    severity=Severity.warning,
                    payload={
                        **payload,
                        "step_key": step.step_key,
                        "reason": reason,
                        "reason_code": reason_code,
                        "unresolved_hints": [h["hint"] for h in evidence.get("hints", []) if h.get("status") != "resolved"][:20],
                    },
                )
            scope_id = row.id
        log.info(
            "scope generated",
            extra={"step_id": str(step_id), "scope_version": final.version, "scope_status": status, "reason_code": reason_code},
        )
        return ScopeDecision(
            status=status,
            contract=final if status == "active" else None,
            version=final.version,
            scope_id=scope_id,
            reason=reason,
            reason_code=reason_code,
            evidence=row.evidence,
        )

    async def current_contract(self, step_id: uuid.UUID) -> ScopeContract | None:
        """The active contract of a step, ``None`` when the step has no active scope."""
        async with self.sessionmaker() as session:
            step = await load_step(session, step_id)
            row = await current_scope_row(session, step)
            if row is None or row.status != "active":
                return None
            return contract_from_row(row)

    async def guard_for(self, step_id: uuid.UUID) -> ScopeGuard:
        """A ScopeGuard for the active scope; deny-all when the step has none (no silent access)."""
        contract = await self.current_contract(step_id)
        if contract is None:
            forbidden, _ = merged_forbidden(self.policy, [])
            contract = deny_all_contract(forbidden=forbidden, reason="no active scope")
        return ScopeGuard(contract, self.policy)

    # ------------------------------------------------------------------------------------------ generation
    async def _generate(
        self, step: _StepSnapshot, workspace: WorkspaceHandle, files: WorkspaceFiles
    ) -> tuple[ScopeStatus, ScopeContract, dict[str, Any], str, str | None]:
        forbidden, rejected_forbidden = merged_forbidden(self.policy, step.forbidden_paths)
        evidence: dict[str, Any] = {
            "generated_at": now_iso(),
            "step_key": step.step_key,
            "kind": step.kind,
            "workspace": {
                "id": str(workspace.id),
                "branch": workspace.branch,
                "base_sha": workspace.base_sha,
                "file_count": len(files.files),
                "listing": files.source,
            },
            "forbidden": {
                "policy": list(self.policy.always_forbidden),
                "step": [p for p in step.forbidden_paths if isinstance(p, str)][:100],
                "invalid": rejected_forbidden,
            },
            "caps": {"max_target_paths": self.policy.max_target_paths, "max_new_paths": self.policy.max_new_paths},
        }

        if step.kind not in WORKSPACE_WRITE_KINDS:
            evidence["read_only"] = True
            reason = f"step kind '{step.kind}' does not write workspace files: read-only scope"
            return "active", deny_all_contract(forbidden=forbidden, reason=reason), evidence, reason, None

        draft = _Draft()
        may_create = step.kind in CREATING_KINDS
        hint_records: list[HintEvidence] = []
        for hint in step.repo_hints:
            if not isinstance(hint, str):
                continue
            record = await self._resolve_hint(hint, workspace, files, may_create=may_create)
            hint_records.append(record)
            for t in record.targets:
                draft.target(t, f"hint:{record.kind}")
            for n in record.new_paths:
                draft.new_path(n, "hint:new_path")
        evidence["hints"] = [r.to_dict() for r in hint_records]

        invalid_new: list[dict[str, str]] = []
        for raw in step.allowed_new_paths:
            try:
                p = normalise_path(str(raw))
            except ValueError as exc:
                invalid_new.append({"path": str(raw)[:300], "reason": str(exc)})
                continue
            draft.new_path(p, "step.allowed_new_paths")
        evidence["invalid_allowed_new_paths"] = invalid_new

        acceptance = parse_acceptance(step.acceptance)
        self._acceptance_paths(acceptance, files, draft, may_create=may_create)
        constraint_tokens = constraint_delete_tokens(step.constraints)
        for token in constraint_tokens:
            for p in self._existing_for(token, files):
                draft.delete(p, "constraint:delete")
        evidence["constraint_delete_tokens"] = constraint_tokens

        # ---- policy merge + forbidden paths (15.3 / 15.5)
        targets, new_paths, delete_paths = self._apply_forbidden(draft, forbidden, files)
        evidence["targets"] = [{"path": p, "sources": draft.targets[p]} for p in targets]
        evidence["allowed_new_paths"] = [{"path": p, "sources": draft.new_paths[p]} for p in new_paths]
        evidence["delete_paths"] = [{"path": p, "sources": draft.delete_paths[p]} for p in delete_paths]
        evidence["excluded"] = draft.excluded
        evidence["caps"].update({"target_count": len(targets), "new_path_count": len(new_paths)})

        operations: list[Operation] = []
        if new_paths:
            operations.append("create")
        if targets:
            operations.append("modify")
        if delete_paths:
            operations.append("delete")
        evidence["operations"] = {
            "create": "allowed new paths present" if new_paths else None,
            "modify": "resolved target paths present" if targets else None,
            "delete": "acceptance absence evidence / explicit delete constraint" if delete_paths else None,
        }

        # ---- unavailable (15.8)
        unavailable: tuple[str, str] | None = None
        if len(targets) > self.policy.max_target_paths:
            unavailable = (
                "too_many_target_paths",
                f"{len(targets)} target paths exceed policies.scope.max_target_paths={self.policy.max_target_paths}",
            )
        elif len(new_paths) > self.policy.max_new_paths:
            unavailable = (
                "too_many_new_paths",
                f"{len(new_paths)} allowed new paths exceed policies.scope.max_new_paths={self.policy.max_new_paths}",
            )
        elif not targets and not new_paths:
            unavailable = (
                "no_resolvable_scope",
                "no repo hint resolved to an existing, permitted file and the step declares no allowed new paths",
            )
        if unavailable is not None:
            code, reason = unavailable
            evidence["unavailable_reason"] = {"code": code, "reason": reason}
            return "unavailable", deny_all_contract(forbidden=forbidden, reason=reason), evidence, reason, code

        reason = f"{len(targets)} target path(s), {len(new_paths)} allowed new path(s) for step {step.step_key}"
        contract = ScopeContract(
            source="planner_and_repo_intelligence",
            version=1,
            strict_target_paths=True,
            target_paths=[glob_escape(t) for t in targets],  # targets are existing files: always literal
            allowed_new_paths=new_paths,
            forbidden_paths=forbidden,
            allowed_operations=operations,
            reason=reason,
        )
        return "active", contract, evidence, reason, None

    def _acceptance_paths(self, acceptance: list[AcceptanceCriterion], files: WorkspaceFiles, draft: _Draft, *, may_create: bool) -> None:
        for crit in acceptance:
            if isinstance(crit, AbsenceEvidence) and crit.pattern is None:
                # "the path itself must not exist": existing matches must be deleted by this step
                for p in self._existing_for(crit.path_glob, files):
                    draft.delete(p, "acceptance:absence")
            elif isinstance(crit, PresenceEvidence) and may_create:
                try:
                    p = normalise_path(crit.path_glob)
                except ValueError:
                    continue
                if any(c in GLOB_CHARS for c in p) or files.exists(p):
                    continue
                ok, _why = files.creatable(p)
                if ok:
                    draft.new_path(p, "acceptance:presence")

    @staticmethod
    def _existing_for(pattern: str, files: WorkspaceFiles) -> list[str]:
        try:
            p = normalise_path(pattern)
        except ValueError:
            return []
        if any(c in GLOB_CHARS for c in p) or p.endswith("/"):
            return files.match(p)
        if files.exists(p):
            return [p]
        return []

    def _apply_forbidden(self, draft: _Draft, forbidden: list[str], files: WorkspaceFiles) -> tuple[list[str], list[str], list[str]]:
        new_paths: list[str] = []
        for p, sources in list(draft.new_paths.items()):
            concrete = not any(c in GLOB_CHARS for c in p) and not p.endswith("/")
            if concrete and any_match(p, forbidden):
                draft.exclude(p, "forbidden", bucket="allowed_new_paths")
            elif concrete and files.exists(p):
                # an explicitly declared, exact path that already exists can only be written as a modification
                for source in sources:
                    draft.target(p, f"{source}(existing)")
                draft.exclude(p, "already exists: promoted to target_paths", bucket="allowed_new_paths")
            else:
                new_paths.append(p)
        targets: list[str] = []
        for p in draft.targets:
            if any_match(p, forbidden):
                draft.exclude(p, "forbidden", bucket="target_paths")
            else:
                targets.append(p)
        deletes = [p for p in draft.delete_paths if p in targets]
        return targets, new_paths, deletes

    async def _resolve_hint(self, hint: str, workspace: WorkspaceHandle, files: WorkspaceFiles, *, may_create: bool) -> HintEvidence:
        hint = _strip_line_suffix(hint.strip(), files)
        kind = classify_hint(hint, files)
        record = HintEvidence(hint=hint, kind=kind)
        if kind == "invalid":
            record.status = "invalid"
            record.reason = "neither a repository-relative path/glob nor a symbol or search text"
            return record
        if kind in ("glob", "directory", "path"):
            p = normalise_path(hint)
            if kind == "glob":
                matches = files.match(p)
            elif kind == "directory":
                matches = files.under(p)
            else:
                matches = [p] if files.exists(p) else []
            if matches:
                record.status = "resolved"
                record.targets = matches
                record.reason = f"{len(matches)} existing file(s)"
                return record
            if kind == "path":
                return self._unmatched_path(record, p, files, may_create=may_create)
            if kind == "directory" and may_create:
                ok, why = files.creatable(p.rstrip("/"))
                if ok:
                    record.status = "new_path"
                    record.new_paths = [p.rstrip("/") + "/"]
                    record.reason = "directory does not exist yet; step kind may create files below it"
                    return record
                record.reason = f"directory does not exist and cannot be created: {why}"
                return record
            record.reason = "no existing file matches (files to create belong in allowed_new_paths)"
            return record
        return await self._resolve_by_repo(record, workspace, files)

    def _unmatched_path(self, record: HintEvidence, path: str, files: WorkspaceFiles, *, may_create: bool) -> HintEvidence:
        if "/" not in path:
            # bare file name: accept a unique basename match as the intended existing file
            same_name = sorted(f for f in files.files if PurePosixPath(f).name == path)
            if len(same_name) == 1:
                record.status = "resolved"
                record.targets = same_name
                record.reason = "unique basename match"
                return record
            if len(same_name) > 1:
                record.status = "ambiguous"
                record.hits = [{"path": f, "note": "same basename"} for f in same_name[:_EVIDENCE_HITS]]
                record.reason = f"{len(same_name)} files share this name"
                return record
        if may_create:
            ok, why = files.creatable(path)
            if ok:
                record.status = "new_path"
                record.new_paths = [path]
                record.reason = "does not exist yet; step kind may create files"
                return record
            record.reason = f"does not exist and cannot be created: {why}"
            return record
        record.reason = "does not exist"
        return record

    async def _resolve_by_repo(self, record: HintEvidence, workspace: WorkspaceHandle, files: WorkspaceFiles) -> HintEvidence:
        s = self.settings
        query = record.hint
        try:
            if record.kind == "symbol":
                name = query.removesuffix("()")
                hits = await self.repo.find_symbol(workspace, name, k=s.search_k)
                threshold = s.min_symbol_score
                if not hits:
                    last = _SYMBOL_SPLIT_RE.split(name)[-1]
                    if last != name:
                        hits = await self.repo.find_symbol(workspace, last, k=s.search_k)
                if not hits:
                    hits = await self.repo.search(workspace, name, k=s.search_k)
                    threshold = s.min_search_score
            else:
                hits = await self.repo.search(workspace, query, k=s.search_k)
                threshold = s.min_search_score
        except Exception as exc:  # provider failure is recorded evidence, never a crash of scope generation
            record.status = "error"
            record.reason = f"repository intelligence failed: {type(exc).__name__}: {str(exc)[:200]}"
            return record
        selected = select_confident_hits(
            hits, files, min_score=threshold, relative_floor=s.relative_score_floor, max_files=s.max_files_per_hint
        )
        record.hits = selected.records
        if selected.ambiguous:
            record.status = "ambiguous"
            record.reason = f"more than {s.max_files_per_hint} high-confidence files (threshold {threshold})"
        elif selected.accepted:
            record.status = "resolved"
            record.targets = selected.accepted
            record.reason = f"{len(selected.accepted)} high-confidence file(s) (threshold {threshold})"
        else:
            record.reason = f"no hit reached the confidence threshold {threshold}"
        return record


@dataclass(frozen=True)
class _StepSnapshot:
    """Detached copy of the step fields generation needs (no DB session held during repository I/O)."""

    id: uuid.UUID
    job_id: uuid.UUID
    step_key: str
    kind: str
    status: str
    superseded: bool
    repo_hints: list[Any]
    allowed_new_paths: list[Any]
    forbidden_paths: list[Any]
    acceptance: list[Any]
    constraints: list[Any]

    @classmethod
    def of(cls, step: Step) -> _StepSnapshot:
        return cls(
            id=step.id,
            job_id=step.job_id,
            step_key=step.step_key,
            kind=str(step.kind),
            status=str(step.status),
            superseded=bool(step.superseded),
            repo_hints=list(step.repo_hints or []),
            allowed_new_paths=list(step.allowed_new_paths or []),
            forbidden_paths=list(step.forbidden_paths or []),
            acceptance=list(step.acceptance or []),
            constraints=list(step.constraints or []),
        )
