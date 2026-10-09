"""Coder-loop integration: :class:`StagnationMonitor` implements ``hermclaw.coder.loop.StagnationHook``.

Per turn the monitor (1) optionally fetches the full workspace diff after a mutating turn (precise diff progress,
20.3), (2) feeds the observation into the :class:`StagnationDetector`, (3) runs the escalation ladder, (4) persists
the detector state and emits ``stagnation.detected``/``strategy.changed`` in one transaction and (5) returns the
directive the coder loop understands (warning/diagnose message for the next turn, or stop + recommendation).

Persistence or diff failures never break the coder loop: detection keeps working in memory and the failure is logged.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.coder.loop import StagnationDirective, TurnObservation
from hermclaw.core.config import StagnationPolicy
from hermclaw.core.interfaces import GitReader, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.persistence.models import StepAttempt
from hermclaw.stagnation.actions import DirectiveKind, EscalationDecision, LadderContext, decide
from hermclaw.stagnation.detector import DetectorTuning, Observation, StagnationDetector, StagnationVerdict, is_mutating
from hermclaw.stagnation.persistence import apply_decision, load_state, prior_escalations, record_events, save_state

if TYPE_CHECKING:
    from hermclaw.scheduler.handlers import StepRunContext

log = get_logger(__name__)

DiffProvider = Callable[[], Awaitable[str]]
WorkspaceResolver = Callable[["StepRunContext"], Awaitable[WorkspaceHandle | None]]
DIFF_MAX_BYTES = 1_000_000


def to_directive(decision: EscalationDecision) -> StagnationDirective:
    """Map a ladder decision onto the coder loop's directive."""
    if decision.kind is DirectiveKind.notice:
        return StagnationDirective(level="warning", message=decision.message, reasons=decision.reasons)
    if decision.kind is DirectiveKind.diagnose:
        return StagnationDirective(level="diagnose", message=decision.message, reasons=decision.reasons)
    if decision.kind is DirectiveKind.stop:
        rec = decision.recommendation.value if decision.recommendation else "replan"
        return StagnationDirective(level="stop", message=decision.message, recommendation=rec, reasons=decision.reasons)
    return StagnationDirective()


class StagnationMonitor:
    """Stateful per-attempt stagnation hook for :class:`hermclaw.coder.loop.CoderLoop`."""

    def __init__(
        self,
        detector: StagnationDetector,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        ladder: LadderContext | None = None,
        diff_provider: DiffProvider | None = None,
    ) -> None:
        self.detector = detector
        self.job_id = job_id
        self.step_id = step_id
        self.attempt_id = attempt_id
        self.sm = sessionmaker
        self.ladder = ladder or LadderContext()
        self.diff_provider = diff_provider
        self.last_verdict: StagnationVerdict | None = None
        self.last_decision: EscalationDecision | None = None
        self.persist_failures = 0
        self._stop_directive: StagnationDirective | None = None
        self._lock = asyncio.Lock()

    async def observe(self, obs: TurnObservation) -> StagnationDirective:
        async with self._lock:
            if self._stop_directive is not None:
                return self._stop_directive
            if self.detector.stopped:
                # restored after a stop: repeat the recorded outcome, never record a second escalation
                self._stop_directive = self._restored_stop()
                return self._stop_directive
            diff = await self._diff_after(obs)
            before = self.detector.last_turn
            verdict = self.detector.observe(Observation.from_tool(obs.turn, obs.action, obs.result, diff_text=diff))
            if self.detector.last_turn == before and not verdict.stagnating:
                return StagnationDirective()  # replayed turn: nothing changed, nothing to persist
            ladder = self.ladder
            if verdict.research_used and not ladder.research_used_in_attempt:
                ladder = replace(ladder, research_used_in_attempt=True)
            decision = decide(verdict, ladder)
            previous = apply_decision(self.detector, decision) if decision.kind is not DirectiveKind.none else None
            self.last_verdict, self.last_decision = verdict, decision
            await self._persist(verdict, decision, previous)
            directive = to_directive(decision)
            if decision.kind is DirectiveKind.stop:
                self._stop_directive = directive
            return directive

    def _restored_stop(self) -> StagnationDirective:
        verdict = self.detector.stop_verdict
        reasons = verdict.reasons if verdict else ("stagnation stop already issued for this attempt",)
        rec = self.detector.escalations[-1] if self.detector.escalations else None
        if rec is None and verdict is not None:
            decided = decide(verdict, self.ladder).recommendation
            rec = decided.value if decided else None
        return StagnationDirective(level="stop", message="stagnation stop already issued", recommendation=rec or "replan", reasons=reasons)

    async def _diff_after(self, obs: TurnObservation) -> str | None:
        if self.diff_provider is None or not is_mutating(obs.action.tool.value, obs.result.mutated_paths):
            return None
        if not obs.result.ok and not obs.result.mutated_paths:
            return None  # a refused edit changed nothing
        try:
            return await self.diff_provider()
        except Exception as exc:  # the detector falls back to action-derived states
            log.warning("workspace diff unavailable for stagnation detection", extra={"error": type(exc).__name__})
            return None

    async def _persist(self, verdict: StagnationVerdict, decision: EscalationDecision, previous: str | None) -> None:
        if self.sm is None:
            return
        try:
            async with self.sm() as s:
                saved = await save_state(s, self.attempt_id, self.detector)
                if not saved:
                    log.warning(
                        "stagnation state not saved (attempt missing or newer state stored)", extra={"attempt_id": str(self.attempt_id)}
                    )
                await record_events(
                    s,
                    job_id=self.job_id,
                    step_id=self.step_id,
                    attempt_id=self.attempt_id,
                    verdict=verdict,
                    decision=decision,
                    previous_strategy=previous,
                    source_id=str(self.attempt_id),
                )
                await s.commit()
        except Exception as exc:  # telemetry/persistence only – never break the coder loop
            self.persist_failures += 1
            log.warning("stagnation state/events not persisted", extra={"attempt_id": str(self.attempt_id), "error": type(exc).__name__})


