"""Planner/replanner errors with stable machine-readable codes (Bauplan §15, §16)."""

from __future__ import annotations

from hermclaw.core.errors import ConflictError, HermclawError

PLANNER_INVALID_OUTPUT = "PLANNER_INVALID_OUTPUT"
PLAN_EXISTS = "PLAN_EXISTS"
PLAN_CHANGED = "PLAN_CHANGED"
REPLAN_LIMIT_REACHED = "REPLAN_LIMIT_REACHED"
NO_PLAN = "NO_PLAN"


class PlannerError(HermclawError):
    """The planner could not produce a valid plan (invalid output after the repair budget)."""

    code = PLANNER_INVALID_OUTPUT
    http_status = 502


class ReplanLimitReached(PlannerError):
    """``policies.correction.max_replans_per_job`` is exhausted for this job."""

    code = REPLAN_LIMIT_REACHED
    http_status = 409


class PlanConflict(ConflictError):
    """Concurrent planning, a second initial plan for a job, or a replan against a stale plan version."""

    code = PLAN_EXISTS


class PlanInvalid(Exception):
    """Internal signal raised by validators inside the repair loop (never leaves the planner package)."""

    def __init__(self, phase: str, errors: list[str]) -> None:
        super().__init__(f"{phase}: {len(errors)} error(s)")
        self.phase = phase
        self.errors = errors
