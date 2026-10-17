"""Semantic plan validation on top of the PlanContract schema (P14 14.5/14.6, Bauplan §15).

``PlanContract`` already rejects duplicate ids, unknown/self dependencies and cycles. This module adds the rules
that need runtime knowledge: configured/available capabilities, step-kind -> capability consistency, step budget,
duplicate work, unconsumed (unreachable) information steps, dependency requirements of checking steps,
repository-hint grounding (no invented paths), path hygiene and acceptance sanity. Every rule is generic; the
error texts are sent back to the planner model verbatim in a repair turn.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

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


def _hint_errors(step: PlanStep, ctx: ValidationContext) -> list[str]:
    errors: list[str] = []
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
    r"(?:^|/)(?:tests?|__tests__|spec|specs)(?:/|$)|(?:^|/)test_[^/]*$|_test\.[a-z]+$|\.(?:test|spec)\.[a-z]+$|(?:^|/)conftest\.py$",
    re.IGNORECASE,
)


def _step_paths(step: PlanStep, ctx: ValidationContext) -> list[str]:
    paths = [*path_like_hints(step, ctx)]
    for raw in step.allowed_new_paths:
        try:
            paths.append(normalise_path(raw))
        except ValueError:
            continue
    return paths


def _split_test_errors(plan: PlanContract, new_steps: Sequence[PlanStep], ctx: ValidationContext, preserved: frozenset[str]) -> list[str]:
    """Code and the tests for it belong in one step: an implementing step cannot run tests a later step writes, and
    the test step may not fix the code (analysis 2026-10-16, U3). A step that only writes tests may therefore not
    depend on a workspace-mutating step that writes non-test code."""
    errors: list[str] = []
    by_id = {s.id: s for s in plan.steps}
    for step in new_steps:
        if step.kind.value not in WORKSPACE_MUTATING_KINDS:
            continue
        paths = _step_paths(step, ctx)
        if not paths or not all(TEST_PATH_RE.search(p) for p in paths):
            continue
        for dep in step.depends_on:
            other = by_id.get(dep)
            if other is None or dep in preserved or other.kind.value not in WORKSPACE_MUTATING_KINDS:
                continue  # tests for code a completed step already delivered are a step of their own
            code = [p for p in _step_paths(other, ctx) if not TEST_PATH_RE.search(p)]
            if code:
                errors.append(
                    f"step {step.id} only writes tests for the code of step {dep}: put code and its tests into ONE step "
                    f"(merge {step.id} into {dep}: add its test paths to {dep}'s allowed_new_paths/repo_hints and its "
                    "test acceptance), so the step can run its own tests and fix both"
                )
    return errors


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
    notes: list[str] = []
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
    """An existing research step that already covers ``question`` (normalised containment, deterministic)."""
    q = normalise_text(question)
    if not q:
        return None
    for step in steps:
        if step.kind.value != StepKind.research.value:
            continue
        goal, title = normalise_text(step.goal), normalise_text(step.title)
        if q in goal or q in title or (len(goal) >= 12 and goal in q):
            return step
    return None


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
    for step in new_steps:
        errors.extend(_must_change_errors(step, ctx))
        errors.extend(_capability_errors(step, ctx))
        errors.extend(_scope_source_errors(step, ctx))
        errors.extend(_hint_errors(step, ctx))
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