async def create_monitor(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    step_id: uuid.UUID,
    attempt_id: uuid.UUID,
    policy: StagnationPolicy | None = None,
    tuning: DetectorTuning | None = None,
    research_available: bool = True,
    heavy_review_available: bool = True,
    diff_provider: DiffProvider | None = None,
    inherit_from_attempt_id: uuid.UUID | None = None,
) -> StagnationMonitor:
    """Build a monitor for an attempt, resuming its persisted state (or a resumed attempt's state, if not stopped)."""
    initial_diff: str | None = None
    if diff_provider is not None:
        try:
            initial_diff = await diff_provider()
        except Exception as exc:
            log.warning("initial workspace diff unavailable for stagnation detection", extra={"error": type(exc).__name__})
    async with sessionmaker() as s:
        state = await load_state(s, attempt_id)
        if state is None and inherit_from_attempt_id is not None:
            inherited = await load_state(s, inherit_from_attempt_id)
            if inherited is not None and not inherited.get("stop"):
                state = {**inherited, "escalations": [], "strategies": list(inherited.get("strategies") or [])}
        used = await prior_escalations(s, step_id, exclude_attempt_id=attempt_id)
    detector = StagnationDetector.from_state(state, policy, tuning=tuning, initial_diff=initial_diff)
    ladder = LadderContext(
        research_available=research_available,
        heavy_review_available=heavy_review_available,
        used=used,
        research_used_in_attempt=detector.research_used,
    )
    return StagnationMonitor(
        detector,
        job_id=job_id,
        step_id=step_id,
        attempt_id=attempt_id,
        sessionmaker=sessionmaker,
        ladder=ladder,
        diff_provider=diff_provider,
    )


async def previous_attempt_id(session: AsyncSession, step_id: uuid.UUID, attempt_no: int) -> uuid.UUID | None:
    q = (
        select(StepAttempt.id)
        .where(StepAttempt.step_id == step_id, StepAttempt.attempt_no < attempt_no)
        .order_by(StepAttempt.attempt_no.desc())
        .limit(1)
    )
    return (await session.execute(q)).scalar_one_or_none()


def make_stagnation_factory(
    *,
    policy: StagnationPolicy | None = None,
    tuning: DetectorTuning | None = None,
    git: GitReader | None = None,
    workspace_for: WorkspaceResolver | None = None,
    research_available: bool = True,
    heavy_review_available: bool = True,
) -> Callable[[StepRunContext], Awaitable[StagnationMonitor]]:
    """A ``StagnationFactory`` for :class:`hermclaw.coder.handler.ImplementDeps`.

    With ``git`` and ``workspace_for`` the monitor measures diff progress on the real workspace diff (incl. untracked
    files); otherwise it derives workspace states from the mutating actions. ``resume`` attempts inherit the previous
    attempt's counters; other attempts start fresh but know which escalations were already used for the step.
    """

    async def factory(ctx: StepRunContext) -> StagnationMonitor:
        diff_provider: DiffProvider | None = None
        if git is not None and workspace_for is not None:
            workspace = await workspace_for(ctx)
            if workspace is not None:
                ws = workspace

                async def provider() -> str:
                    return await git.diff(ws, None, max_bytes=DIFF_MAX_BYTES)

                diff_provider = provider
        inherit: uuid.UUID | None = None
        if ctx.attempt_kind == "resume":
            async with ctx.sessionmaker() as s:
                inherit = await previous_attempt_id(s, ctx.step_id, ctx.attempt_no)
        effective: StagnationPolicy = policy or ctx.config.policies.stagnation
        return await create_monitor(
            ctx.sessionmaker,
            job_id=ctx.job_id,
            step_id=ctx.step_id,
            attempt_id=ctx.attempt_id,
            policy=effective,
            tuning=tuning,
            research_available=research_available,
            heavy_review_available=heavy_review_available,
            diff_provider=diff_provider,
            inherit_from_attempt_id=inherit,
        )

    return factory
