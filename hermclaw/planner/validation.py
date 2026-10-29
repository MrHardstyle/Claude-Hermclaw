"""Semantic plan validation on top of the PlanContract schema (P14 14.5/14.6, Bauplan §15).

``PlanContract`` already rejects duplicate ids, unknown/self dependencies and cycles. This module adds the rules
that need runtime knowledge: configured/available capabilities, step-kind -> capability consistency, step budget,
duplicate work, unconsumed (unreachable) information steps, dependency requirements of checking steps,
repository-hint grounding (no invented paths), path hygiene and acceptance sanity. Every rule is generic; the
error texts are sent back to the planner model verbatim in a repair turn.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

from hermclaw.contracts.acceptance import (
    AbsenceEvidence,
    AcceptanceCriterion,
    CommandEvidence,
    DiffEvidence,
    PresenceEvidence,
    SchemaEvidence,
    TestEvidence,
)
from hermclaw.contracts.common import MUTATING_STEP_KINDS, StepKind
from hermclaw.contracts.plan import PlanContract, PlanStep, ResearchRequest
from hermclaw.contracts.scope import normalise_path
from hermclaw.core.config import HermclawConfig
from hermclaw.planner.inputs import (
    GLOB_CHARS,
    PlannerInput,
    PlannerSettings,
    TestFramework,
    collect_known_paths,
    collect_known_symbols,
    detect_test_command,
    has_file_shape,
)
from hermclaw.research.questions import unanswerable_reason
from hermclaw.scope.guard import acceptance_glob_matches, any_match, path_matches

MUTATING_KINDS = frozenset(k.value for k in MUTATING_STEP_KINDS)
INFO_KINDS = frozenset({StepKind.research.value, StepKind.discover.value, StepKind.inventory.value})
RUNTIME_ONLY_KINDS = frozenset({StepKind.plan.value, StepKind.replan.value})
REMOTE_MUTATING_KINDS = frozenset({StepKind.ssh.value, StepKind.deploy.value})
WORKSPACE_MUTATING_KINDS = MUTATING_KINDS - REMOTE_MUTATING_KINDS
CHECKING_KINDS = frozenset({StepKind.review.value, StepKind.verify.value})
SUBSTANTIVE_EVIDENCE = frozenset({"presence", "absence", "command", "test", "diff", "schema", "artifact"})
CONTRACT_MAX_STEPS = 60

_SYMBOL_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::|#|->|\\)[A-Za-z_$][\w$]*)*(?:\(\))?$")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")

HintKind = Literal["path", "symbol", "invalid"]


def normalise_text(text: str) -> str:
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", text.lower())).strip()


# function words of the languages jobs are written in; they say nothing about what a research question is about
_FILLER_TEXT = (
    "the and for with what which how are does there this that from into about can should all any its was "
    "research answer sources reason recherchiere recherche sind ist wie welche welcher welches gibt die "
    "der das den dem des und oder für mit von zur zum aus auf bei ein eine einer eines nicht auch "
    "aktuelle aktuellen"
)
_FILLER_WORDS = frozenset(_FILLER_TEXT.split())
RESEARCH_COVERAGE = 0.6
_MIN_COVERAGE_TOKENS = 4


def content_tokens(text: str) -> set[str]:
    return {t for t in normalise_text(text).split() if len(t) >= 3 and not t.isdigit() and t not in _FILLER_WORDS}


def new_files_per_step(max_turns: int) -> int:
    """Two of every five coder turns may create a file; the rest run tests and fix (20 turns -> 8 files)."""
    return max(3, max_turns * 2 // 5)


def step_turn_budget(step: PlanStep, max_turns: int, max_turns_per_step: int) -> int:
    """The coder turns of one attempt: ``max_turns``, and for a step that creates or names more files than
    :func:`new_files_per_step` five turns for every two files beyond (11 files at 20 turns -> 28), up to
    ``max_turns_per_step``."""
    files = {p for p in (*step.allowed_new_paths, *step.repo_hints) if not any(c in GLOB_CHARS for c in p) and not p.endswith("/")}
    extra = math.ceil(max(0, len(files) - new_files_per_step(max_turns)) * 5 / 2)
    return max(max_turns, min(max_turns_per_step, max_turns + extra))


@dataclass(frozen=True)
class ValidationContext:
    """Everything the semantic validator and the deterministic enrichment need (built once per planner run)."""

    config_capabilities: tuple[str, ...]
    available_capabilities: tuple[str, ...]
    kind_capability: dict[str, str]
    known_paths: tuple[str, ...] = ()
    known_symbols: frozenset[str] = frozenset()
    context_text: str = ""
    always_forbidden: tuple[str, ...] = ()
    forbidden_command_patterns: tuple[str, ...] = ()
    max_steps: int = 30
    many_paths_threshold: int = 6
    # new files one coder step can create within its turn budget (one write per file plus test runs and fixes)
    max_new_files_per_step: int = 8
    test_command: str | None = None
    test_framework: TestFramework = "generic"
    has_existing_tests: bool = False
    capability_network: dict[str, bool] = field(default_factory=dict)
    job_text: str = ""
    _known_set: frozenset[str] = frozenset()
    _known_dirs: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls, config: HermclawConfig, inputs: PlannerInput, settings: PlannerSettings, *, extra_text: str = "", job_text: str = ""
    ) -> ValidationContext:
        """``extra_text`` (job goal, constraints, failure evidence) also counts as context symbols may come from;
        ``job_text`` is what the user asked for (title, prompt, constraints)."""
        caps_cfg = config.capabilities
        config_caps = tuple(c.name for c in caps_cfg.capabilities)
        if inputs.capabilities is None:
            available = config_caps
        else:
            wanted = set(inputs.capabilities)
            available = tuple(c for c in config_caps if c in wanted)
        kind_capability: dict[str, str] = {}
        for kind in StepKind:
            if kind.value in RUNTIME_ONLY_KINDS:
                continue
            cap = caps_cfg.step_kind_capability.get(kind.value)
            if cap and cap in available:
                kind_capability[kind.value] = cap
        known = collect_known_paths(inputs)
        test_command, framework = detect_test_command(inputs)
        parts = [
            *(s.snippet for s in inputs.retrieved_context),
            extra_text,
            json.dumps(inputs.repository_inventory, ensure_ascii=False, default=str) if inputs.repository_inventory else "",
            json.dumps(inputs.research_summary, ensure_ascii=False, default=str) if inputs.research_summary else "",
            *inputs.existing_tests,
        ]
        context_text = "\n".join(p for p in parts if p.strip())
        return cls(
            config_capabilities=config_caps,
            available_capabilities=available,
            kind_capability=kind_capability,
            known_paths=tuple(known),
            known_symbols=frozenset(collect_known_symbols(inputs)),
            context_text=context_text,
            always_forbidden=tuple(config.policies.scope.always_forbidden),
            forbidden_command_patterns=tuple(config.policies.commands.forbidden_patterns),
            max_steps=min(settings.max_steps, CONTRACT_MAX_STEPS),
            many_paths_threshold=settings.many_paths_threshold,
            max_new_files_per_step=new_files_per_step(config.policies.coder.max_turns),
            test_command=test_command,
            test_framework=framework,
            has_existing_tests=bool(inputs.existing_tests),
            capability_network={c.name: c.network for c in caps_cfg.capabilities},
            job_text=job_text,
            _known_set=frozenset(known),
            _known_dirs=frozenset(_parent_dirs(known)),
        )

    @property
    def research_capability(self) -> str | None:
        return self.kind_capability.get(StepKind.research.value)

    @property
    def network_capabilities(self) -> tuple[str, ...]:
        return tuple(c for c in self.available_capabilities if self.capability_network.get(c, False))

    # ------------------------------------------------------------------ hint helpers
    def classify_hint(self, hint: str) -> HintKind:
        h = hint.strip()
        if not h:
            return "invalid"
        if "/" in h or "\\" in h or any(c in GLOB_CHARS for c in h) or h in self._known_set or h in self._known_dirs:
            return "path"
        if has_file_shape(h):
            return "path"
        if _SYMBOL_RE.match(h):
            return "symbol"
        return "invalid"

    def path_exists(self, path: str) -> bool:
        """True when ``path`` (file, directory or glob) matches at least one known repository path."""
        if any(c in GLOB_CHARS for c in path):
            return any(path_matches(k, path) for k in self.known_paths)
        if path.endswith("/"):
            return path.rstrip("/") in self._known_dirs
        return path in self._known_set or path in self._known_dirs

    def is_known_dir(self, path: str) -> bool:
        return path.rstrip("/") in self._known_dirs

    def matching_known_paths(self, pattern: str, *, limit: int) -> set[str]:
        """Known repository paths matched by a glob or directory pattern (at most ``limit``)."""
        out: set[str] = set()
        for known in self.known_paths:
            if path_matches(known, pattern):
                out.add(known)
                if len(out) >= limit:
                    break
        return out

    def symbol_known(self, symbol: str) -> bool:
        """A symbol hint is grounded when it is a known symbol or occurs as a whole word in the provided context."""
        if not self.known_symbols and not self.context_text.strip():
            return True  # nothing to verify against -> symbols are accepted
        base = symbol.removesuffix("()")
        last = re.split(r"\.|::|#|->|\\", base)[-1]
        if base in self.known_symbols or last in self.known_symbols:
            return True
        return any(_word_in(name, self.context_text) for name in {base, last} if name)


def _word_in(word: str, text: str) -> bool:
    return re.search(r"(?<![\w$])" + re.escape(word) + r"(?![\w$])", text) is not None


def _parent_dirs(paths: Iterable[str]) -> set[str]:
    dirs: set[str] = set()
    for p in paths:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return dirs


def path_like_hints(step: PlanStep, ctx: ValidationContext) -> list[str]:
    """Normalised path/glob hints of a step (directories get a trailing slash so they work as globs)."""
    out: list[str] = []
    for hint in step.repo_hints:
        if ctx.classify_hint(hint) != "path":
            continue
        try:
            p = normalise_path(hint)
        except ValueError:
            continue
        if not any(c in GLOB_CHARS for c in p) and not p.endswith("/") and ctx.is_known_dir(p) and p not in ctx.known_paths:
            p += "/"
        if p not in out:
            out.append(p)
    return out


def is_catch_all(pattern: str) -> bool:
    """A pattern made only of wildcards and slashes (``*``, ``**``, ``**/*``) names no concrete location."""
    p = pattern.strip()
    return bool(p) and all(c in "*?/" for c in p)


def has_substantive_acceptance(acceptance: Sequence[AcceptanceCriterion]) -> bool:
    return any(a.type in SUBSTANTIVE_EVIDENCE for a in acceptance)


# --------------------------------------------------------------------------------------------- rules
def _capability_errors(step: PlanStep, ctx: ValidationContext) -> list[str]:
    errors: list[str] = []
    kind = step.kind.value
    if kind in RUNTIME_ONLY_KINDS:
        errors.append(f"step {step.id}: kind '{kind}' is reserved for the runtime and must not appear in a plan")
        return errors
    if step.capability not in ctx.config_capabilities:
        errors.append(
            f"step {step.id}: unknown capability '{step.capability}'; allowed capabilities: {', '.join(ctx.available_capabilities)}"
        )
    elif step.capability not in ctx.available_capabilities:
        errors.append(
            f"step {step.id}: capability '{step.capability}' is not available for this job; "
            f"available: {', '.join(ctx.available_capabilities)}"
        )
    required = ctx.kind_capability.get(kind)
    if required is None:
        errors.append(f"step {step.id}: step kind '{kind}' is not available for this job; allowed kinds: {', '.join(ctx.kind_capability)}")
    elif step.capability != required and step.capability in ctx.config_capabilities:
        errors.append(f"step {step.id}: kind '{kind}' requires capability '{required}', got '{step.capability}'")
    return errors


def _hint_errors(step: PlanStep, ctx: ValidationContext, plan: PlanContract | None = None) -> list[str]:
    errors: list[str] = []
    creators = {s.id: s for s in plan.steps if s.id != step.id} if plan is not None else {}
    before = _reachable(plan, step.id) if plan is not None else set()
    for hint in step.repo_hints:
        kind = ctx.classify_hint(hint)
        if kind == "invalid":
            errors.append(f"step {step.id}: repo_hint '{hint[:120]}' is neither a repository-relative path/glob nor a symbol name")
            continue
        if kind == "symbol":
            if not ctx.symbol_known(hint):
                errors.append(f"step {step.id}: repo_hint symbol '{hint[:120]}' does not appear in the provided repository context")
            continue
        try:
            p = normalise_path(hint)
        except ValueError:
            errors.append(
                f"step {step.id}: repo_hint '{hint[:120]}' must be repository-relative without '..' "
                "(absolute host paths belong in goal or constraints)"
            )
            continue
        if is_catch_all(p):
            errors.append(f"step {step.id}: repo_hint '{p}' matches everything; name the concrete files, directories or globs")
            continue
        if not any(c in GLOB_CHARS for c in p) and any_match(p, list(ctx.always_forbidden)):
            errors.append(f"step {step.id}: repo_hint '{p}' is a forbidden path (runtime policy)")
            continue
        made_by = [sid for sid, s in creators.items() if not any(c in GLOB_CHARS for c in p) and any_match(p, list(s.allowed_new_paths))]
        if ctx.known_paths and not ctx.path_exists(p) and made_by:
            # a file another step of this plan creates: the step reads what that step delivered, once it has run
            # (live G1: three planner answers named collector.py/tests in a later step's hints, BUG-262)
            if not any(sid in before for sid in made_by):
                errors.append(
                    f"step {step.id}: repo_hint '{p}' is created by step {made_by[0]}: add {made_by[0]} to depends_on "
                    "(the file exists only after that step has run)"
                )
            continue
        if ctx.known_paths and not ctx.path_exists(p):
            errors.append(
                f"step {step.id}: repo_hint '{p}' does not exist in repository_inventory/retrieved_context "
                "(never invent paths; files to be created belong in allowed_new_paths)"
            )
    for field_name, values in (("allowed_new_paths", step.allowed_new_paths), ("forbidden_paths", step.forbidden_paths)):
        for raw in values:
            try:
                p = normalise_path(raw)
            except ValueError:
                errors.append(f"step {step.id}: {field_name} entry '{raw[:120]}' must be repository-relative without '..'")
                continue
            if field_name == "allowed_new_paths" and is_catch_all(p):
                errors.append(
                    f"step {step.id}: allowed_new_paths entry '{p}' matches everything; scope stays explicit - name the "
                    "directory or file pattern where new files are created"
                )
                continue
            if field_name == "allowed_new_paths" and any_match(p, list(ctx.always_forbidden)):
                errors.append(f"step {step.id}: allowed_new_paths entry '{p}' is a forbidden path (runtime policy)")
    return errors


def _scope_source_errors(step: PlanStep, ctx: ValidationContext) -> list[str]:
    """A workspace-mutating step must name the files it works on: the runtime builds its scope only from
    repo_hints, allowed_new_paths and concrete presence evidence (Bauplan §15, scope stays explicit). Without any
    of them the step can never run (SCOPE_UNAVAILABLE), so the plan is rejected while the planner can still fix it."""
    if step.kind.value not in WORKSPACE_MUTATING_KINDS or step.repo_hints or step.allowed_new_paths:
        return []
    for crit in step.acceptance:
        # the scope engine turns presence evidence for a concrete file that does not exist yet into a new path
        if isinstance(crit, PresenceEvidence) and not any(ch in GLOB_CHARS for ch in crit.path_glob):
            try:
                if not ctx.path_exists(normalise_path(crit.path_glob)):
                    return []
            except ValueError:
                pass
    return [
        f"step {step.id}: {step.kind.value} step names no files to work on; list the existing files it changes in "
        "repo_hints and/or the files or directories it creates in allowed_new_paths (repository-relative, e.g. "
        "'app/main.py' or 'tests/'). The runtime builds the step's scope only from these; in an empty or new "
        "repository every file is created and belongs in allowed_new_paths"
    ]


def _checked_candidates(plan: PlanContract, step: PlanStep) -> str:
    """Repair hint for a checking step without dependencies: the mutating steps listed before it."""
    before: list[str] = []
    for other in plan.steps:
        if other.id == step.id:
            break
        if other.kind.value in MUTATING_KINDS:
            before.append(other.id)
    if not before:
        return "; add the ids of those steps to its depends_on"
    return f'; add the ids of those steps to its depends_on, e.g. "depends_on": {json.dumps(before[-3:])}'


def _acceptance_errors(step: PlanStep, ctx: ValidationContext) -> list[str]:
    errors: list[str] = []
    kind = step.kind.value
    if kind in REMOTE_MUTATING_KINDS and not has_substantive_acceptance(step.acceptance):
        errors.append(
            f"step {step.id}: mutating step of kind '{kind}' needs machine-checkable acceptance "
            "(e.g. command evidence with a read-only check, or artifact evidence)"
        )
    network_allowed = ctx.capability_network.get(step.capability, False)
    if step.network and not network_allowed and step.capability in ctx.config_capabilities:
        errors.append(
            f"step {step.id}: network access is not allowed for capability '{step.capability}' (network is off by "
            f"default; only capabilities with network=true may use it: {', '.join(ctx.network_capabilities) or 'none'})"
        )
    for idx, crit in enumerate(step.acceptance):
        where = f"step {step.id}: acceptance[{idx}] ({crit.type})"
        if isinstance(crit, CommandEvidence) and crit.network and not network_allowed:
            errors.append(f"{where}: requests network access, which capability '{step.capability}' does not allow")
        if isinstance(crit, DiffEvidence):
            for field_name, globs in (("must_change", crit.must_change), ("must_not_change", crit.must_not_change)):
                for glob in globs:
                    try:
                        normalise_path(glob)
                    except ValueError:
                        errors.append(f"{where}: {field_name} entry '{glob[:120]}' must be repository-relative without '..'")
            continue
        if isinstance(crit, PresenceEvidence | AbsenceEvidence):
            try:
                normalise_path(crit.path_glob)
            except ValueError:
                errors.append(f"{where}: path_glob must be repository-relative without '..'")
            if crit.pattern is not None:
                try:
                    re.compile(crit.pattern)
                except re.error as exc:
                    errors.append(f"{where}: pattern is not a valid regular expression ({exc})")
        elif isinstance(crit, SchemaEvidence):
            try:
                normalise_path(crit.path)
            except ValueError:
                errors.append(f"{where}: path must be repository-relative without '..'")
        elif isinstance(crit, CommandEvidence | TestEvidence):
            for pattern in ctx.forbidden_command_patterns:
                try:
                    hit = re.search(pattern, crit.command)
                except re.error:  # pragma: no cover - policy patterns are validated by ops tests
                    continue
                if hit:
                    errors.append(f"{where}: command '{crit.command[:120]}' is forbidden by the runtime command policy")
                    break
            if isinstance(crit, CommandEvidence) and crit.stdout_pattern is not None:
                try:
                    re.compile(crit.stdout_pattern)
                except re.error as exc:
                    errors.append(f"{where}: stdout_pattern is not a valid regular expression ({exc})")
    return errors


def _research_question_errors(plan: PlanContract, new_steps: Sequence[PlanStep]) -> list[str]:
    """Research searches the web: questions about the repository itself or about which technology to use are
    unanswerable there and steered every live job into unrelated frameworks (analysis 2026-10-16, U1)."""
    errors: list[str] = []
    for idx, req in enumerate(plan.research_needed):
        why = unanswerable_reason(req.question)
        if why:
            errors.append(f"research_needed[{idx}] '{req.question[:120]}' cannot be researched on the web – {why}; remove it")
    for step in new_steps:
        if step.kind.value != StepKind.research.value:
            continue
        why = unanswerable_reason(f"{step.title}. {step.goal}")
        if why:
            errors.append(f"step {step.id}: research step '{step.title[:120]}' cannot be answered by web research – {why}; remove the step")
    return errors


TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs)(?:/|$)|(?:^|/)test_[^/]*$|_test\.[a-z]+$|\.(?:test|spec)\.[a-z]+$|(?:^|/)conftest\.py$|\.tests\.ps1$",
    re.IGNORECASE,
)


#: documentation a step reads or touches alongside its real work; it does not make a test step a code step (BUG-209)
DOC_PATH_RE = re.compile(r"\.(?:md|markdown|rst|txt|adoc)$|(?:^|/)(?:README|CHANGELOG|LICENSE|NOTICE|AUTHORS)[^/]*$", re.IGNORECASE)


def _step_paths(step: PlanStep, ctx: ValidationContext) -> list[str]:
    paths = [*path_like_hints(step, ctx)]
    for raw in step.allowed_new_paths:
        try:
            paths.append(normalise_path(raw))
        except ValueError:
            continue
    return paths


#: kinds whose handler only runs checks: nothing in them ever writes a file
CHECK_ONLY_KINDS = frozenset({StepKind.test.value, StepKind.verify.value, StepKind.review.value})


def _check_only_creation_errors(new_steps: Sequence[PlanStep]) -> list[str]:
    """A ``test``/``verify``/``review`` step only runs the checks of its acceptance; files listed in its
    ``allowed_new_paths`` are never written, and the criteria that need them fail every attempt (rc22 battery X1: the
    unit tests were planned into a ``test`` step, the implementing step's review demanded them, BUG-209)."""
    errors: list[str] = []
    for step in new_steps:
        if step.kind.value in CHECK_ONLY_KINDS and step.allowed_new_paths:
            paths = ", ".join(step.allowed_new_paths[:5])
            errors.append(
                f"step {step.id}: kind '{step.kind.value}' only runs checks and never writes files, so {paths} would "
                "never be created; files a step writes (tests included) belong in a workspace-mutating step (kind "
                "implement) - put them into the step that writes the code they test, and keep this step without "
                "allowed_new_paths or remove it"
            )
    return errors


_TEST_STEM_RE = re.compile(r"^(?:test_(?P<a>.+)|(?P<b>.+?)_test|(?P<c>.+?)\.(?:test|spec))$", re.IGNORECASE)


def _tested_stem(path: str) -> str | None:
    """``collector`` for tests/test_collector.py, src/app.test.js or pkg/app_test.go; None for fixtures and helpers."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    m = _TEST_STEM_RE.match(name.rsplit(".", 1)[0] if "." in name else name)
    stem = m and (m.group("a") or m.group("b") or m.group("c"))
    return stem.lower() if stem else None


def _code_stem(path: str) -> str:
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return (name.rsplit(".", 1)[0] if "." in name else name).lower()


def _split_test_errors(plan: PlanContract, new_steps: Sequence[PlanStep], ctx: ValidationContext, preserved: frozenset[str]) -> list[str]:
    """Code and the tests for it belong in one step: an implementing step cannot run tests a later step writes, and
    the test step may not fix the code (analysis 2026-10-16, U3). A step that only writes tests may therefore not
    depend on a workspace-mutating step that writes non-test code – nor sit beside it without a dependency when its
    test files name that step's code (tests/test_collector.py for collector.py): the planner dropped the dependency
    and the tests were written apart from the code, shaped to whatever it returned (live G1, BUG-237)."""
    errors: list[str] = []
    for step in new_steps:
        if step.kind.value not in WORKSPACE_MUTATING_KINDS:
            continue
        paths = _step_paths(step, ctx)
        tests = [p for p in paths if TEST_PATH_RE.search(p)]
        if not tests:
            continue
        stems = {t for t in map(_tested_stem, tests) if t}
        own_code = [p for p in paths if not TEST_PATH_RE.search(p) and not DOC_PATH_RE.search(p)]
        if own_code:
            # a step with code of its own may still carry tests named after ANOTHER step's code: the frontend step
            # wrote tests/test_collector.py without depending on the collector step (live G1 on rc39, BUG-250)
            foreign = stems - {_code_stem(p) for p in own_code}
            for other in plan.steps:
                if other.id == step.id or other.id in preserved or other.kind.value not in WORKSPACE_MUTATING_KINDS:
                    continue
                named = sorted(foreign & {_code_stem(p) for p in _step_paths(other, ctx) if not TEST_PATH_RE.search(p)})
                if named:
                    moved = [t for t in tests if _tested_stem(t) in named]
                    errors.append(
                        f"step {step.id} writes tests for code of step {other.id} ({', '.join(moved)}): move these test "
                        f"paths to step {other.id} (its allowed_new_paths/repo_hints and test acceptance), which writes "
                        "that code and can run them"
                    )
            continue
        # a README among the hints of a tests-only step is context, not code (rc22 battery X1)
        for other in plan.steps:
            if other.id == step.id or other.id in preserved or other.kind.value not in WORKSPACE_MUTATING_KINDS:
                continue  # tests for code a completed step already delivered are a step of their own
            code = [p for p in _step_paths(other, ctx) if not TEST_PATH_RE.search(p) and not DOC_PATH_RE.search(p)]
            if not code:
                continue
            if other.id in step.depends_on or stems & {_code_stem(p) for p in code}:
                errors.append(
                    f"step {step.id} only writes tests for the code of step {other.id}: put code and its tests into ONE "
                    f"step (merge {step.id} into {other.id}: add its test paths to {other.id}'s allowed_new_paths/"
                    "repo_hints and its test acceptance), so the step can run its own tests and fix both. Merge only "
                    "these tests: other components stay in their own steps"
                )
    return errors


def _named_in(path: str, text: str) -> bool:
    """Whether ``text`` (lower case) names the file: its path, its name, or a specific stem (``users`` for
    app/routers/users.py, ``users`` for tests/test_users.py) as a word – not a generic one like ``config``."""
    name = path.rsplit("/", 1)[-1].lower()
    if path.lower() in text or name in text:
        return True
    stems = {_code_stem(path), _tested_stem(path) or ""} - _GENERIC_STEMS - {""}
    return any(len(st) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(st)}(?![a-z0-9])", text) for st in stems)


def _drop_unnamed_existing_must_change(
    plan: PlanContract, ctx: ValidationContext, preserved: frozenset[str]
) -> tuple[PlanContract, list[str]]:
    """``must_change`` on an existing file neither the step's title and goal nor the job name: the worker sees its goal, so it
    has no reason to touch the file, and a correct solution failed the diff check (cycle 1 on rc59 M2: input data and
    the entry point, BUG-278; R-033 blind judging: the most frequent release violation of every planner model). The
    file stays in the scope – the step may change it – but a change is no longer demanded. New files and directory
    globs are untouched: creating them is what the step is for."""
    notes: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        if step.id in preserved or step.kind.value not in WORKSPACE_MUTATING_KINDS:
            steps.append(step)
            continue
        text = f"{step.title}\n{step.goal}\n{ctx.job_text}".lower()
        dropped: list[str] = []
        acceptance: list[AcceptanceCriterion] = []
        for crit in step.acceptance:
            if not isinstance(crit, DiffEvidence):
                acceptance.append(crit)
                continue
            keep = []
            for glob in crit.must_change:
                try:
                    path = normalise_path(glob)
                except ValueError:
                    keep.append(glob)
                    continue
                concrete = not any(c in GLOB_CHARS for c in path) and not path.endswith("/")
                if concrete and ctx.path_exists(path) and not _named_in(path, text):
                    dropped.append(glob)
                else:
                    keep.append(glob)
            if len(keep) == len(crit.must_change):
                acceptance.append(crit)
            elif keep or crit.must_not_change or crit.max_changed_files is not None:
                acceptance.append(crit.model_copy(update={"must_change": keep}))
        if dropped:
            steps.append(step.model_copy(update={"acceptance": acceptance}))
            notes.append(
                f"step {step.id}: {', '.join(dropped[:5])} may change but is not required to "
                "(named neither in the step goal nor in the job)"
            )
        else:
            steps.append(step)
    if not notes:
        return plan, []
    return plan.model_copy(update={"steps": steps}), notes


def _move_foreign_tests(plan: PlanContract, ctx: ValidationContext, preserved: frozenset[str]) -> tuple[PlanContract, list[str]]:
    """Move test paths named after another step's code into that step, where code and tests are written together.

    A step that writes code of its own and also tests for the code of ANOTHER step (``_split_test_errors``, BUG-250)
    was rejected with the instruction to move them; three of 18 planner bench answers (R-033) repeated the plan in
    every repair turn until the planner gave up. The move is mechanical: the paths keep their field (new paths stay
    new, hints stay hints), ``must_change`` on them goes with them. A test stem named by code of several steps stays
    an error for the planner to decide."""
    by_id = {s.id: s for s in plan.steps}
    mutating = [s for s in plan.steps if s.id not in preserved and s.kind.value in WORKSPACE_MUTATING_KINDS]
    code_stems: dict[str, set[str]] = {
        s.id: {_code_stem(p) for p in _step_paths(s, ctx) if not TEST_PATH_RE.search(p) and not DOC_PATH_RE.search(p)} for s in mutating
    }
    moves: dict[str, dict[str, set[str]]] = {}  # source step -> target step -> normalised test paths
    for step in mutating:
        paths = _step_paths(step, ctx)
        own = code_stems[step.id]
        if not own:
            continue  # a tests-only step is merged by the planner (the repair message says which step to merge into)
        for test in (p for p in paths if TEST_PATH_RE.search(p)):
            stem = _tested_stem(test)
            if not stem or stem in own:
                continue
            owners = [o.id for o in mutating if o.id != step.id and stem in code_stems[o.id]]
            if len(owners) == 1:
                moves.setdefault(step.id, {}).setdefault(owners[0], set()).add(test)
    if not moves:
        return plan, []

    def norm(raw: str) -> str | None:
        try:
            return normalise_path(raw)
        except ValueError:
            return None

    added: dict[str, dict[str, list[str]]] = {}  # target -> field -> raw entries
    notes: list[str] = []
    updated: dict[str, PlanStep] = {}
    for src_id, targets in moves.items():
        src = by_id[src_id]
        moving = set().union(*targets.values())
        new_paths = [r for r in src.allowed_new_paths if norm(r) not in moving]
        hints = [h for h in src.repo_hints if norm(h) not in moving]
        acceptance: list[AcceptanceCriterion] = []
        carried: dict[str, list[str]] = {}
        for crit in src.acceptance:
            if not isinstance(crit, DiffEvidence) or not any(norm(g) in moving for g in crit.must_change):
                acceptance.append(crit)
                continue
            for g in crit.must_change:
                if norm(g) in moving:
                    tgt = next(t for t, ps in targets.items() if norm(g) in ps)
                    carried.setdefault(tgt, []).append(g)
            keep = [g for g in crit.must_change if norm(g) not in moving]
            if keep or crit.must_not_change or crit.max_changed_files is not None:
                acceptance.append(crit.model_copy(update={"must_change": keep}))
        updated[src_id] = src.model_copy(update={"allowed_new_paths": new_paths, "repo_hints": hints, "acceptance": acceptance})
        for tgt, ps in targets.items():
            slot = added.setdefault(tgt, {"allowed_new_paths": [], "repo_hints": [], "must_change": []})
            slot["allowed_new_paths"] += [r for r in src.allowed_new_paths if norm(r) in ps]
            slot["repo_hints"] += [h for h in src.repo_hints if norm(h) in ps]
            slot["must_change"] += carried.get(tgt, [])
            notes.append(f"step {src_id}: moved tests {', '.join(sorted(ps)[:5])} to step {tgt}, which writes the code they test")
    for tgt, slot in added.items():
        base = updated.get(tgt, by_id[tgt])
        new_paths = [*base.allowed_new_paths, *(r for r in slot["allowed_new_paths"] if r not in base.allowed_new_paths)]
        hints = [*base.repo_hints, *(h for h in slot["repo_hints"] if h not in base.repo_hints)]
        acceptance = list(base.acceptance)
        if slot["must_change"]:
            acceptance.append(DiffEvidence(must_change=slot["must_change"]))
        updated[tgt] = base.model_copy(update={"allowed_new_paths": new_paths, "repo_hints": hints, "acceptance": acceptance})
    return plan.model_copy(update={"steps": [updated.get(s.id, s) for s in plan.steps]}), notes


#: file stems too generic to name a component in a goal ("config.toml" vs. "die Konfiguration")
_GENERIC_STEMS = frozenset({"config", "index", "utils", "helpers", "common", "settings", "setup", "types", "constants", "__init__"})


_JOB_PATH = re.compile(r"(?<![\w/.@:-])((?:[\w.-]+/)*[A-Za-z_][\w-]*\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/-])")
_JOB_TICKED = re.compile(r"`([^`\n]{2,60})`")
_JOB_ERROR_STATUS = re.compile(r"(?<![\w.])([45]\d\d)(?!\w|\.\d)")
_HTTP_JOB = re.compile(r"\b(?:api|https?|rest|endpoints?|endpunkte?|status ?codes?)\b", re.IGNORECASE)
_DOMAIN_SUFFIXES = frozenset({"com", "de", "org", "net", "io", "ai", "dev"})


def _is_job_path(token: str) -> bool:
    if "/" in token:
        return True
    stem, _, ext = token.rpartition(".")
    if len(stem) < 2 or ext.lower() in _DOMAIN_SUFFIXES:
        return False
    return not (ext.lower() == "js" and stem[0].isupper())  # Node.js, Vue.js: products, not files


def job_literals(job_text: str) -> list[str]:
    """Language-neutral requirements of the job: file paths, names in backticks and – in an HTTP job – the error
    status codes it demands. Unlike prose they survive the translation into an English plan word for word."""
    found = [m.group(1).rstrip(".") for m in _JOB_PATH.finditer(job_text) if _is_job_path(m.group(1).rstrip("."))]
    found += [m.group(1).strip() for m in _JOB_TICKED.finditer(job_text)]
    if _HTTP_JOB.search(job_text):
        found += [m.group(1) for m in _JOB_ERROR_STATUS.finditer(job_text)]
    return list(dict.fromkeys(f for f in found if f))


def _strings(value: Any) -> list[str]:
    """Every string in a JSON-like value, as written. The JSON text of the plan escaped quotes and backslashes, so a
    literal such as `{"error": "..."}` could never be found and the plan was rejected in every repair turn (BUG-296)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _strings(v)]
    if isinstance(value, list):
        return [t for v in value for t in _strings(v)]
    return []


def _job_literal_errors(plan: PlanContract, ctx: ValidationContext) -> list[str]:
    """Every literal of the job (``job_literals``) appears in some step: goal, paths, hints or acceptance.

    First plans of J2 and X3 left out the demanded status 400 and the named database file; all four such plans needed
    replans, while eight of nine plans naming every literal ran without one. A literal no step names is a
    requirement no coder is asked to deliver (traceability, plan release criteria)."""
    missing = _missing_job_literals(plan, ctx)
    if not missing:
        return []
    names = ", ".join(repr(m) for m in missing)
    return [
        f"the job names {names}, but no step mentions it: write each into the goal and an acceptance criterion of the "
        "step that delivers it (a file to read or create, a status code to answer)"
    ]


def _missing_job_literals(plan: PlanContract, ctx: ValidationContext) -> list[str]:
    literals = job_literals(ctx.job_text)
    if not literals:
        return []
    text = "\n".join(_strings([s.model_dump(mode="json", exclude={"id", "depends_on"}) for s in plan.steps])).lower()
    return [lit for lit in literals if lit.lower() not in text and lit.rsplit("/", 1)[-1].lower() not in text]


_SENTENCE_END = re.compile(r"(?<=[.!?:])\s+|\n+")
_WORD = re.compile(r"[A-Za-zÄÖÜäöüß][\wÄÖÜäöüß-]{3,}")


_WRAPPED_LINE = re.compile(r"[ \t]*\n[ \t]*(?![-*•][ \t])")


def _job_sentences(job_text: str) -> list[str]:
    """The sentences of the job; a line that continues a wrapped sentence joins it, a list item starts a new one
    ("Die README beschreibt⏎die neuen Felder" is one sentence, BUG-333)."""
    sentences = (s.strip().lstrip("-*•").strip() for s in _SENTENCE_END.split(_WRAPPED_LINE.sub(" ", job_text)))
    return [s for s in sentences if s]


def _literal_context(literal: str, job_text: str) -> str:
    """The job sentence(s) naming ``literal`` – they say which part of the work it belongs to."""
    return " ".join(s for s in _job_sentences(job_text) if literal in s) or literal


def _delivering_step(literal: str, steps: Sequence[PlanStep], ctx: ValidationContext) -> PlanStep:
    """The step that delivers ``literal``: one whose files the literal or its job sentence names, else the one whose
    goal shares the most words with that sentence, else the first."""
    context = _literal_context(literal, ctx.job_text)
    named = {p.rsplit("/", 1)[-1].lower() for p in job_literals(context) if _is_job_path(p)}
    words = {w.lower() for w in _WORD.findall(context)}

    def score(step: PlanStep) -> tuple[int, int]:
        files = {p.rsplit("/", 1)[-1].lower() for p in (*_step_paths(step, ctx), *step.repo_hints)}
        own = {w.lower() for w in _WORD.findall(f"{step.title} {step.goal}")}
        return len(files & named), len(words & own)

    return max(steps, key=score)  # max keeps the first of equal scores


def carry_job_literals(plan: PlanContract, ctx: ValidationContext) -> tuple[PlanContract, list[str]]:
    """Write every job literal no step names into the goal of the step that delivers it.

    Ladder rc65: the first plans of A4 and A7 left out `Giro: 1234.50` and `Authorization: Bearer <token>`; each repair
    turn of the 27B planner took about 10 min, so A7 spent 29 of its 40 min planning and the coder ran out of time
    (BUG-312). Which step delivers a literal follows from the job sentence naming it, so no model turn is needed.
    ``_job_literal_errors`` stays as the check for plans without a step that changes the workspace."""
    missing = _missing_job_literals(plan, ctx)
    steps = [s for s in plan.steps if s.kind.value in WORKSPACE_MUTATING_KINDS]
    if not missing or not steps:
        return plan, []
    carried: dict[str, list[str]] = {}
    for literal in missing:
        carried.setdefault(_delivering_step(literal, steps, ctx).id, []).append(literal)

    def with_literals(step: PlanStep, lits: list[str]) -> PlanStep:
        goal = f"{step.goal.rstrip()} Exactly as the job states: {', '.join(f'`{lit}`' for lit in lits)}."
        return step.model_copy(update={"goal": goal[:4000]})

    updated = [with_literals(s, carried[s.id]) if s.id in carried else s for s in plan.steps]
    notes = [f"step {sid}: job literal(s) {', '.join(repr(lit) for lit in lits)} added to the goal" for sid, lits in carried.items()]
    return plan.model_copy(update={"steps": updated}), notes


# documentation, page and configuration files: code files reach the coder through imports and the files it changes,
# these drop out of a plan unnoticed
_NAMED_FILE_SUFFIXES = frozenset(
    {"md", "rst", "txt", "adoc", "html", "htm", "css", "yaml", "yml", "toml", "json", "ini", "cfg", "conf", "xml", "csv"}
)


def _named_files(ctx: ValidationContext) -> dict[str, str]:
    """Existing documentation, page and configuration files the job names by their stem without a path – "Die README
    beschreibt …" → README.md, "das Dockerfile" → Dockerfile – each with the job sentence(s) naming it. A stem shared
    by more than two files is ambiguous and left out."""
    by_stem: dict[str, list[str]] = {}
    for path in ctx.known_paths:
        name = path.rsplit("/", 1)[-1]
        stem, _, suffix = name.partition(".")
        if ctx.is_known_dir(path) or not stem or stem.startswith("_"):
            continue
        if not suffix or suffix.rsplit(".", 1)[-1].lower() in _NAMED_FILE_SUFFIXES:
            by_stem.setdefault(stem, []).append(path)
    out: dict[str, str] = {}
    for stem, paths in by_stem.items():
        if len(paths) > 2 or not _names_component(stem, ctx.job_text):
            continue
        sentences = [s for s in _job_sentences(ctx.job_text) if _names_component(stem, s)]
        for path in paths:
            out[path] = " ".join(sentences)
    return out


def carry_job_named_files(plan: PlanContract, ctx: ValidationContext) -> tuple[PlanContract, list[str]]:
    """Give an existing file the job names but no workspace-changing step names to the step that delivers it: the
    file joins its hints and the job sentence its goal.

    Ladder A10 #1 on rc78: the job said "Die README beschreibt die neuen Felder und Parameter"; the plan's own goal
    promised to "update the web UI and README", yet its only implement step named neither and only the verify step
    listed README.md – as a file not to change. The coder worked to the step's demands, and the README stayed as it
    was (BUG-333). A name without a path is no literal (``job_literals``), so this compares it with the files of the
    repository; a step that names the file – also as a file it must not change – is the planner's decision."""
    named = _named_files(ctx)
    steps = [s for s in plan.steps if s.kind.value in WORKSPACE_MUTATING_KINDS]
    if not named or not steps:
        return plan, []
    raw = "\n".join(_strings([s.model_dump(mode="json", exclude={"id", "depends_on"}) for s in steps]))
    text = raw.lower()

    def stem(path: str) -> str:
        return path.rsplit("/", 1)[-1].split(".", 1)[0]

    missing = {
        p: sentence
        for p, sentence in named.items()
        if p.lower() not in text and p.rsplit("/", 1)[-1].lower() not in text and not _names_component(stem(p), raw)
    }
    if not missing:
        return plan, []
    carried: dict[str, list[str]] = {}
    for path in missing:
        carried.setdefault(_delivering_step(stem(path), steps, ctx).id, []).append(path)

    def with_files(step: PlanStep, paths: list[str]) -> PlanStep:
        sentences = list(dict.fromkeys(missing[p] for p in paths if missing[p]))
        asks = f" The job also asks: {' '.join(sentences)}" if sentences else ""
        goal = f"{step.goal.rstrip()} Files the job names: {', '.join(f'`{p}`' for p in paths)}.{asks}"
        hints = [*step.repo_hints, *(p for p in paths if p not in step.repo_hints)]
        return step.model_copy(update={"goal": goal[:4000], "repo_hints": hints})

    updated = [with_files(s, carried[s.id]) if s.id in carried else s for s in plan.steps]
    notes = [
        f"step {sid}: file(s) the job names {', '.join(repr(p) for p in paths)} added to goal and hints" for sid, paths in carried.items()
    ]
    return plan.model_copy(update={"steps": updated}), notes


def _reachable(plan: PlanContract, start: str) -> set[str]:
    by_id = {s.id: s for s in plan.steps}
    seen: set[str] = set()
    todo = list(by_id[start].depends_on) if start in by_id else []
    while todo:
        cur = todo.pop()
        if cur not in seen and cur in by_id:
            seen.add(cur)
            todo.extend(by_id[cur].depends_on)
    return seen


def _names_component(stem: str, text: str) -> bool:
    """Whether ``text`` names the component ``stem``: a stem of five or more characters as a word start in any case
    ("startet den Collector" – collector.py); a shorter one only as an upper-case acronym ("die lokale API" – api.py),
    so that ordinary words ("app", "web") do not count (BUG-273)."""
    if len(stem) >= 5:
        return re.search(rf"(?<![\w]){re.escape(stem)}", text, re.IGNORECASE) is not None
    return re.search(rf"(?<![\w]){re.escape(stem.upper())}(?![a-z])", text) is not None


def _component_stems(plan: PlanContract, ctx: ValidationContext, preserved: frozenset[str]) -> dict[str, set[str]]:
    """The code components (file stems, not tests, docs or generic names) each open workspace-mutating step writes."""
    code: dict[str, set[str]] = {}
    for s in plan.steps:
        if s.kind.value in WORKSPACE_MUTATING_KINDS and s.id not in preserved:
            stems = {_code_stem(p) for p in _step_paths(s, ctx) if not TEST_PATH_RE.search(p) and not DOC_PATH_RE.search(p)}
            code[s.id] = {t for t in stems if len(t) >= 3 and t not in _GENERIC_STEMS}
    return code


def _unordered_references(
    plan: PlanContract, new_ids: Sequence[str], ctx: ValidationContext, preserved: frozenset[str]
) -> list[tuple[PlanStep, PlanStep, list[str]]]:
    """(step, other, components): ``step`` names components ``other`` writes and neither depends on the other."""
    code = _component_stems(plan, ctx, preserved)
    by_id = {s.id: s for s in plan.steps}
    out: list[tuple[PlanStep, PlanStep, list[str]]] = []
    reported: set[frozenset[str]] = set()
    for sid in new_ids:
        step = by_id.get(sid)
        if step is None or step.id not in code:
            continue
        text = f"{step.title}\n{step.goal}"
        ancestors = _reachable(plan, step.id)
        for other in plan.steps:
            pair = frozenset({step.id, other.id})
            if other.id not in code or other.id == step.id or pair in reported or other.id in ancestors:
                continue
            if step.id in _reachable(plan, other.id):
                continue
            named = sorted(t for t in code[other.id] - code[step.id] if _names_component(t, text))
            if named:
                reported.add(pair)
                out.append((step, other, named))
    return out


def _unordered_reference_errors(
    plan: PlanContract, new_steps: Sequence[PlanStep], ctx: ValidationContext, preserved: frozenset[str]
) -> list[str]:
    """A step whose title or goal names a component another step writes ("startet den Collector" – collector.py) must
    be ordered with that step by a dependency: unordered, it runs first and guesses the interface. The planner left the
    frontend and the start scripts beside the collector step without one, and both guessed config format, port and
    JSON fields (live G1 on rc39, BUG-251; the written-interface rule of BUG-234 alone did not hold).
    ``_order_unordered_references`` adds most of these dependencies before the check; what is left is reported."""
    return [
        f"step {step.id} refers to {', '.join(named)}, which step {other.id} writes, but neither step depends "
        f"on the other: add {other.id} to {step.id}'s depends_on (or the reverse, if {other.id} uses "
        f"{step.id}'s files) so the consuming step runs after the providing one and works with its real "
        "interface instead of guessing it"
        for step, other, named in _unordered_references(plan, [s.id for s in new_steps], ctx, preserved)
    ]


def _order_unordered_references(plan: PlanContract, ctx: ValidationContext, preserved: frozenset[str]) -> tuple[PlanContract, list[str]]:
    """Add the dependency ``_unordered_reference_errors`` asks for: the step that names another step's component
    depends on it. Live G1 on rc88: each of three planner answers fixed the pairs the repair named and left others
    unordered, and the job failed after 17 minutes of planning (BUG-352). A step that only writes tests is left to the
    planner (it belongs merged into the code step, ``_split_test_errors``); so is a dependency that would close a
    cycle, which the reachability check already excludes."""
    notes: list[str] = []
    new_ids = [s.id for s in plan.steps if s.id not in preserved]
    for _ in range(len(plan.steps) ** 2):
        pending = [
            (step, other, named)
            for step, other, named in _unordered_references(plan, new_ids, ctx, preserved)
            if _component_stems(plan, ctx, preserved).get(step.id) or not any(TEST_PATH_RE.search(p) for p in _step_paths(step, ctx))
        ]
        if not pending:
            break
        step, other, named = pending[0]
        updated = step.model_copy(update={"depends_on": [*step.depends_on, other.id]})
        plan = plan.model_copy(update={"steps": [updated if s.id == step.id else s for s in plan.steps]})
        notes.append(f"step {step.id}: depends on {other.id} now, whose {', '.join(named)} it uses")
    return plan, notes


_GOAL_FILE_RE = re.compile(r"(?<![\w./-])((?:[\w-]+/)*[\w-]+(?:\.[\w-]+)*\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w/-])")


def _unlisted_goal_files(step: PlanStep, plan: PlanContract, ctx: ValidationContext) -> list[str]:
    """The repository files – existing or created by another step – the goal of ``step`` names without listing them
    among its files or protecting them with ``must_not_change``."""
    if step.kind.value not in WORKSPACE_MUTATING_KINDS:
        return []
    own = _step_paths(step, ctx)
    protected = [g for c in step.acceptance if isinstance(c, DiffEvidence) for g in c.must_not_change]
    files = set(ctx.known_paths)
    for other in plan.steps:
        if other.id != step.id:
            files.update(p for p in other.allowed_new_paths if not any(c in GLOB_CHARS for c in p) and not p.endswith("/"))
    by_name: dict[str, list[str]] = {}
    for f in files:
        by_name.setdefault(PurePosixPath(f).name, []).append(f)
    out: list[str] = []
    for m in _GOAL_FILE_RE.finditer(step.goal):
        token = m.group(1)
        named = [token] if token in files else (by_name.get(token, []) if "/" not in token else [])
        if len(named) != 1:
            continue
        path = named[0]
        if any(path == o or _globs_overlap(path, o) or _globs_overlap(o, path) for o in own):
            continue
        if any(acceptance_glob_matches(path, g) for g in protected):
            continue
        if path not in out:
            out.append(path)
    return out


def _goal_file_errors(step: PlanStep, plan: PlanContract, ctx: ValidationContext) -> list[str]:
    """A step whose goal names a repository file – one that exists or another step creates – must list it among its
    files (repo_hints or allowed_new_paths). Unlisted, the scope leaves it out: the replanned config step of G1 was to
    "implement the data models in collector.py" with only README/config/docs in scope, worked on collector.py as an
    adopted leftover and was measured on the 28 failing collector tests of later steps until it stagnated (BUG-268).

    A file the step protects with ``must_not_change`` may be named without being listed: "the input data
    data/buchungen.csv stays unchanged" names it to protect it. Listed as a hint it counted as the step's own file and
    lost the protection (BUG-292), so a goal could never name the data it must not touch (BUG-297).
    ``_list_goal_files`` lists them before the check (BUG-353)."""
    return [
        f"step {step.id}: its goal names {path}, which is not among the step's files: add it to repo_hints (or "
        "allowed_new_paths when the step creates it), or leave it out of the goal if another step changes it; a file the "
        "step only reads and must keep as it is: protect it with must_not_change"
        for path in _unlisted_goal_files(step, plan, ctx)
    ]


def _list_goal_files(plan: PlanContract, ctx: ValidationContext, preserved: frozenset[str]) -> tuple[PlanContract, list[str]]:
    """Add the files a step's goal names to its repo_hints – the first remedy ``_goal_file_errors`` names, and one that
    forbids nothing. Live G1 on rc89: two of three planner answers failed on one such file each (docs/api.md,
    config.example.toml) while the repair turns fixed one class of errors and brought another (BUG-353)."""
    notes: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        named = [] if step.id in preserved else _unlisted_goal_files(step, plan, ctx)
        if named:
            notes.append(f"step {step.id}: file(s) its goal names added to repo_hints: {', '.join(named)}")
        steps.append(step.model_copy(update={"repo_hints": [*step.repo_hints, *named]}) if named else step)
    return (plan.model_copy(update={"steps": steps}), notes) if notes else (plan, [])


def _ground_hints(plan: PlanContract, ctx: ValidationContext, preserved: frozenset[str]) -> tuple[PlanContract, list[str]]:
    """Repair the repo_hints ``_hint_errors`` rejects where the remedy is mechanical (BUG-353): a symbol the context does
    not contain is dropped (symbols only steer retrieval); a file another step creates makes this step depend on that
    one unless the order is the reverse; a path that neither exists nor is created moves to allowed_new_paths of a
    workspace-mutating step ("files to be created belong in allowed_new_paths") and is dropped from another step that
    keeps other hints. Live G1 on rc89: an answer named 'src/collector' and the symbol 'web' in two steps."""
    if not ctx.known_paths:
        return plan, []
    notes: list[str] = []
    for sid in [s.id for s in plan.steps if s.id not in preserved]:
        step = next(s for s in plan.steps if s.id == sid)
        hints, new_paths, depends = list(step.repo_hints), list(step.allowed_new_paths), list(step.depends_on)
        mutating = step.kind.value in WORKSPACE_MUTATING_KINDS
        for hint in step.repo_hints:
            kind = ctx.classify_hint(hint)
            if kind == "symbol":
                if not ctx.symbol_known(hint):
                    hints.remove(hint)
                    notes.append(f"step {sid}: dropped repo_hint symbol '{hint}', which the repository context does not contain")
                continue
            if kind != "path":
                continue
            try:
                path = normalise_path(hint)
            except ValueError:
                continue
            if is_catch_all(path) or ctx.path_exists(path) or any_match(path, list(ctx.always_forbidden)):
                continue
            concrete = not any(c in GLOB_CHARS for c in path)
            made_by = [o.id for o in plan.steps if o.id != sid and concrete and any_match(path, list(o.allowed_new_paths))]
            if made_by:
                if not any(m in depends or m in _reachable(plan, sid) for m in made_by) and sid not in _reachable(plan, made_by[0]):
                    depends.append(made_by[0])
                    notes.append(f"step {sid}: depends on {made_by[0]} now, which creates its repo_hint {path}")
                continue
            if mutating:
                hints.remove(hint)
                new_paths.append(hint)
                notes.append(f"step {sid}: repo_hint {path} does not exist yet; moved to allowed_new_paths")
            elif len(hints) > 1:
                hints.remove(hint)
                notes.append(f"step {sid}: dropped repo_hint {path}, which does not exist")
        if (hints, new_paths, depends) != (list(step.repo_hints), list(step.allowed_new_paths), list(step.depends_on)):
            updated = step.model_copy(update={"repo_hints": hints, "allowed_new_paths": new_paths, "depends_on": depends})
            plan = plan.model_copy(update={"steps": [updated if s.id == sid else s for s in plan.steps]})
    return plan, notes


#: a cargo build product addressed below ./target; the sandbox images build into $CARGO_TARGET_DIR (/tmp/cargo-target)
_CARGO_PRODUCT = re.compile(r"(?<![\w}$/.-])(?:\./)?target/(debug|release)/")


def _cargo_target_commands(plan: PlanContract, preserved: frozenset[str]) -> tuple[PlanContract, list[str]]:
    """Point command and test evidence at the cargo build products where cargo puts them (BUG-379): the rust image
    sets CARGO_TARGET_DIR, so `cargo build && ./target/debug/app` finds no program. ``${CARGO_TARGET_DIR:-target}``
    is the same path outside the sandbox. Live R2-rust on rc103: the coder could not run the plan's own acceptance
    command and asked for a replan to add .cargo/config.toml."""
    notes: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        acceptance: list[AcceptanceCriterion] = []
        for crit in step.acceptance:
            command = crit.command if isinstance(crit, CommandEvidence | TestEvidence) else ""
            moved = _CARGO_PRODUCT.sub(r"${CARGO_TARGET_DIR:-target}/\1/", command)
            if step.id in preserved or moved == command or len(moved) > 2000:
                acceptance.append(crit)
                continue
            acceptance.append(crit.model_copy(update={"command": moved}))
            notes.append(f"step {step.id}: cargo build products addressed as ${{CARGO_TARGET_DIR:-target}} in '{moved[:80]}'")
        steps.append(step.model_copy(update={"acceptance": acceptance}) if acceptance != step.acceptance else step)
    if not notes:
        return plan, []
    return plan.model_copy(update={"steps": steps}), notes


def _step_size_errors(step: PlanStep, ctx: ValidationContext) -> list[str]:
    """A step that creates more files than one coder attempt can write fails late on its turn budget. After the
    code-and-tests rule the planner merged a whole widget (collector, frontend, scripts, tests, fixtures, README:
    13 new files) into one step (grand job G1 on 0.1.0rc26, BUG-226)."""
    if step.kind.value not in WORKSPACE_MUTATING_KINDS:
        return []
    n = len(step.allowed_new_paths)
    if n <= ctx.max_new_files_per_step:
        return []
    return [
        f"step {step.id} creates {n} new files; one step can create at most {ctx.max_new_files_per_step} within its turn "
        "budget: split it by component (e.g. backend, frontend, scripts, documentation) into several steps, each with "
        "the tests of its own code"
    ]


def _must_change_errors(step: PlanStep, ctx: ValidationContext) -> list[str]:
    """A ``must_change`` glob that can match none of the step's files can never pass: the verifier would fail a
    correctly finished step (live job e3cab55e, analysis 2026-10-16, U4)."""
    if step.kind.value not in WORKSPACE_MUTATING_KINDS:
        return []
    paths = _step_paths(step, ctx)
    if not paths:
        return []
    errors: list[str] = []
    for idx, crit in enumerate(step.acceptance):
        if not isinstance(crit, DiffEvidence):
            continue
        for glob in crit.must_change:
            if not any(_globs_overlap(glob, p) for p in paths):
                errors.append(
                    f"step {step.id}: acceptance[{idx}] (diff) must_change '{glob[:120]}' matches none of the files the "
                    f"step may change ({', '.join(paths[:8])}); use those paths"
                )
    return errors


def prune_unreachable_must_change(
    plan: PlanContract, ctx: ValidationContext, *, preserved: frozenset[str] = frozenset()
) -> tuple[PlanContract, list[str]]:
    """Drop ``must_change`` globs a step can never satisfy when the step still requires a change it can make.

    A runaway answer listed dozens of invented files (src/utils/encoders.js, ...) next to the right ones; rejecting
    the whole plan made the repair turns repeat it until the job failed (rc16 battery J5, BUG-188). The step could not
    change those files anyway, so dropping them narrows nothing it can do. A step whose ``must_change`` globs ALL miss
    keeps them: that is a wrong path the planner has to fix (``_must_change_errors``)."""
    plan, notes = _drop_existing_manifest_must_change(plan, ctx, preserved)
    plan, contradicting = _drop_self_contradicting_must_not_change(plan, ctx, preserved)
    notes += contradicting
    plan, moved = _move_foreign_tests(plan, ctx, preserved)
    notes += moved
    plan, listed = _list_goal_files(plan, ctx, preserved)
    notes += listed
    plan, grounded = _ground_hints(plan, ctx, preserved)
    notes += grounded
    plan, ordered = _order_unordered_references(plan, ctx, preserved)
    notes += ordered
    plan, unnamed = _drop_unnamed_existing_must_change(plan, ctx, preserved)
    notes += unnamed
    plan, cargo = _cargo_target_commands(plan, preserved)
    notes += cargo
    steps: list[PlanStep] = []
    for step in plan.steps:
        paths = _step_paths(step, ctx) if step.id not in preserved and step.kind.value in WORKSPACE_MUTATING_KINDS else []
        diffs = [c for c in step.acceptance if isinstance(c, DiffEvidence)]
        reachable = {g for c in diffs for g in c.must_change if any(_globs_overlap(g, p) for p in paths)}
        dropped = [g for c in diffs for g in c.must_change if g not in reachable]
        if not paths or not reachable or not dropped:
            steps.append(step)
            continue
        acceptance: list[AcceptanceCriterion] = []
        for crit in step.acceptance:
            if isinstance(crit, DiffEvidence) and any(g not in reachable for g in crit.must_change):
                keep = [g for g in crit.must_change if g in reachable]
                if not keep and not crit.must_not_change and crit.max_changed_files is None:
                    continue
                acceptance.append(crit.model_copy(update={"must_change": keep}))
                continue
            acceptance.append(crit)
        steps.append(step.model_copy(update={"acceptance": acceptance}))
        shown = ", ".join(g[:80] for g in dropped[:5]) + (f" (+{len(dropped) - 5})" if len(dropped) > 5 else "")
        notes.append(f"step {step.id}: dropped must_change outside the step's files: {shown}")
    if not notes:
        return plan, []
    return plan.model_copy(update={"steps": steps}), notes


DEPENDENCY_MANIFESTS = ("requirements.txt", "package.json")
#: dependency manifests and lockfiles of the supported ecosystems (job metrics count changes to them as new dependencies)
EXISTING_MANIFEST_NAMES = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        "composer.json",
        "composer.lock",
        "Gemfile",
        "Gemfile.lock",
    }
)


def _drop_self_contradicting_must_not_change(
    plan: PlanContract, ctx: ValidationContext, preserved: frozenset[str]
) -> tuple[PlanContract, list[str]]:
    """``must_not_change`` on a file the step itself must change or is pointed at can never pass. A degenerate planner
    answer repeated its acceptance blocks and forbade the step's own store.py/test_store.py; the replanner copied the
    criterion twice and the bug-fix job ended with REPLAN_LIMIT (rc19 battery J6, BUG-204). Dropped, with a note –
    repair turns on such an answer only repeat it.

    Only files the step writes count as its own: new paths and hints its title or goal names. A hint the step only reads
    stays protected – cycle 2 M2 on rc60 lost the protection of the job's input data (``data/buchungen.csv``) and the
    coder added an invented booking to it to test the last month (BUG-292)."""
    notes: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        if step.id in preserved or step.kind.value not in WORKSPACE_MUTATING_KINDS:
            steps.append(step)
            continue
        text = f"{step.title}\n{step.goal}".lower()
        new_paths = set(step.allowed_new_paths)
        concrete = [
            p
            for p in _step_paths(step, ctx)
            if not any(c in GLOB_CHARS for c in p) and not p.endswith("/") and (p in new_paths or _named_in(p, text))
        ]
        required = {g for c in step.acceptance if isinstance(c, DiffEvidence) for g in c.must_change}
        dropped: list[str] = []
        acceptance: list[AcceptanceCriterion] = []
        for crit in step.acceptance:
            if not isinstance(crit, DiffEvidence) or not crit.must_not_change:
                acceptance.append(crit)
                continue
            keep = [g for g in crit.must_not_change if g not in required and not any(acceptance_glob_matches(p, g) for p in concrete)]
            dropped += [g for g in crit.must_not_change if g not in keep]
            if len(keep) == len(crit.must_not_change):
                acceptance.append(crit)
            elif keep or crit.must_change or crit.max_changed_files is not None:
                acceptance.append(crit.model_copy(update={"must_not_change": keep}))
        if dropped:
            steps.append(step.model_copy(update={"acceptance": acceptance}))
            shown = ", ".join(dict.fromkeys(g[:80] for g in dropped))
            notes.append(f"step {step.id}: dropped must_not_change on the step's own files: {shown}")
        else:
            steps.append(step)
    if not notes:
        return plan, []
    return plan.model_copy(update={"steps": steps}), notes


def _drop_existing_manifest_must_change(
    plan: PlanContract, ctx: ValidationContext, preserved: frozenset[str]
) -> tuple[PlanContract, list[str]]:
    """``must_change`` on an existing dependency manifest the job does not name: the step *may* change it (it stays in
    the scope), but demanding a change made the coder edit a correct package.json just to satisfy the criterion
    (rc19 battery J5, BUG-203). Whether a dependency is really needed is shown by the tests and the import check."""
    job = ctx.job_text.lower()
    notes: list[str] = []
    steps: list[PlanStep] = []
    for step in plan.steps:
        if step.id in preserved or step.kind.value not in WORKSPACE_MUTATING_KINDS:
            steps.append(step)
            continue
        dropped: list[str] = []
        acceptance: list[AcceptanceCriterion] = []
        for crit in step.acceptance:
            if not isinstance(crit, DiffEvidence):
                acceptance.append(crit)
                continue
            keep = []
            for glob in crit.must_change:
                name = glob.rstrip("/").rsplit("/", 1)[-1]
                try:
                    exists = ctx.path_exists(normalise_path(glob))
                except ValueError:
                    exists = False
                if name in EXISTING_MANIFEST_NAMES and exists and name.lower() not in job:
                    dropped.append(glob)
                else:
                    keep.append(glob)
            if len(keep) == len(crit.must_change):
                acceptance.append(crit)
            elif keep or crit.must_not_change or crit.max_changed_files is not None:
                acceptance.append(crit.model_copy(update={"must_change": keep}))
        if dropped:
            steps.append(step.model_copy(update={"acceptance": acceptance}))
            notes.append(f"step {step.id}: {', '.join(dropped[:5])} may change but is not required to (existing manifest)")
        else:
            steps.append(step)
    if not notes:
        return plan, []
    return plan.model_copy(update={"steps": steps}), notes


# a name the job quotes literally ("…", „…“, `…`, '…', «…»): a package it asks for by name
_QUOTED_NAME = re.compile(r"[\"'`\u201e\u201c\u201d\u00ab\u00bb]([A-Za-z0-9@][A-Za-z0-9@/._-]{1,100})[\"'`\u201c\u201d\u00ab\u00bb]")


def _name_key(text: str) -> str:
    return re.sub(r"[-_.]+", "-", text.lower())


def quoted_names(job_text: str) -> list[str]:
    """Names the job quotes, normalised like package names (``-``/``_``/``.`` are equivalent)."""
    return list(dict.fromkeys(_name_key(m.group(1)) for m in _QUOTED_NAME.finditer(job_text)))


def new_manifest_notes(plan: PlanContract, ctx: ValidationContext, preserved: frozenset[str] = frozenset()) -> list[str]:
    """Steps that add a dependency manifest the repository does not have. The runtime installs third-party packages
    there only when the job names them (BUG-174); Gemma still planned Flask for a job that names no framework and the
    whole job failed on refused installs (live battery 2026-10-17, J1, BUG-178). Which packages the plan means is only
    in its prose, so this is advice for one repair turn, not a hard rule (see ``AdviceOnce``)."""
    notes: list[str] = []
    job = ctx.job_text.lower()
    quoted = quoted_names(ctx.job_text)
    for step in plan.steps:
        if step.id in preserved or step.kind.value not in WORKSPACE_MUTATING_KINDS:
            continue
        # the step adds a package the job quotes by name: the manifest is what the job asks for. The advice made Gemma
        # drop requirements.txt for a job that quoted its package, and the job never reached the install that would
        # have shown the package does not exist (live battery J4 on 0.1.0rc14, BUG-183)
        step_text = _name_key(f"{step.title} {step.goal}")
        if any(q in step_text for q in quoted):
            continue
        named = [*step.allowed_new_paths, *step.repo_hints]
        named += [g for c in step.acceptance if isinstance(c, DiffEvidence) for g in c.must_change]
        for raw in dict.fromkeys(named):
            try:
                path = normalise_path(raw)
            except ValueError:
                continue  # invalid paths are reported by the semantic validation
            name = path.rsplit("/", 1)[-1]
            if name not in DEPENDENCY_MANIFESTS or ctx.path_exists(path) or name in job:
                continue
            notes.append(
                f"step {step.id}: adds {path}, but the repository has no dependency manifest, and the runtime installs "
                "third-party packages there only if the job names them. If the job names every package this step "
                f"needs, answer again unchanged; otherwise plan the standard-library solution without {path} and "
                "without third-party imports (the sandbox already has the test runner: pytest/unittest, node --test)."
            )
            break
    return notes


class AdviceOnce:
    """Advice the planner gets in one repair turn; repeating the same plan afterwards is accepted."""

    def __init__(self) -> None:
        self.given = False

    def __call__(self, notes: list[str]) -> list[str]:
        if self.given or not notes:
            return []
        self.given = True
        return notes


def _literal_prefix(pattern: str) -> str:
    cut = min((pattern.find(c) for c in "*?[" if c in pattern), default=len(pattern))
    return pattern[:cut]


def _globs_overlap(glob: str, path: str) -> bool:
    """Whether the acceptance ``glob`` can match a file covered by the step path/pattern ``path`` (conservative:
    when unsure, they overlap)."""
    g = glob.strip()
    if not g:
        return True
    concrete = not any(c in GLOB_CHARS for c in path) and not path.endswith("/")
    if concrete:
        return acceptance_glob_matches(path, g)
    if "/" not in g.rstrip("/") and any(c in g for c in "*?["):
        return True  # file-name pattern (gitignore convention): may match a file anywhere
    a, b = _literal_prefix(g), _literal_prefix(path)
    return a.startswith(b) or b.startswith(a)


def find_research_step(question: str, steps: Sequence[PlanStep]) -> PlanStep | None:
    """An existing research step that already covers ``question`` (deterministic): normalised containment, or the step
    names at least ``RESEARCH_COVERAGE`` of the question's content words. The planner often writes its own research
    step *and* lists the same question, phrased differently, under research_needed; containment alone then generated
    a duplicate research step per topic (live grand job G1 on 0.1.0rc24, BUG-221)."""
    q = normalise_text(question)
    if not q:
        return None
    q_tokens = content_tokens(question)
    best: tuple[float, PlanStep] | None = None
    for step in steps:
        if step.kind.value != StepKind.research.value:
            continue
        goal, title = normalise_text(step.goal), normalise_text(step.title)
        if q in goal or q in title or (len(goal) >= 12 and goal in q):
            return step
        if len(q_tokens) >= _MIN_COVERAGE_TOKENS:
            covered = len(q_tokens & (content_tokens(step.goal) | content_tokens(step.title))) / len(q_tokens)
            if covered >= RESEARCH_COVERAGE and (best is None or covered > best[0]):
                best = (covered, step)
    return best[1] if best else None


def open_research_requests(plan: PlanContract) -> list[ResearchRequest]:
    """``research_needed`` entries that are not yet covered by a research step (duplicates removed)."""
    out: list[ResearchRequest] = []
    seen: set[str] = set()
    for req in plan.research_needed:
        key = normalise_text(req.question)
        if key in seen or find_research_step(req.question, plan.steps) is not None:
            continue
        seen.add(key)
        out.append(req)
    return out


def ancestors(plan: PlanContract) -> dict[str, set[str]]:
    """Transitive dependencies of every step (the plan is a validated DAG)."""
    deps = {s.id: set(s.depends_on) for s in plan.steps}
    memo: dict[str, set[str]] = {}
    for sid in plan.topological_order():
        acc: set[str] = set()
        for d in deps[sid]:
            acc.add(d)
            acc |= memo.get(d, set())
        memo[sid] = acc
    return memo


def semantic_errors(
    plan: PlanContract,
    ctx: ValidationContext,
    *,
    preserved: frozenset[str] = frozenset(),
    limit: int = 30,
) -> list[str]:
    """All semantic errors of ``plan``. Steps in ``preserved`` (completed steps of a replan) are history and are
    only used for the structural checks."""
    errors: list[str] = []
    new_steps = [s for s in plan.steps if s.id not in preserved]
    open_requests = open_research_requests(plan)

    if len(new_steps) + len(open_requests) > ctx.max_steps:
        errors.append(f"plan has {len(new_steps)} steps plus {len(open_requests)} research request(s); at most {ctx.max_steps} are allowed")
    if len(plan.steps) + len(open_requests) > CONTRACT_MAX_STEPS:
        errors.append(f"the complete plan (including completed steps) would exceed {CONTRACT_MAX_STEPS} steps")
    if open_requests and ctx.research_capability is None:
        errors.append("research_needed requires the 'research' capability, which is not available; state an assumption instead")

    errors.extend(_research_question_errors(plan, new_steps))
    errors.extend(_split_test_errors(plan, new_steps, ctx, preserved))
    errors.extend(_unordered_reference_errors(plan, new_steps, ctx, preserved))
    errors.extend(_check_only_creation_errors(new_steps))
    if not preserved:  # a replan keeps the replaced step's requirements as a constraint instead (BUG-285)
        errors.extend(_job_literal_errors(plan, ctx))
    for step in new_steps:
        errors.extend(_must_change_errors(step, ctx))
        errors.extend(_step_size_errors(step, ctx))
        errors.extend(_capability_errors(step, ctx))
        errors.extend(_scope_source_errors(step, ctx))
        errors.extend(_hint_errors(step, ctx, plan))
        errors.extend(_goal_file_errors(step, plan, ctx))
        errors.extend(_acceptance_errors(step, ctx))

    # duplicate work
    seen_goal: dict[tuple[str, str], str] = {}
    seen_title: dict[tuple[str, str], str] = {}
    for step in plan.steps:
        for key, seen, label in (
            ((step.kind.value, normalise_text(step.goal)), seen_goal, "goal"),
            ((step.kind.value, normalise_text(step.title)), seen_title, "title"),
        ):
            other = seen.get(key)
            if other is not None and (step.id not in preserved or other not in preserved):
                hint = " (it is already completed; depend on it instead of repeating it)" if other in preserved else ""
                errors.append(f"steps {other} and {step.id} describe the same work (kind {step.kind.value}, same {label}){hint}")
            seen.setdefault(key, step.id)

    # dependency rules
    dependants: dict[str, int] = {s.id: 0 for s in plan.steps}
    for step in plan.steps:
        for dep in set(step.depends_on):
            if dep in dependants:
                dependants[dep] += 1
    kinds = {s.id: s.kind.value for s in plan.steps}
    anc = ancestors(plan)
    has_work = any(s.kind.value not in INFO_KINDS for s in plan.steps)
    for step in new_steps:
        kind = step.kind.value
        if kind in CHECKING_KINDS and not step.depends_on:
            errors.append(f"step {step.id}: {kind} step must depend on the step(s) it checks{_checked_candidates(plan, step)}")
        if kind in INFO_KINDS and has_work and dependants.get(step.id, 0) == 0:
            errors.append(
                f"step {step.id}: {kind} step is unreachable for the rest of the plan (no step depends on it); "
                "add it to depends_on of the steps that need its result or remove it"
            )
        if kind == StepKind.research.value:
            mutating_before = sorted(d for d in anc.get(step.id, set()) if kinds.get(d) in MUTATING_KINDS and d not in preserved)
            if mutating_before:
                errors.append(
                    f"step {step.id}: research must run before the work that needs it, but it depends on mutating "
                    f"step(s) {', '.join(mutating_before)}"
                )

    unique: list[str] = []
    for e in errors:
        if e not in unique:
            unique.append(e)
    return unique[:limit]
