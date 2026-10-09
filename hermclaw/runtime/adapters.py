"""Thin adapters from concrete components to the runtime/coder ports (no logic of their own)."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.coder.handler import CODE_STEP_KINDS, VerificationOutcome
from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.step import StepContract
from hermclaw.core.config import HermclawConfig
from hermclaw.core.interfaces import CommandExecutor, GitReader, WorkspaceHandle
from hermclaw.persistence.models import Step
from hermclaw.verifier import Verifier
from hermclaw.verifier.types import VerificationStep, load_step

ExecutorFor = Callable[[WorkspaceHandle, StepContract | None], Awaitable[CommandExecutor]]


def merge_scopes(scopes: list[ScopeContract | None]) -> ScopeContract | None:
    """Union of the step scopes of a job (regression rerun after a base update)."""
    present = [s for s in scopes if s is not None]
    if not present:
        return None

    def union(attr: str) -> list[str]:
        out: list[str] = []
        for s in present:
            for p in getattr(s, attr):
                if p not in out:
                    out.append(p)
        return out

    allowed = set(union("target_paths")) | set(union("allowed_new_paths"))
    ops: list[str] = []
    for s in present:
        for op in s.allowed_operations:
            if op not in ops:
                ops.append(op)
    return ScopeContract(
        source="manual",
        target_paths=union("target_paths"),
        allowed_new_paths=union("allowed_new_paths"),
        forbidden_paths=[f for f in union("forbidden_paths") if f not in allowed],
        allowed_operations=ops,  # type: ignore[arg-type]
        strict_target_paths=all(s.strict_target_paths for s in present),
        reason="union of the job's step scopes (regression rerun)",
    )


class VerifierAdapter:
    """``VerifierPort`` (implement handler) and ``RegressionCheck`` (runtime driver) over :class:`Verifier`."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], config: HermclawConfig, git: GitReader, executor_for: ExecutorFor
    ) -> None:
        self.sm = sessionmaker
        self.config = config
        self.git = git
        self.executor_for = executor_for

    async def _verifier(self, workspace: WorkspaceHandle, step: StepContract | None) -> Verifier:
        return Verifier(self.sm, self.config, await self.executor_for(workspace, step), self.git)

    async def verify(
        self, step: StepContract, workspace: WorkspaceHandle, *, job_id: uuid.UUID, step_id: uuid.UUID, attempt_id: uuid.UUID
    ) -> VerificationOutcome:
        vstep = VerificationStep.build(
            key=step.step_key, kind=str(step.kind), acceptance=list(step.acceptance), scope=step.scope, network=step.network
        )
        out = await (await self._verifier(workspace, step)).run(vstep, workspace, job_id=job_id, step_id=step_id, attempt_id=attempt_id)
        return VerificationOutcome(report=out.report, run_id=out.run_id)

    async def rerun(self, job_id: uuid.UUID, workspace: WorkspaceHandle) -> tuple[bool, dict[str, Any]]:
        """23.5: after a base update every completed code step of the job is verified again on the rebased tree."""
        async with self.sm() as s:
            step_ids = list(
                (
                    await s.execute(
                        select(Step.id)
                        .where(
                            Step.job_id == job_id,
                            Step.superseded.is_(False),
                            Step.status == "completed",
                            Step.kind.in_(list(CODE_STEP_KINDS)),
                        )
                        .order_by(Step.created_at, Step.step_key)
                    )
                ).scalars()
            )
            vsteps = [await load_step(s, sid) for sid in step_ids]
        verifier = await self._verifier(workspace, None)
        # the rebased tree carries the changes of *all* steps: verify each step's evidence under the union scope
        merged = merge_scopes([v.scope for v in vsteps])
        failed: list[dict[str, Any]] = []
        for sid, vstep in zip(step_ids, vsteps, strict=True):
            out = await verifier.run(dataclasses.replace(vstep, scope=merged), workspace, job_id=job_id, step_id=sid, attempt_id=None)
            if not out.report.passed:
                failed.append(
                    {"step_key": vstep.key, "summary": out.report.summary, "failures": [c.name for c in out.report.failures][:20]}
                )
        return not failed, {"checked_steps": len(step_ids), "failed": failed}
