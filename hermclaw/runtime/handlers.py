"""Step handlers for verification-only and job-level review steps (plan kinds ``test``, ``verify``, ``review``).

``test`` / ``verify`` – the deterministic verifier evaluates the step's acceptance evidence on the current workspace
                       state (HEAD baseline: earlier steps are committed). Failure → blocked with the failing checks
                       as replan evidence (the planner creates fix steps; completed work is preserved).
``review``           – heavy review (Qwen3.8 27B) of the *whole* job change against the job base, with a verifier run
                       under the union of the job's step scopes as factual input. ``fix_required`` (or any
                       major/blocker finding, invariant enforced) → blocked with the findings as replan evidence.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.coder.handler import CODE_STEP_KINDS, ModelLeaser, ReviewerPort, step_contract
from hermclaw.contracts.review import enforce_review_invariant
from hermclaw.core.config import HermclawConfig
from hermclaw.core.errors import ResourceUnavailable
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.persistence.models import ScopeContractRow, Step
from hermclaw.runtime.adapters import VerifierAdapter, merge_scopes
from hermclaw.runtime.transitions import emit_status
from hermclaw.scheduler.handlers import StepOutcome, StepRunContext

if TYPE_CHECKING:
    from hermclaw.contracts.scope import ScopeContract
    from hermclaw.gitops.engine import GitEngine


async def _workspace(git: GitEngine, job_id: uuid.UUID, *, step_baseline: bool) -> WorkspaceHandle | None:
    for ws in await git.list_workspaces(job_id):
        if ws.status in ("archived", "cleaned", "removed", "failed"):
            continue
        handle = await git.handle(ws)
        if step_baseline:
            fresh = await git.get_workspace(ws.id)
            if fresh.head_sha and fresh.head_sha != handle.base_sha:
                handle = dataclasses.replace(handle, base_sha=fresh.head_sha)
        return handle
    return None


class VerifyStepHandler:
    kinds = frozenset({"test", "verify"})

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], config: HermclawConfig, *, git: GitEngine, verifier: VerifierAdapter
    ) -> None:
        self.sm, self.config, self.git, self.verifier = sessionmaker, config, git, verifier

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        async with self.sm() as s:
            row = (await s.execute(select(Step).where(Step.id == ctx.step_id))).scalar_one()
        handle = await _workspace(self.git, ctx.job_id, step_baseline=True)
        if handle is None:
            return StepOutcome(
                "blocked", error_code="NO_WORKSPACE", error_message="job has no active workspace", replan_reason="missing_dependency"
            )
        async with self.sm() as s:
            await emit_status(s, ctx.job_id, f"Verifikation {row.step_key}: {len(row.acceptance or [])} Kriterien", step_id=ctx.step_id)
            await s.commit()
        out = await self.verifier.verify(step_contract(row), handle, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id)
        if out.report.passed:
            return StepOutcome(
                "completed", summary=out.report.summary or "verification passed", result={"verification_run_id": str(out.run_id)}
            )
        failures = [{"check": f"{c.check_type}:{c.name}", "message": c.message[:500], "evidence": c.evidence} for c in out.report.failures][
            :20
        ]
        return StepOutcome(
            "blocked",
            error_code="VERIFICATION_STEP_FAILED",
            error_message=out.report.summary[:2000],
            replan_reason="step_failed",
            replan_evidence={"verification_run_id": str(out.run_id), "failures": failures},
        )


class ReviewStepHandler:
    kinds = frozenset({"review"})

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig,
        *,
        git: GitEngine,
        verifier: VerifierAdapter,
        reviewer: ReviewerPort,
        resources: ModelLeaser | None = None,
        lease_wait_seconds: float = 3600.0,
    ) -> None:
        self.sm, self.config, self.git, self.verifier, self.reviewer = sessionmaker, config, git, verifier, reviewer
        self.resources = resources
        self.lease_wait_seconds = lease_wait_seconds

    async def _job_scope(self, job_id: uuid.UUID) -> ScopeContract | None:
        from hermclaw.contracts.scope import ScopeContract

        async with self.sm() as s:
            rows = (
                (
                    await s.execute(
                        select(ScopeContractRow)
                        .join(Step, Step.id == ScopeContractRow.step_id)
                        .where(ScopeContractRow.job_id == job_id, ScopeContractRow.status == "active", Step.kind.in_(list(CODE_STEP_KINDS)))
                    )
                )
                .scalars()
                .all()
            )
        return merge_scopes([ScopeContract.model_validate(r.contract) for r in rows])

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        async with self.sm() as s:
            row = (await s.execute(select(Step).where(Step.id == ctx.step_id))).scalar_one()
        handle = await _workspace(self.git, ctx.job_id, step_baseline=False)  # the whole job change vs. the job base
        if handle is None:
            return StepOutcome("completed", summary="nothing to review (job without repository changes)")
        step = step_contract(row, await self._job_scope(ctx.job_id))
        verification = await self.verifier.verify(step, handle, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id)
        async with self.sm() as s:
            await emit_status(s, ctx.job_id, f"Heavy Review {row.step_key} (gesamte Änderung)", step_id=ctx.step_id)
            await s.commit()
        try:
            if self.resources is not None:
                profile = self.config.models.by_role("heavy")
                async with self.resources.hold_model(
                    profile, job_id=ctx.job_id, step_id=ctx.step_id, wait_timeout=self.lease_wait_seconds, preemptible=False
                ):
                    raw = await self.reviewer.review(
                        step, handle, verification.report, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id
                    )
            else:
                raw = await self.reviewer.review(
                    step, handle, verification.report, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id
                )
        except ResourceUnavailable as exc:
            return StepOutcome("failed", error_code=exc.code, error_message=exc.message, retryable=True)
        review, overridden = enforce_review_invariant(raw)
        if review.verdict == "pass" and verification.report.passed:
            return StepOutcome(
                "completed", summary=review.summary or "review passed", result={"verdict": "pass", "findings": len(review.findings)}
            )
        findings: list[dict[str, Any]] = [f.model_dump() for f in review.findings][:30]
        return StepOutcome(
            "blocked",
            error_code="REVIEW_STEP_FIX_REQUIRED",
            error_message=(review.summary or verification.report.summary)[:2000],
            replan_reason="step_failed",
            replan_evidence={"findings": findings, "invariant_override": overridden, "verifier_passed": verification.report.passed},
        )
