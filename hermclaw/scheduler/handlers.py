"""Step/job handler interfaces used by the scheduler (P25).

The scheduler owns *when* and *where* something runs (DAG readiness, leases, retries, timeouts,
pause/cancel). Handlers own *what* runs for a step kind (coder loop, research, verification, media…).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import HermclawConfig

Outcome = Literal["completed", "failed", "blocked", "checkpointed", "replan", "cancelled"]


@dataclass
class StepOutcome:
    outcome: Outcome
    summary: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    replan_reason: str | None = None
    replan_evidence: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None


class CancelToken:
    """Cooperative cancellation/pause signal for long running handlers."""

    def __init__(self) -> None:
        self._cancel = asyncio.Event()
        self._yield = asyncio.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = reason
        self._cancel.set()

    def request_checkpoint(self, reason: str = "pause") -> None:
        self.reason = reason
        self._yield.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def checkpoint_requested(self) -> bool:
        return self._yield.is_set()


@dataclass
class StepRunContext:
    job_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_no: int
    attempt_kind: str  # initial|correction|retry|resume
    step_key: str
    kind: str
    capability: str
    sessionmaker: async_sessionmaker[AsyncSession]
    config: HermclawConfig
    token: CancelToken
    checkpoint: dict[str, Any] = field(default_factory=dict)
    correction_input: dict[str, Any] = field(default_factory=dict)
    services: Any = None  # hermclaw.runtime.services.RuntimeServices (late bound)


class StepHandler(Protocol):
    kinds: frozenset[str]

    async def run(self, ctx: StepRunContext) -> StepOutcome: ...


@dataclass
class JobPhaseResult:
    ok: bool
    next_status: str | None = None  # explicit target job status, e.g. "running" after planning
    error_code: str | None = None
    error_message: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class JobDriver(Protocol):
    """Job-level phases before/after the DAG: preparation (inventory/triage/research/planning) and finalisation (commit/push)."""

    async def prepare(self, job_id: uuid.UUID, token: CancelToken) -> JobPhaseResult: ...

    async def finalize(self, job_id: uuid.UUID, token: CancelToken) -> JobPhaseResult: ...

    async def replan(self, job_id: uuid.UUID, reason: str, evidence: dict[str, Any], token: CancelToken) -> JobPhaseResult:
        """Create a new plan version. The driver (the replanner) increments ``jobs.replan_count`` in its own transaction."""
        ...
