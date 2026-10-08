"""StepContract – everything a worker turn needs, built fresh from persistent state (Bauplan §18)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import Field

from hermclaw.contracts.acceptance import AcceptanceCriterion
from hermclaw.contracts.common import Contract, Risk, StepKind, StepStatus
from hermclaw.contracts.scope import ScopeContract


class StepContract(Contract):
    id: UUID
    job_id: UUID
    step_key: str
    title: str
    kind: StepKind
    capability: str
    goal: str
    status: StepStatus
    risk: Risk = Risk.low
    depends_on: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)
    repo_hints: list[str] = Field(default_factory=list)
    scope: ScopeContract | None = None
    turn_budget: int = 20
    network: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
