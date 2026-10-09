"""Persistence helpers for stagnation detection (P20).

The detector state lives in ``step_attempts.fingerprints["stagnation"]`` (JSONB) so that a restarted runtime resumes
an attempt with its counters intact. Writes merge into the JSON object (other keys of ``fingerprints`` are kept) and
are guarded by the state's ``last_turn`` so a stale writer can never roll the state back. Events
(``stagnation.detected``, ``strategy.changed``) are appended in the caller's transaction via
:func:`hermclaw.events.store.append_event`, which redacts payloads.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Integer, case, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.config import StagnationPolicy
from hermclaw.events.store import append_event
from hermclaw.persistence.models import Event, StepAttempt
from hermclaw.stagnation.actions import DirectiveKind, EscalationDecision, Recommendation
from hermclaw.stagnation.detector import DetectorTuning, StagnationDetector, StagnationLevel, StagnationVerdict

STATE_KEY = "stagnation"
SOURCE_TYPE = "stagnation"
_SEVERITY = {
    StagnationLevel.warning: Severity.info,
    StagnationLevel.diagnose: Severity.warning,
    StagnationLevel.stop: Severity.error,
}
_RECOMMENDATIONS = frozenset(r.value for r in Recommendation)


async def load_state(session: AsyncSession, attempt_id: uuid.UUID) -> dict[str, Any] | None:
    """The persisted detector state of an attempt (``None`` if the attempt or the state does not exist)."""
    res = await session.execute(select(StepAttempt.fingerprints).where(StepAttempt.id == attempt_id))
    fingerprints = res.scalar_one_or_none()
    if not isinstance(fingerprints, Mapping):
        return None
    state = fingerprints.get(STATE_KEY)
    return dict(state) if isinstance(state, Mapping) else None


async def save_state(session: AsyncSession, attempt_id: uuid.UUID, detector: StagnationDetector) -> bool:
    """Merge the detector state into ``step_attempts.fingerprints`` (caller commits).

    Returns ``False`` when nothing was written: the attempt does not exist, or the stored state is newer (a higher
    ``last_turn``) than this detector's – a stale writer never overwrites newer state.
    """
    state = detector.to_state()
    stored = StepAttempt.fingerprints[STATE_KEY]["last_turn"]
    # a corrupt stored value (not a number) must not block saving a fresh state forever
    stored_turn = case((func.jsonb_typeof(stored) == "number", cast(stored.astext, Integer)), else_=-1)
    base = case((func.jsonb_typeof(StepAttempt.fingerprints) == "object", StepAttempt.fingerprints), else_=literal({}, type_=JSONB))
    merged = base.op("||", return_type=JSONB)(literal({STATE_KEY: state}, type_=JSONB))
    stmt = (
        update(StepAttempt)
        .where(StepAttempt.id == attempt_id)
        .where(stored_turn <= int(state["last_turn"]))
        .values(fingerprints=merged)
        .execution_options(synchronize_session=False)
    )
    res = await session.execute(stmt)
    return bool(getattr(res, "rowcount", 0))


async def load_detector(
    session: AsyncSession,
    attempt_id: uuid.UUID,
    policy: StagnationPolicy | None = None,
    *,
    tuning: DetectorTuning | None = None,
    initial_diff: str | None = None,
) -> StagnationDetector:
    """Restore the attempt's detector, or a fresh one if nothing (valid) is stored."""
    return StagnationDetector.from_state(await load_state(session, attempt_id), policy, tuning=tuning, initial_diff=initial_diff)


async def prior_escalations(session: AsyncSession, step_id: uuid.UUID, *, exclude_attempt_id: uuid.UUID | None = None) -> tuple[str, ...]:
    """Recommendations already issued by stagnation stops in earlier attempts of the step, oldest first."""
    q = select(StepAttempt.id, StepAttempt.fingerprints).where(StepAttempt.step_id == step_id).order_by(StepAttempt.attempt_no)
    out: list[str] = []
    for attempt_id, fingerprints in (await session.execute(q)).all():
        if attempt_id == exclude_attempt_id or not isinstance(fingerprints, Mapping):
            continue
        state = fingerprints.get(STATE_KEY)
        if not isinstance(state, Mapping):
            continue
        for rec in state.get("escalations") or []:
            if isinstance(rec, str) and rec in _RECOMMENDATIONS:
                out.append(rec)
    return tuple(out)


def apply_decision(detector: StagnationDetector, decision: EscalationDecision) -> str | None:
    """Record a decision's strategy/recommendation in the detector state; returns the previous strategy label."""
    previous = detector.strategies[-1] if detector.strategies else None
    if decision.strategy:
        detector.strategies.append(decision.strategy)
        del detector.strategies[: max(0, len(detector.strategies) - 20)]
    if decision.kind is DirectiveKind.stop and decision.recommendation is not None:
        detector.escalations.append(decision.recommendation.value)
    return previous


def _signals_payload(verdict: StagnationVerdict) -> list[dict[str, Any]]:
    return [
        {
            "kind": s.kind.value,
            "fingerprint": s.key,
            "count": s.count,
            "level": s.level.value,
            "label": s.label,
            "tool": s.tool,
            "error_code": s.error_code,
            "after_code_change": s.after_code_change,
            "tests": list(s.tests[:10]),
        }
        for s in verdict.repeated_signals[:8]
    ]


async def record_events(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    step_id: uuid.UUID,
    attempt_id: uuid.UUID,
    verdict: StagnationVerdict,
    decision: EscalationDecision,
    previous_strategy: str | None = None,
    source_id: str | None = None,
) -> list[Event]:
    """Emit ``stagnation.detected`` (any non-none level) and ``strategy.changed`` (diagnose/stop) events."""
    events: list[Event] = []
    if verdict.level is StagnationLevel.none:
        return events
    common: dict[str, Any] = {
        "turn": verdict.turn,
        "level": verdict.level.value,
        "cause": decision.cause.value if decision.cause else None,
    }
    events.append(
        await append_event(
            session,
            EventType.STAGNATION_DETECTED,
            source_type=SOURCE_TYPE,
            source_id=source_id,
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            severity=_SEVERITY.get(verdict.level, Severity.info),
            payload={
                **common,
                "reasons": list(verdict.reasons[:8]),
                "signals": _signals_payload(verdict),
                "directive": decision.kind.value,
                "recommendation": decision.recommendation.value if decision.recommendation else None,
                "diagnoses_issued": verdict.diagnoses_issued,
            },
        )
    )
    if decision.strategy and (decision.kind is DirectiveKind.stop or decision.strategy != previous_strategy):
        events.append(
            await append_event(
                session,
                EventType.STRATEGY_CHANGED,
                source_type=SOURCE_TYPE,
                source_id=source_id,
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt_id,
                severity=Severity.warning if decision.kind is DirectiveKind.stop else Severity.info,
                payload={
                    **common,
                    "from": previous_strategy or "default",
                    "to": decision.strategy,
                    "recommendation": decision.recommendation.value if decision.recommendation else None,
                    "reason": verdict.reasons[0] if verdict.reasons else "",
                },
            )
        )
    return events
