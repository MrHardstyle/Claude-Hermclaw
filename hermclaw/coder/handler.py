"""Implement step handler: scope → coder loop → scope audit → deterministic verifier → heavy review → runtime commit,
with the bounded correction pipeline (Bauplan §19-§22, Phase 19 + 23).

Correction (P23): a failed verification or a ``fix_required`` review produces a *correction attempt* of the same step
(``step_attempts.kind = correction``) whose input carries the real verifier/review evidence (23.4). Counters live in
PostgreSQL (``steps.correction_count``, ``steps.attempt_count``) and are never lost (23.3). Every attempt is verified
completely again (23.5 regression rerun). When the correction budget is exhausted the step is blocked with
``repeated_verifier_failure`` and the scheduler escalates to the Gemma replanner (23.6).
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.coder.loop import CoderLoop, CoderResult, CoderSettings, StagnationHook, history_from_checkpoint
from hermclaw.context_builder import ContextBuilder, ContextBuilderConfig, CorrectionItem, StepBrief
from hermclaw.contracts.common import StepKind
from hermclaw.contracts.events import EventType
from hermclaw.contracts.review import ReviewContract, enforce_review_invariant
from hermclaw.contracts.scope import ScopeContract, ScopeExpansionRequest
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.verification import VerificationReport
from hermclaw.core.config import HermclawConfig
from hermclaw.core.errors import ConfigError, GitError, HermclawError, ResourceUnavailable
from hermclaw.core.interfaces import CommandExecutor, RepoContextProvider, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.models.protocols import ChatModel
from hermclaw.persistence.models import Step, StepAttempt
from hermclaw.runtime.transitions import emit_status
from hermclaw.scheduler.handlers import StepOutcome, StepRunContext
from hermclaw.tools.context import ScopeExpansionOutcome, ToolPermissions
from hermclaw.tools.engine import ToolEngine

if TYPE_CHECKING:
    from hermclaw.gitops.engine import GitEngine
    from hermclaw.scope.audit import ScopeAuditor
    from hermclaw.scope.engine import ScopeEngine
    from hermclaw.scope.expansion import ScopeExpansionHandler

log = get_logger(__name__)

CODE_STEP_KINDS = frozenset({StepKind.implement.value, StepKind.documentation.value})
_BLOCK_REASONS = {
    "scope_unavailable": "scope_unavailable",
    "missing_dependency": "missing_dependency",
    "test_conflict": "test_architecture_conflict",
    "requirement_unclear": "step_failed",
    "external_failure": "worker_unavailable",
    "other": "step_failed",
}


# ----------------------------------------------------------------------------- ports (adapters wrap P21/P22/P12)
@dataclass
class VerificationOutcome:
    report: VerificationReport
    run_id: uuid.UUID  # verification_runs.id – required by GitEngine.commit_verified


class VerifierPort(Protocol):
    async def verify(
        self, step: StepContract, workspace: WorkspaceHandle, *, job_id: uuid.UUID, step_id: uuid.UUID, attempt_id: uuid.UUID
    ) -> VerificationOutcome: ...


class ReviewerPort(Protocol):
    def should_review(self, step_kind: str, verifier_passed: bool) -> bool: ...

    async def review(
        self,
        step: StepContract,
        workspace: WorkspaceHandle,
        verification: VerificationReport,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> ReviewContract: ...


class ResearchPort(Protocol):
    async def ask(self, question: str, *, job_id: uuid.UUID, step_id: uuid.UUID) -> str: ...


class ModelLeaser(Protocol):
    """The part of :class:`hermclaw.resources.manager.ResourceManager` the handler uses."""

    def hold_model(self, profile: Any, **kw: Any) -> contextlib.AbstractAsyncContextManager[Any]: ...


ExecutorFactory = Callable[[WorkspaceHandle, StepContract], Awaitable[CommandExecutor]]
StagnationFactory = Callable[[StepRunContext], Awaitable[StagnationHook]]


@dataclass
class ImplementDeps:
    git: GitEngine
    scope_engine: ScopeEngine
    scope_auditor: ScopeAuditor
    expansion: ScopeExpansionHandler
    chat: ChatModel
    repo: RepoContextProvider
    executor_factory: ExecutorFactory
    verifier: VerifierPort
    reviewer: ReviewerPort | None = None
    research: ResearchPort | None = None
    stagnation_factory: StagnationFactory | None = None
    coder_settings: CoderSettings = field(default_factory=CoderSettings)
    resources: ModelLeaser | None = None  # P09: GPU model leases (coder during the loop, heavy during review)
    lease_wait_seconds: float = 3600.0
    lease_ttl_seconds: float = 600.0
    lease_keeper_interval: float | None = None  # None: resource manager default (heartbeat-derived)


# ----------------------------------------------------------------------------- helpers
def step_contract(row: Step, scope: ScopeContract | None = None) -> StepContract:
    return StepContract.model_validate(
        {
            "id": row.id,
            "job_id": row.job_id,
            "step_key": row.step_key,
            "title": row.title,
            "kind": row.kind,
            "capability": row.capability,
            "goal": row.goal,
            "status": row.status,
            "risk": row.risk,
            "constraints": list(row.constraints or []),
            "acceptance": list(row.acceptance or []),
            "repo_hints": list(row.repo_hints or []),
            "scope": scope,
            "turn_budget": row.turn_budget,
            "network": row.network,
        }
    )


def _item_dict(i: CorrectionItem) -> dict[str, str]:
    return {"source": i.source, "label": i.label, "message": i.message, "path": i.path, "suggested_fix": i.suggested_fix}


def corrections_from_input(data: dict[str, Any]) -> list[CorrectionItem]:
    out: list[CorrectionItem] = []
    for d in data.get("items") or []:
        if isinstance(d, dict) and d.get("message"):
            out.append(
                CorrectionItem(
                    source=str(d.get("source", "verifier")),
                    label=str(d.get("label", "")),
                    message=str(d["message"]),
                    path=str(d.get("path", "")),
                    suggested_fix=str(d.get("suggested_fix", "")),
                )
            )
    return out


@dataclass
class _Run:
    """Per-attempt state passed between the pipeline stages."""

    ctx: StepRunContext
    row: Step
    step: StepContract
    ws: Any
    handle: WorkspaceHandle


class _Callbacks:
    def __init__(self, deps: ImplementDeps, ctx: StepRunContext, workspace: WorkspaceHandle) -> None:
        self.deps, self.ctx, self.workspace = deps, ctx, workspace

    async def on_research(self, question: str) -> str:
        if self.deps.research is None:
            return "research is not available for this step; continue with repository evidence or block_step"
        return await self.deps.research.ask(question, job_id=self.ctx.job_id, step_id=self.ctx.step_id)

    async def on_scope_expansion(self, request: ScopeExpansionRequest) -> ScopeExpansionOutcome:
        decision = await self.deps.expansion.handle(self.ctx.step_id, request, self.workspace, attempt_id=self.ctx.attempt_id)
        return ScopeExpansionOutcome(granted=decision.granted, message=decision.reason, contract=decision.contract, data=decision.to_dict())

    async def on_replan(self, reason: str) -> None:
        return None  # request_replan is terminal; the handler turns it into a blocked outcome


# ----------------------------------------------------------------------------- handler
class ImplementStepHandler:
    kinds = CODE_STEP_KINDS

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], config: HermclawConfig, deps: ImplementDeps) -> None:
        self.sm = sessionmaker
        self.config = config
        self.deps = deps

    async def _load(self, step_id: uuid.UUID) -> Step:
        async with self.sm() as s:
            return (await s.execute(select(Step).where(Step.id == step_id))).scalar_one()

    async def _workspace(self, job_id: uuid.UUID) -> tuple[Any, WorkspaceHandle] | None:
        for ws in await self.deps.git.list_workspaces(job_id):
            if ws.status not in ("archived", "cleaned", "removed", "failed"):
                return ws, await self.deps.git.handle(ws)
        return None

    async def _status(self, ctx: StepRunContext, text: str) -> None:
        async with self.sm() as s:
            await emit_status(s, ctx.job_id, text, step_id=ctx.step_id)
            await s.commit()

    async def _correction(  # noqa: PLR0917 - private, called with the evidence tuple
        self, ctx: StepRunContext, row: Step, source: str, items: list[CorrectionItem], code: str, detail: str
    ) -> StepOutcome:
        """Schedule a bounded correction attempt carrying the evidence, or escalate to the replanner."""
        limit = self.config.policies.correction.max_corrections_per_step
        evidence = {"source": source, "items": [_item_dict(i) for i in items[:30]], "detail": DEFAULT_REDACTOR.text(detail)[:2000]}
        if row.correction_count >= limit or row.attempt_count >= row.max_attempts:
            return StepOutcome(
                "blocked",
                summary=f"correction budget exhausted ({row.correction_count}/{limit})",
                error_code=code,
                error_message=DEFAULT_REDACTOR.text(detail)[:2000],
                replan_reason="repeated_verifier_failure",
                replan_evidence=evidence,
            )
        async with self.sm() as s:
            await s.execute(
                update(StepAttempt).where(StepAttempt.id == ctx.attempt_id).values(correction_input={"pending": True, **evidence})
            )
            await s.execute(update(Step).where(Step.id == row.id).values(correction_count=Step.correction_count + 1))
            await append_event(
                s,
                EventType.CORRECTION_STARTED,
                source_type="coder",
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                attempt_id=ctx.attempt_id,
                payload={"source": source, "items": len(items), "correction": row.correction_count + 1, "limit": limit},
            )
            await s.commit()
        return StepOutcome(
            "failed",
            summary=f"{source} requires correction",
            error_code=code,
            error_message=detail[:2000],
            retryable=True,
            retry_delay_seconds=0,
        )

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        row = await self._load(ctx.step_id)
        found = await self._workspace(ctx.job_id)
        if found is None:
            return StepOutcome(
                "blocked", error_code="NO_WORKSPACE", error_message="job has no active workspace", replan_reason="missing_dependency"
            )
        ws, handle = found
        # step baseline: changes of *this* step are measured against the HEAD at step start (earlier steps of the
        # job are already committed), so scope checks, diffs and verification never attribute them to this step
        fresh = await self.deps.git.get_workspace(ws.id)
        if fresh.head_sha and fresh.head_sha != handle.base_sha:
            handle = dataclasses.replace(handle, base_sha=fresh.head_sha)
        # scope (P15): reuse the active version on retries/corrections, create it on the first attempt
        contract = await self.deps.scope_engine.current_contract(ctx.step_id)
        if contract is None:
            decision = await self.deps.scope_engine.create_scope(ctx.step_id, handle)
            if not decision.runnable:
                return StepOutcome(
                    "blocked",
                    error_code="SCOPE_UNAVAILABLE",
                    error_message=decision.reason,
                    replan_reason="scope_unavailable",
                    replan_evidence={"reason_code": decision.reason_code, **(decision.evidence or {})},
                )
            contract = decision.contract
        step = step_contract(row, contract)
        try:
            capability = self.config.capabilities.get(row.capability)
        except ConfigError:
            capability = None
        guard = await self.deps.scope_engine.guard_for(ctx.step_id)
        executor = await self.deps.executor_factory(handle, step)
        engine = ToolEngine(
            self.sm,
            self.config.policies,
            handle,
            guard,
            executor,
            self.deps.git.reader(),
            self.deps.repo,
            _Callbacks(self.deps, ctx, handle),
            permissions=ToolPermissions.for_step(kind=row.kind, network=row.network, turn_budget=row.turn_budget, capability=capability),
        )
        builder = ContextBuilder(self.deps.repo, self.deps.git.reader(), ContextBuilderConfig.from_config(self.config))
        settings = CoderSettings(
            **{**self.deps.coder_settings.__dict__, "max_turns": min(row.turn_budget, self.config.policies.coder.max_turns)}
        )
        stagnation = await self.deps.stagnation_factory(ctx) if self.deps.stagnation_factory else None
        loop = CoderLoop(self.deps.chat, builder, engine, settings=settings, stagnation=stagnation, sessionmaker=self.sm)
        start, history, last_failure = (
            history_from_checkpoint(ctx.checkpoint.get("coder") or {}) if ctx.checkpoint.get("coder") else (0, [], None)
        )
        corrections = corrections_from_input(ctx.correction_input) if ctx.attempt_kind == "correction" else []
        await self._status(ctx, f"Coder startet {row.step_key} ({ctx.attempt_kind}, max {settings.max_turns} Turns)")
        try:
            async with self._model(ctx, "coder", preemptible=True):
                result = await loop.run(
                    step=StepBrief.from_contract(step),
                    workspace=handle,
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    token=ctx.token,
                    history=history,
                    start_turn=start + 1,
                    last_failure=last_failure,
                    correction=corrections,
                )
        except ResourceUnavailable as exc:
            return StepOutcome("failed", error_code=exc.code, error_message=exc.message, retryable=True)
        try:
            return await self._after_loop(_Run(ctx, row, step, ws, handle), result)
        except ResourceUnavailable as exc:  # heavy-review lease not granted in time
            return StepOutcome("failed", error_code=exc.code, error_message=exc.message, retryable=True)

    def _model(self, ctx: StepRunContext, role: str, *, preemptible: bool) -> contextlib.AbstractAsyncContextManager[Any]:
        """Hold the GPU model lease for ``role``; a preemption request (video/image) makes the coder checkpoint."""
        if self.deps.resources is None:
            return contextlib.nullcontext()
        profile = self.config.models.by_role(role)

        def on_preempt(*_: Any) -> None:
            ctx.token.request_checkpoint(f"GPU needed by a higher-priority job ({role} lease preempted)")

        return self.deps.resources.hold_model(
            profile,
            on_preempt=on_preempt if preemptible else None,
            job_id=ctx.job_id,
            step_id=ctx.step_id,
            ttl_seconds=self.deps.lease_ttl_seconds,
            wait_timeout=self.deps.lease_wait_seconds,
            preemptible=preemptible,
            keeper_interval=self.deps.lease_keeper_interval,
        )

    async def _after_loop(self, run: _Run, result: CoderResult) -> StepOutcome:
        ctx, row = run.ctx, run.row
        if result.outcome == "cancelled":
            return StepOutcome("cancelled", summary=result.detail or "cancelled")
        if result.outcome == "checkpointed":
            return StepOutcome("checkpointed", summary="paused", checkpoint={"coder": result.checkpoint()})
        if result.outcome == "model_failed":
            return StepOutcome("failed", error_code=result.error_code or "MODEL_FAILED", error_message=result.detail, retryable=True)
        if result.outcome == "blocked":
            block = result.block or {}
            reason = _BLOCK_REASONS.get(str(block.get("reason_code", "other")), "step_failed")
            return StepOutcome(
                "blocked",
                error_code="CODER_BLOCKED",
                error_message=str(block.get("message", ""))[:2000],
                replan_reason=reason,
                replan_evidence={"block": block},
            )
        if result.outcome == "replan":
            return StepOutcome(
                "blocked",
                error_code="CODER_REQUESTED_REPLAN",
                error_message=result.replan_reason,
                replan_reason="step_failed",
                replan_evidence={"coder_reason": result.replan_reason},
            )
        if result.outcome == "stagnated":
            items = [
                CorrectionItem(
                    source="stagnation", label="stagnation", message=f"stagnation detected: {result.detail}. Change the approach."
                )
            ]
            if result.recommendation == "heavy_review" and self.deps.reviewer is not None:
                return await self._review_and_correct(run, VerificationReport(passed=False, checks=[], summary="stagnation"), items)
            if result.recommendation == "research" and self.deps.research is not None:
                question = (
                    f"Step '{row.title}' ({row.kind}) is stuck: {result.detail}. "
                    f"Last failure: {(result.last_failure or 'n/a')[:600]}. Which documented approach or API usage solves this?"
                )
                summary = await self.deps.research.ask(DEFAULT_REDACTOR.text(question), job_id=ctx.job_id, step_id=ctx.step_id)
                items.append(CorrectionItem(source="research", label="research result", message=summary[:3000]))
                return await self._correction(ctx, row, "stagnation", items, "STAGNATION", "stagnation escalated to research")
            return StepOutcome(
                "blocked",
                error_code="STAGNATION",
                error_message=result.detail,
                replan_reason="stagnation",
                replan_evidence={"recommendation": result.recommendation, "history": [r.tool for r in result.history][-10:]},
            )
        if result.outcome == "budget_exhausted":
            items = [
                CorrectionItem(
                    source="runtime",
                    label="turn budget",
                    message="the previous attempt used all turns without complete_step; finish faster",
                )
            ]
            if result.last_failure:
                items.append(CorrectionItem(source="runtime", label="last failure", message=result.last_failure[:1500]))
            return await self._correction(ctx, row, "budget", items, "TURN_BUDGET_EXHAUSTED", result.detail)
        return await self._verify_review_commit(run, result)

    async def _verify_review_commit(self, run: _Run, result: CoderResult) -> StepOutcome:
        ctx, row, step, handle = run.ctx, run.row, run.step, run.handle
        audit = await self.deps.scope_auditor.audit_status(
            ctx.step_id, await self.deps.git.reader().status(handle), attempt_id=ctx.attempt_id
        )
        if not audit.ok:
            items = [
                CorrectionItem(source="scope", label="scope violation", message=str(v.get("reason") or v), path=str(v.get("path", "")))
                for v in audit.violations
            ]
            return await self._correction(ctx, row, "scope", items, "SCOPE_VIOLATION", f"{len(audit.violations)} out-of-scope change(s)")
        await self._status(ctx, f"Verifier prüft {row.step_key}")
        outcome = await self.deps.verifier.verify(step, handle, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id)
        if not outcome.report.passed:
            items = CorrectionItem.from_verification(outcome.report)
            return await self._correction(
                ctx, row, "verifier", list(items), "VERIFIER_FAILED", outcome.report.summary or "verification failed"
            )
        if self.deps.reviewer is not None and self.deps.reviewer.should_review(row.kind, True):
            await self._status(ctx, f"Heavy Review für {row.step_key}")
            async with self._model(ctx, "heavy", preemptible=False):
                raw_review = await self.deps.reviewer.review(
                    step, handle, outcome.report, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id
                )
            review, _ = enforce_review_invariant(raw_review)
            if review.verdict != "pass":
                return await self._correction(
                    ctx,
                    row,
                    "review",
                    list(CorrectionItem.from_review(review)),
                    "REVIEW_FIX_REQUIRED",
                    review.summary or "review requires fixes",
                )
        return await self._commit(run, outcome, result)

    async def _review_and_correct(self, run: _Run, report: VerificationReport, items: list[CorrectionItem]) -> StepOutcome:
        ctx, row, step, handle = run.ctx, run.row, run.step, run.handle
        assert self.deps.reviewer is not None
        async with self._model(ctx, "heavy", preemptible=False):
            raw_review = await self.deps.reviewer.review(
                step, handle, report, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id
            )
        review, _ = enforce_review_invariant(raw_review)
        return await self._correction(
            ctx, row, "review", items + list(CorrectionItem.from_review(review)), "STAGNATION", "stagnation escalated to heavy review"
        )

    async def _commit(self, run: _Run, outcome: VerificationOutcome, result: CoderResult) -> StepOutcome:
        ctx, row, step, ws = run.ctx, run.row, run.step, run.ws
        summary = str(((result.completion or {}).get("report") or {}).get("summary") or "step completed")
        try:
            staged = await self.deps.git.stage_allowed(ws, step.scope or ScopeContract(), step_id=ctx.step_id)
            if staged.refused:
                items = [
                    CorrectionItem(source="scope", label="staging refused", message=str(r), path=getattr(r, "path", ""))
                    for r in staged.refused
                ]
                return await self._correction(
                    ctx, row, "scope", items, "STAGING_REFUSED", f"{len(staged.refused)} path(s) refused at staging"
                )
            if not staged.staged:
                return StepOutcome(
                    "completed", summary=summary, result={"commit": None, "verification_run_id": str(outcome.run_id), "note": "no changes"}
                )
            commit = await self.deps.git.commit_verified(
                ws, f"{row.step_key}: {row.title}"[:200], outcome.run_id, step_id=ctx.step_id, scope=step.scope
            )
        except GitError as exc:
            return StepOutcome("failed", error_code=exc.code, error_message=DEFAULT_REDACTOR.text(exc.message), retryable=False)
        except HermclawError as exc:  # pragma: no cover - defensive
            return StepOutcome("failed", error_code=exc.code, error_message=DEFAULT_REDACTOR.text(exc.message), retryable=True)
        return StepOutcome(
            "completed",
            summary=summary,
            result={"commit": commit.sha, "files": commit.files, "verification_run_id": str(outcome.run_id), "turns": result.turns},
        )
