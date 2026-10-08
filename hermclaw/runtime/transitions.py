"""Apply state transitions to persisted jobs/steps and emit audit events (P05)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import JobStatus, Severity, StepStatus
from hermclaw.contracts.events import EventType
from hermclaw.events.store import append_event
from hermclaw.persistence.models import Job, Step
from hermclaw.runtime.state_machines import JOB_TERMINAL, check_job, check_step


def _now() -> datetime:
    return datetime.now(UTC)


async def transition_job(
    session: AsyncSession,
    job: Job,
    to: JobStatus | str,
    *,
    reason: str = "",
    actor: str = "runtime",
    error_code: str | None = None,
    error_message: str | None = None,
    status_line: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    src = job.status
    if str(src) == str(to):
        return
    dst = check_job(src, to)
    job.status = dst.value
    job.row_version += 1
    if dst not in JOB_TERMINAL and job.started_at is None and dst != JobStatus.queued:
        job.started_at = _now()
    if dst in JOB_TERMINAL:
        job.finished_at = _now()
    if dst == JobStatus.queued:  # retry
        job.finished_at = None
        job.error_code = None
        job.error_message = None
    if error_code:
        job.error_code = error_code
    if error_message:
        job.error_message = error_message
    payload = {"from": str(src), "to": dst.value, "reason": reason, "actor": actor, **(extra or {})}
    severity = Severity.error if dst == JobStatus.failed else Severity.info
    await append_event(
        session, EventType.JOB_TRANSITION, source_type="runtime", source_id=actor, job_id=job.id, severity=severity, payload=payload
    )
    terminal_event = {
        JobStatus.succeeded: EventType.JOB_SUCCEEDED,
        JobStatus.failed: EventType.JOB_FAILED,
        JobStatus.cancelled: EventType.JOB_CANCELLED,
    }.get(dst)
    if terminal_event:
        await append_event(
            session,
            terminal_event,
            source_type="runtime",
            source_id=actor,
            job_id=job.id,
            severity=severity,
            payload={"reason": reason, "error_code": job.error_code},
        )
    if status_line:
        await emit_status(session, job.id, status_line, step_id=None)


async def transition_step(
    session: AsyncSession,
    step: Step,
    to: StepStatus | str,
    *,
    reason: str = "",
    actor: str = "runtime",
    error_code: str | None = None,
    error_message: str | None = None,
    status_line: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    src = step.status
    if str(src) == str(to):
        return
    dst = check_step(src, to)
    step.status = dst.value
    step.row_version += 1
    if dst == StepStatus.running and step.started_at is None:
        step.started_at = _now()
    if dst in (StepStatus.completed, StepStatus.failed, StepStatus.cancelled):
        step.finished_at = _now()
    if dst == StepStatus.ready:
        step.finished_at = None
        step.lease_owner = None
        step.lease_expires_at = None
    if error_code:
        step.error_code = error_code
    if error_message:
        step.error_message = error_message
    severity = Severity.warning if dst in (StepStatus.failed, StepStatus.blocked) else Severity.info
    await append_event(
        session,
        EventType.STEP_TRANSITION,
        source_type="runtime",
        source_id=actor,
        job_id=step.job_id,
        step_id=step.id,
        severity=severity,
        payload={"from": str(src), "to": dst.value, "step_key": step.step_key, "reason": reason, **(extra or {})},
    )
    if status_line:
        await emit_status(session, step.job_id, status_line, step_id=step.id)


async def emit_status(
    session: AsyncSession, job_id: Any, text_line: str, *, step_id: Any = None, severity: Severity = Severity.info
) -> None:
    """Human-readable live status line for the UI (no chain-of-thought, Bauplan §33)."""
    await append_event(
        session,
        EventType.STATUS,
        source_type="runtime",
        job_id=job_id,
        step_id=step_id,
        severity=severity,
        payload={"text": text_line[:500]},
    )
