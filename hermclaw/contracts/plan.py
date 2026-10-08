"""PlanContract – Gemma planner output, validated before persistence (Bauplan §15)."""

from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from hermclaw.contracts.acceptance import AcceptanceCriterion
from hermclaw.contracts.common import Contract, Risk, StepKind

STEP_ID_RE = re.compile(r"^S\d{3}$")


class PlanStep(Contract):
    id: str = Field(description="S001, S002, ...")
    title: str = Field(min_length=3, max_length=200)
    kind: StepKind
    capability: str = Field(min_length=2, max_length=64)
    goal: str = Field(min_length=5, max_length=4000)
    depends_on: list[str] = Field(default_factory=list)
    repo_hints: list[str] = Field(default_factory=list, max_length=40)
    constraints: list[str] = Field(default_factory=list, max_length=40)
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list, max_length=30)
    preferred_worker_capabilities: list[str] = Field(default_factory=list)
    risk: Risk = Risk.low
    allowed_new_paths: list[str] = Field(default_factory=list, max_length=40)
    forbidden_paths: list[str] = Field(default_factory=list, max_length=40)
    network: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not STEP_ID_RE.match(v):
            raise ValueError("step id must match S000 pattern, e.g. S001")
        return v


class ResearchRequest(Contract):
    question: str = Field(min_length=5, max_length=1000)
    reason: str = Field(default="", max_length=1000)


class PlanContract(Contract):
    goal: str = Field(min_length=3, max_length=4000)
    summary: str = Field(default="", max_length=4000)
    assumptions: list[str] = Field(default_factory=list, max_length=40)
    risks: list[str] = Field(default_factory=list, max_length=40)
    research_needed: list[ResearchRequest] = Field(default_factory=list, max_length=10)
    steps: list[PlanStep] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def _dag(self) -> PlanContract:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        known = set(ids)
        for s in self.steps:
            for d in s.depends_on:
                if d not in known:
                    raise ValueError(f"step {s.id} depends on unknown step {d}")
                if d == s.id:
                    raise ValueError(f"step {s.id} depends on itself")
        # cycle detection (Kahn)
        indeg = {s.id: len(set(s.depends_on)) for s in self.steps}
        children: dict[str, list[str]] = {s.id: [] for s in self.steps}
        for s in self.steps:
            for d in set(s.depends_on):
                children[d].append(s.id)
        queue = [i for i, n in indeg.items() if n == 0]
        seen = 0
        while queue:
            cur = queue.pop()
            seen += 1
            for c in children[cur]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    queue.append(c)
        if seen != len(self.steps):
            raise ValueError("plan dependencies contain a cycle")
        return self

    def topological_order(self) -> list[str]:
        order: list[str] = []
        done: set[str] = set()
        remaining = {s.id: set(s.depends_on) for s in self.steps}
        while remaining:
            ready = sorted(k for k, deps in remaining.items() if deps <= done)
            if not ready:  # pragma: no cover - guarded by validator
                raise ValueError("cycle")
            for r in ready:
                order.append(r)
                done.add(r)
                del remaining[r]
        return order
