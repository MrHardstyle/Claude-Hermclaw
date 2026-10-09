"""Persistence of verification runs: ``verification_runs`` + ``verification_checks`` (+ ``command_runs`` /
``test_runs`` for every command the verifier executed) and the VERIFIER_* events."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.verification import VerificationReport
from hermclaw.events.store import append_event
from hermclaw.persistence.models import CommandRun, TestRun, VerificationCheckRow, VerificationRun
from hermclaw.verifier.commands import CommandRecord, TestRecord
from hermclaw.verifier.text import pg_safe
from hermclaw.verifier.types import VerificationStep

SOURCE_TYPE = "verifier"
MAX_FAILED_EVENTS = 50


async def start_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    step_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    step: VerificationStep,
) -> uuid.UUID:
    async with sessionmaker() as session:
        run = VerificationRun(job_id=job_id, step_id=step_id, attempt_id=attempt_id, passed=False, status="running", changed_files=[])
        session.add(run)
        await session.flush()
        await append_event(
            session,
            EventType.VERIFIER_STARTED,
            source_type=SOURCE_TYPE,
            source_id=str(run.id),
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            payload={
                "verification_run_id": str(run.id),
                "step_key": step.key,
                "step_kind": step.kind,
                "acceptance_count": len(step.acceptance),
                "scope_version": step.scope.version if step.scope is not None else None,
            },
        )
        await session.commit()
        return run.id


def _counts(report: VerificationReport) -> dict[str, int]:
    out = {"pass": 0, "fail": 0, "skip": 0, "error": 0}
    for c in report.checks:
        out[c.status] += 1
    return out


async def finish_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    job_id: uuid.UUID,
    step_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    report: VerificationReport,
    status: str,
    commands: Sequence[CommandRecord],
    tests: Sequence[TestRecord],
    duration_ms: int,
) -> None:
    async with sessionmaker() as session:
        run = await session.get(VerificationRun, run_id, with_for_update=True)
        if run is None:
            raise LookupError(f"verification run {run_id} vanished")
        for check in report.checks:
            session.add(
                VerificationCheckRow(
                    verification_run_id=run_id,
                    check_type=check.check_type,
                    name=check.name,
                    status=check.status,
                    blocking=check.blocking,
                    message=check.message,
                    evidence=check.evidence,
                )
            )
        for cmd in commands:
            session.add(
                CommandRun(
                    job_id=job_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    target="sandbox",
                    command=cmd.command,
                    classification=f"verifier:{cmd.purpose}"[:32],
                    cwd=".",
                    exit_code=cmd.exit_code,
                    timed_out=cmd.timed_out,
                    network=cmd.network,
                    stdout_excerpt=cmd.stdout_excerpt,
                    stderr_excerpt=cmd.stderr_excerpt,
                    duration_ms=cmd.duration_ms,
                )
            )
        for t in tests:
            session.add(
                TestRun(
                    job_id=job_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    command=t.command,
                    framework=t.framework[:32],
                    status=t.status,
                    passed=t.passed,
                    failed=t.failed,
                    errors=t.errors,
                    skipped=t.skipped,
                    output_excerpt=t.output_excerpt,
                    duration_ms=t.duration_ms,
                )
            )
        run.passed = report.passed
        run.status = status
        run.summary = pg_safe(report.summary)
        run.changed_files = [pg_safe(p) for p in report.changed_files]
        run.finished_at = datetime.now(UTC)
        failing = [c for c in report.checks if c.status in ("fail", "error")]
        for check in failing[:MAX_FAILED_EVENTS]:
            payload: dict[str, Any] = {
                "verification_run_id": str(run_id),
                "check_type": check.check_type,
                "name": check.name,
                "status": check.status,
                "blocking": check.blocking,
                "message": check.message[:1000],
            }
            if isinstance(check.evidence.get("path"), str):
                payload["path"] = check.evidence["path"]
            await append_event(
                session,
                EventType.VERIFIER_CHECK_FAILED,
                source_type=SOURCE_TYPE,
                source_id=str(run_id),
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt_id,
                severity=Severity.warning if check.blocking else Severity.info,
                payload=payload,
            )
        await append_event(
            session,
            EventType.VERIFIER_FINISHED,
            source_type=SOURCE_TYPE,
            source_id=str(run_id),
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            severity=Severity.info if report.passed else (Severity.error if status == "error" else Severity.warning),
            duration_ms=duration_ms,
            payload={
                "verification_run_id": str(run_id),
                "passed": report.passed,
                "status": status,
                "summary": report.summary[:2000],
                "counts": _counts(report),
                "checks": len(report.checks),
                "failures": [f"{c.check_type}:{c.name}" for c in report.failures][:20],
                "changed_files": len(report.changed_files),
                "failed_events_truncated": max(0, len(failing) - MAX_FAILED_EVENTS),
            },
        )
        await session.commit()


async def abort_run(sessionmaker: async_sessionmaker[AsyncSession], *, run_id: uuid.UUID, reason: str) -> None:
    """Best effort: mark a run that could not be finished as ``error`` (never ``passed``)."""
    async with sessionmaker() as session:
        run = await session.get(VerificationRun, run_id, with_for_update=True)
        if run is None or run.status != "running":
            return
        run.passed = False
        run.status = "error"
        run.summary = reason[:2000]
        run.finished_at = datetime.now(UTC)
        await append_event(
            session,
            EventType.VERIFIER_FINISHED,
            source_type=SOURCE_TYPE,
            source_id=str(run_id),
            job_id=run.job_id,
            step_id=run.step_id,
            attempt_id=run.attempt_id,
            severity=Severity.error,
            payload={"verification_run_id": str(run_id), "passed": False, "status": "error", "summary": reason[:2000]},
        )
        await session.commit()
