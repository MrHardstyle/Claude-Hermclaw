"""Replanning contracts (P24, Bauplan §16).

``ReplanTrigger`` is what the orchestrator hands to the replanner. ``ReplanContract`` is the model's answer: the
remaining work as PlanContract-shaped steps. A step whose id equals a completed step is a deliberate re-run and
must carry a ``rerun_reason``; otherwise completed steps are never repeated. Dependencies may reference completed
step ids, so the DAG is validated after merging the kept completed steps (see ``replanner.merge_plan``).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import Field, model_validator

from hermclaw.contracts.common import Contract
from hermclaw.contracts.plan import PlanStep, ResearchRequest

ReplanReason = Literal[
    "scope_unavailable",
    "stagnation",
    "test_architecture_conflict",
    "missing_dependency",
    "worker_unavailable",
    "research_changed_assumptions",
    "repeated_verifier_failure",
    "repository_changed",
]
REPLAN_REASONS: tuple[str, ...] = (
    "scope_unavailable",
    "stagnation",
    "test_architecture_conflict",
    "missing_dependency",
    "worker_unavailable",
    "research_changed_assumptions",
    "repeated_verifier_failure",
    "repository_changed",
)
# Triggers that prove the failed approach does not work: repeating the failed step unchanged is rejected.
APPROACH_FAILURE_REASONS = frozenset({"scope_unavailable", "stagnation", "test_architecture_conflict", "repeated_verifier_failure"})


class ReplanTrigger(Contract):
    """Why a replan is requested, with deterministic evidence (verifier/test/scope output, never model reasoning)."""

    reason_code: ReplanReason
    failed_step_id: uuid.UUID | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    detail: str = Field(default="", max_length=4000)


class ReplanStep(PlanStep):
    rerun_reason: str | None = Field(
        default=None,
        max_length=1000,
        description="only for re-running an already completed step (same id): why it must run again",
    )


class ReplanContract(Contract):
    goal: str = Field(min_length=3, max_length=4000)
    summary: str = Field(default="", max_length=4000)
    assumptions: list[str] = Field(default_factory=list, max_length=40)
    risks: list[str] = Field(default_factory=list, max_length=40)
    research_needed: list[ResearchRequest] = Field(default_factory=list, max_length=10)
    steps: list[ReplanStep] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def _ids(self) -> ReplanContract:
        ids = [s.id for s in self.steps]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate step ids: {', '.join(dupes)}")
        for s in self.steps:
            if s.id in s.depends_on:
                raise ValueError(f"step {s.id} depends on itself")
        return self
