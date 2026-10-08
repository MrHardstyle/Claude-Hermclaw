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
    PresenceEvidence,
    SchemaEvidence,
    TestEvidence,
)
from hermclaw.contracts.common import MUTATING_STEP_KINDS, StepKind
from hermclaw.contracts.plan import PlanContract, PlanStep, ResearchRequest
from hermclaw.contracts.scope import normalise_path
from hermclaw.core.config import HermclawConfig
from hermclaw.planner.inputs import (
    PlannerInput,
    PlannerSettings,
    TestFramework,
    collect_known_paths,
    collect_known_symbols,
    detect_test_command,
)
from hermclaw.scope.guard import any_match, path_matches

MUTATING_KINDS = frozenset(k.value for k in MUTATING_STEP_KINDS)
INFO_KINDS = frozenset({StepKind.research.value, StepKind.discover.value, StepKind.inventory.value})
RUNTIME_ONLY_KINDS = frozenset({StepKind.plan.value, StepKind.replan.value})
REMOTE_MUTATING_KINDS = frozenset({StepKind.ssh.value, StepKind.deploy.value})
WORKSPACE_MUTATING_KINDS = MUTATING_KINDS - REMOTE_MUTATING_KINDS
CHECKING_KINDS = frozenset({StepKind.review.value, StepKind.verify.value})
SUBSTANTIVE_EVIDENCE = frozenset({"presence", "absence", "command", "test", "diff", "schema", "artifact"})
CONTRACT_MAX_STEPS = 60

_GLOB_CHARS = frozenset("*?[")
_FILE_EXTENSIONS_TEXT = """py pyi pyx ipynb js mjs cjs jsx ts tsx mts cts vue svelte astro php phtml inc rb erb go rs java
    kt kts scala groovy
    c h cc cpp cxx hpp hh cs fs swift m mm sh bash zsh fish ps1 bat cmd sql psql html htm css scss sass less styl
    json jsonc json5 yaml yml toml ini cfg conf env xml xsd md mdx rst txt adoc lock gradle properties tf tfvars hcl
    j2 jinja jinja2 twig blade tpl mustache hbs service timer socket mount target dockerfile containerfile csv tsv
    svg png jpg jpeg gif webp ico mp4 webm proto graphql gql mod sum neon dist pem crt key pub log patch diff
"""
_FILE_EXTENSIONS = frozenset(_FILE_EXTENSIONS_TEXT.split())
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
    _known_set: frozenset[str] = frozenset()
    _known_dirs: frozenset[str] = frozenset()

    @classmethod
    def build(cls, config: HermclawConfig, inputs: PlannerInput, settings: PlannerSettings, *, extra_text: str = "") -> ValidationContext:
        """``extra_text`` (job goal, constraints, failure evidence) also counts as context symbols may come from."""
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
        context_text = "\n".join(
            [
                *(s.snippet for s in inputs.retrieved_context),
                extra_text,
                json.dumps(inputs.repository_inventory, ensure_ascii=False, default=str),
                json.dumps(inputs.research_summary, ensure_ascii=False, default=str),
                *inputs.existing_tests,
            ]
        )
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
            _known_set=frozenset(known),
            _known_dirs=frozenset(_parent_dirs(known)),
        )

    @property
    def research_capability(self) -> str | None:
        return self.kind_capability.get(StepKind.research.value)

    # ------------------------------------------------------------------ hint helpers
    def classify_hint(self, hint: str) -> HintKind:
        h = hint.strip()
        if not h:
            return "invalid"
        if "/" in h or "\\" in h or any(c in _GLOB_CHARS for c in h) or h in self._known_set or h in self._known_dirs:
            return "path"
        ext = h.rsplit(".", 1)[-1].lower() if "." in h else ""
        if ext in _FILE_EXTENSIONS or h.startswith("."):
            return "path"
        if _SYMBOL_RE.match(h):
            return "symbol"
        return "invalid"

    def path_exists(self, path: str) -> bool:
        """True when ``path`` (file, directory or glob) matches at least one known repository path."""
        if any(c in _GLOB_CHARS for c in path):
            return any(path_matches(k, path) for k in self.known_paths)
        if path.endswith("/"):
            return path.rstrip("/") in self._known_dirs
        return path in self._known_set or path in self._known_dirs

    def is_known_dir(self, path: str) -> bool:
        return path.rstrip("/") in self._known_dirs

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
        if not any(c in _GLOB_CHARS for c in p) and not p.endswith("/") and ctx.is_known_dir(p) and p not in ctx.known_paths:
            p += "/"
        if p not in out:
            out.append(p)
    return out


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
        if not any(c in _GLOB_CHARS for c in p) and any_match(p, list(ctx.always_forbidden)):
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
            if field_name == "allowed_new_paths" and any_match(p, list(ctx.always_forbidden)):
                errors.append(f"step {step.id}: allowed_new_paths entry '{p}' is a forbidden path (runtime policy)")
    return errors


def _acceptance_errors(step: PlanStep, ctx: ValidationContext) -> list[str]:
    errors: list[str] = []
    kind = step.kind.value
    if kind in REMOTE_MUTATING_KINDS and not has_substantive_acceptance(step.acceptance):
        errors.append(
            f"step {step.id}: mutating step of kind '{kind}' needs machine-checkable acceptance "
            "(e.g. command evidence with a read-only check, or artifact evidence)"
        )
    for idx, crit in enumerate(step.acceptance):
        where = f"step {step.id}: acceptance[{idx}] ({crit.type})"
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

    for step in new_steps:
        errors.extend(_capability_errors(step, ctx))
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
            errors.append(f"step {step.id}: {kind} step must depend on the step(s) it checks")
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
