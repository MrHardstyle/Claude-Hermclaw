"""Deterministic plan enrichment after validation (P14 14.7 risk, 14.8 acceptance, 14.9 research requests).

The model's plan is never trusted to be complete. After schema + semantic validation the runtime adds, in this
order and without any model involvement:

1. research steps for every ``research_needed`` entry that no research step covers yet (14.9); the first work
   steps depend on them, so research always runs before the work that needs it;
2. acceptance criteria for workspace-mutating steps (14.8): scope + security evidence always, and – when the
   model gave no substantive criterion – diff evidence over the step's path hints plus the repository's own test
   command (if one was detected);
3. a risk floor (14.7): deploy/ssh/database are at least ``medium``; implement steps touching many paths without
   test evidence are ``high``. The model's risk is never lowered.

Everything is generic and depends only on the plan, the validation context and the risk policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hermclaw.contracts.acceptance import (
    AcceptanceCriterion,
    DiffEvidence,
    ScopeEvidence,
    SecurityEvidence,
    TestEvidence,
)
from hermclaw.contracts.common import Risk, StepKind
from hermclaw.contracts.plan import PlanContract, PlanStep
from hermclaw.contracts.scope import normalise_path
from hermclaw.core.errors import ValidationFailed
from hermclaw.planner.errors import PlanInvalid
from hermclaw.planner.inputs import GLOB_CHARS, PlannerSettings
from hermclaw.planner.parsing import format_validation_errors
from hermclaw.planner.validation import (
    INFO_KINDS,
    WORKSPACE_MUTATING_KINDS,
    ValidationContext,
    find_research_step,
    has_substantive_acceptance,
    normalise_text,
    open_research_requests,
    path_like_hints,
)

MAX_ACCEPTANCE = 30
RISK_ORDER: dict[Risk, int] = {Risk.low: 0, Risk.medium: 1, Risk.high: 2}
DEFAULT_MIN_RISK: dict[str, Risk] = {
    StepKind.deploy.value: Risk.medium,
    StepKind.ssh.value: Risk.medium,
    StepKind.database.value: Risk.medium,
}


def max_risk(a: Risk, b: Risk) -> Risk:
    return a if RISK_ORDER[a] >= RISK_ORDER[b] else b


class RiskPolicy(BaseModel):
    """Deterministic risk floor. Job-level overrides can only make it stricter, never weaker."""

    model_config = ConfigDict(extra="forbid")

    min_risk_by_kind: dict[str, Risk] = Field(default_factory=lambda: dict(DEFAULT_MIN_RISK))
    many_paths_threshold: int = Field(default=6, ge=1, le=1000)
    high_if_untested_many_paths: bool = True

    @classmethod
    def effective(cls, overrides: dict[str, Any], settings: PlannerSettings) -> RiskPolicy:
        base = cls(many_paths_threshold=settings.many_paths_threshold)
        if not overrides:
            return base
        try:
            extra = cls.model_validate(overrides)
        except ValidationError as exc:
            raise ValidationFailed("invalid risk_policy override", details={"errors": format_validation_errors(exc, overrides)}) from exc
        unknown = sorted(set(extra.min_risk_by_kind) - {k.value for k in StepKind})
        if unknown:
            raise ValidationFailed("invalid risk_policy override", details={"errors": [f"unknown step kind(s): {', '.join(unknown)}"]})
        floors = dict(base.min_risk_by_kind)
        for kind, risk in extra.min_risk_by_kind.items():
            floors[kind] = max_risk(floors.get(kind, Risk.low), risk)
        fields = extra.model_fields_set
        return cls(
            min_risk_by_kind=floors,
            many_paths_threshold=min(base.many_paths_threshold, extra.many_paths_threshold)
            if "many_paths_threshold" in fields
            else base.many_paths_threshold,
            high_if_untested_many_paths=base.high_if_untested_many_paths or extra.high_if_untested_many_paths,
        )

    def prompt_view(self) -> dict[str, Any]:
        return {
            "min_risk_by_kind": {k: v.value for k, v in sorted(self.min_risk_by_kind.items())},
            "high_if_implement_touches_at_least_paths_without_tests": self.many_paths_threshold
            if self.high_if_untested_many_paths
            else None,
            "risk_is_never_lowered_by_runtime": True,
        }


@dataclass
class EnrichmentResult:
    plan: PlanContract
    notes: list[str] = field(default_factory=list)
    generated_research_steps: list[str] = field(default_factory=list)
    generated_acceptance: dict[str, list[str]] = field(default_factory=dict)
    risk_raised: dict[str, tuple[str, str]] = field(default_factory=dict)


# --------------------------------------------------------------------------------------------- research (14.9)
def next_step_id(used: set[str]) -> str:
    numbers = {int(i[1:]) for i in used if len(i) == 4 and i[0] == "S" and i[1:].isdigit()}
    candidate = (max(numbers) + 1) if numbers else 1
    if candidate > 999:
        candidate = next(n for n in range(1, 1000) if n not in numbers)
    return f"S{candidate:03d}"


def _research_step(step_id: str, question: str, reason: str, capability: str, network: bool) -> dict[str, Any]:
    q = " ".join(question.split())
    goal = f"Research and answer with sources: {q}"
    if reason.strip():
        goal += f" Reason: {' '.join(reason.split())}"
    return {
        "id": step_id,
        "title": f"Research: {q}"[:200],
        "kind": StepKind.research.value,
        "capability": capability,
        "goal": goal[:4000],
        "depends_on": [],
        "network": network,
        "risk": Risk.low.value,
    }


def _first_work_steps(steps: Sequence[dict[str, Any]], preserved: frozenset[str]) -> list[str]:
    """New non-information steps whose direct dependencies contain no other new non-information step.

    Every new work step either is such a step or transitively depends on one, so a dependency on the research
    step from these steps orders the research before all new work.
    """
    kinds = {s["id"]: s["kind"] for s in steps}
    out: list[str] = []
    for s in steps:
        if s["id"] in preserved or s["kind"] in INFO_KINDS:
            continue
        if not any(d not in preserved and kinds.get(d) not in INFO_KINDS for d in s.get("depends_on", [])):
            out.append(s["id"])
    return out


def add_research_steps(
    data: dict[str, Any], plan: PlanContract, ctx: ValidationContext, *, preserved: frozenset[str], reserved_ids: set[str]
) -> list[str]:
    """Turn uncovered ``research_needed`` entries into research steps (mutates ``data``); returns the new ids."""
    capability = ctx.research_capability
    requests = open_research_requests(plan)
    if not requests or capability is None:
        return []
    used = {s["id"] for s in data["steps"]} | set(reserved_ids)
    network = ctx.capability_network.get(capability, True)
    created: list[str] = []
    for req in requests:
        step_id = next_step_id(used)
        used.add(step_id)
        data["steps"].insert(len(created), _research_step(step_id, req.question, req.reason, capability, network))
        created.append(step_id)
    first = _first_work_steps(data["steps"], preserved)
    for s in data["steps"]:
        if s["id"] in first:
            s["depends_on"] = [*s.get("depends_on", []), *[c for c in created if c not in s.get("depends_on", [])]]
    return created


# --------------------------------------------------------------------------------------------- acceptance (14.8)
def generate_acceptance(step: PlanStep, ctx: ValidationContext) -> tuple[list[AcceptanceCriterion], list[str]]:
    """Deterministic acceptance for a workspace-mutating step. Returns (acceptance, labels of added criteria)."""
    acceptance: list[AcceptanceCriterion] = list(step.acceptance)
    if step.kind.value not in WORKSPACE_MUTATING_KINDS:
        return acceptance, []
    added: list[str] = []

    def add(item: AcceptanceCriterion, label: str) -> None:
        if len(acceptance) < MAX_ACCEPTANCE:
            acceptance.append(item)
            added.append(label)

    if not has_substantive_acceptance(acceptance):
        must_change = path_like_hints(step, ctx)
        if not must_change:
            must_change = _normalised(step.allowed_new_paths)
        add(
            DiffEvidence(
                description="generated: the step must change its target paths",
                must_change=must_change,
                must_not_change=_normalised(step.forbidden_paths),
                allow_empty=False,
            ),
            "diff",
        )
        if ctx.test_command:
            add(
                TestEvidence(
                    description="generated: repository test suite must pass",
                    command=ctx.test_command,
                    framework=ctx.test_framework,
                ),
                "test",
            )
    if not any(a.type == "scope" for a in acceptance):
        add(ScopeEvidence(description="generated: all changes stay inside the runtime scope"), "scope")
    if not any(a.type == "security" for a in acceptance):
        add(SecurityEvidence(description="generated: no secrets and no conflict markers"), "security")
    return acceptance, added


def _normalised(paths: Sequence[str]) -> list[str]:
    out: list[str] = []
    for raw in paths:
        try:
            p = normalise_path(raw)
        except ValueError:
            continue
        if p not in out:
            out.append(p)
    return out


# --------------------------------------------------------------------------------------------- risk (14.7)
def touched_paths(step: PlanStep, ctx: ValidationContext, *, limit: int) -> int:
    """How many paths a step may touch (capped at ``limit``).

    A glob or directory hint counts every known repository path it matches (at least one), an exact hint and
    every ``allowed_new_paths`` entry count once.
    """
    touched: set[str] = set()
    for hint in path_like_hints(step, ctx):
        if hint.endswith("/") or any(c in GLOB_CHARS for c in hint):
            touched |= ctx.matching_known_paths(hint, limit=limit) or {hint}
        else:
            touched.add(hint)
        if len(touched) >= limit:
            return limit
    touched |= set(_normalised(step.allowed_new_paths))
    return min(len(touched), limit)


def assign_risk(step: PlanStep, ctx: ValidationContext, policy: RiskPolicy) -> tuple[Risk, str | None]:
    floor = policy.min_risk_by_kind.get(step.kind.value, Risk.low)
    reason: str | None = f"kind {step.kind.value} is at least {floor.value}" if floor != Risk.low else None
    if (
        step.kind.value == StepKind.implement.value
        and policy.high_if_untested_many_paths
        and not any(a.type == "test" for a in step.acceptance)
        and touched_paths(step, ctx, limit=policy.many_paths_threshold) >= policy.many_paths_threshold
    ):
        floor = Risk.high
        reason = f"implement step touches at least {policy.many_paths_threshold} paths without test evidence"
    final = max_risk(step.risk, floor)
    return final, (reason if final != step.risk else None)


# --------------------------------------------------------------------------------------------- pipeline
def enrich_plan(
    plan: PlanContract,
    ctx: ValidationContext,
    policy: RiskPolicy,
    *,
    preserved: frozenset[str] = frozenset(),
    reserved_ids: set[str] | None = None,
) -> EnrichmentResult:
    """Apply research steps, acceptance generation and risk floors. Preserved (completed) steps are untouched.

    Raises ``PlanInvalid('enrichment', …)`` if the enriched plan no longer validates (e.g. the step budget is
    exceeded by the generated research steps); the repair loop then reports it to the model.
    """
    data: dict[str, Any] = plan.model_dump(mode="json")
    result = EnrichmentResult(plan=plan)
    created = add_research_steps(data, plan, ctx, preserved=preserved, reserved_ids=reserved_ids or set())
    result.generated_research_steps = created
    for sid in created:
        result.notes.append(f"research step {sid} generated from research_needed")
    try:
        staged = PlanContract.model_validate(data)
    except ValidationError as exc:
        raise PlanInvalid("enrichment", format_validation_errors(exc, data)) from exc

    steps_out: list[dict[str, Any]] = []
    for original in staged.steps:
        if original.id in preserved:
            steps_out.append(original.model_dump(mode="json"))
            continue
        step = original
        acceptance, added = generate_acceptance(step, ctx)
        if added:
            result.generated_acceptance[step.id] = added
            result.notes.append(f"step {step.id}: generated acceptance {', '.join(added)}")
            step = step.model_copy(update={"acceptance": acceptance})
        risk, why = assign_risk(step, ctx, policy)
        if why is not None:
            result.risk_raised[step.id] = (step.risk.value, risk.value)
            result.notes.append(f"step {step.id}: risk raised {step.risk.value} -> {risk.value} ({why})")
            step = step.model_copy(update={"risk": risk})
        steps_out.append(step.model_dump(mode="json"))
    final_data = {**staged.model_dump(mode="json"), "steps": steps_out}
    try:
        result.plan = PlanContract.model_validate(final_data)
    except ValidationError as exc:  # pragma: no cover - generated criteria are valid by construction
        raise PlanInvalid("enrichment", format_validation_errors(exc, final_data)) from exc
    return result


__all__ = [
    "EnrichmentResult",
    "RiskPolicy",
    "add_research_steps",
    "assign_risk",
    "enrich_plan",
    "find_research_step",
    "generate_acceptance",
    "max_risk",
    "next_step_id",
    "normalise_text",
]
