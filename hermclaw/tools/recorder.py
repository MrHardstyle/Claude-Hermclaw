"""Persistence of tool calls, command runs, test runs, checkpoints and tool events (P17 17.13).

Every write goes through :class:`~hermclaw.core.redaction.Redactor` before it reaches the database: tool arguments,
result summaries, command lines and stdout/stderr excerpts. Long argument values (file contents, patches) are stored
as a bounded head plus length and SHA-256 digest, so the audit trail stays small but still identifies repetitions.
Each method uses its own short transaction; events are inserted in the same transaction as the row they describe.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.core.redaction import Redactor
from hermclaw.events.store import append_event
from hermclaw.persistence.models import CommandRun, Step, TestRun, ToolCallRow
from hermclaw.tools.context import CallContext
from hermclaw.tools.output import clip_head, excerpt

SOURCE_TYPE = "tool_engine"
ARG_VALUE_CHARS = 2000
SUMMARY_CHARS = 1000
EXCERPT_CHARS = 8000


@dataclass(frozen=True)
class PendingEvent:
    event_type: str
    payload: dict[str, Any]
    severity: Severity = Severity.info
    with_duration: bool = False  # only the event describing the whole operation carries its duration


def compact_arguments(redactor: Redactor, args: dict[str, Any]) -> dict[str, Any]:
    """Redacted copy of tool arguments with long strings replaced by head + length + digest."""

    def shrink(value: Any) -> Any:
        if isinstance(value, str) and len(value) > ARG_VALUE_CHARS:
            digest = hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()[:16]
            return f"{value[:ARG_VALUE_CHARS]}…[{len(value)} chars, sha256:{digest}]"
        if isinstance(value, dict):
            return {k: shrink(v) for k, v in value.items()}
        if isinstance(value, list):
            return [shrink(v) for v in value[:200]]
        return value

    redacted = redactor.obj(args)
    return shrink(redacted) if isinstance(redacted, dict) else {"value": shrink(redacted)}


class ToolRecorder:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], redactor: Redactor) -> None:
        self.sessionmaker = sessionmaker
        self.redactor = redactor

    async def _events(self, session: AsyncSession, ctx: CallContext, events: list[PendingEvent], *, duration_ms: int | None = None) -> None:
        for ev in events:
            await append_event(
                session,
                ev.event_type,
                source_type=SOURCE_TYPE,
                source_id=str(ctx.tool_call_id),
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                attempt_id=ctx.attempt_id,
                severity=ev.severity,
                payload=self.redactor.obj(ev.payload),
                duration_ms=duration_ms if ev.with_duration else None,
            )

    async def emit(self, ctx: CallContext, events: list[PendingEvent]) -> None:
        if not events:
            return
        async with self.sessionmaker() as session, session.begin():
            await self._events(session, ctx, events)

    async def start(self, ctx: CallContext, tool: str, args: dict[str, Any], event: PendingEvent) -> None:
        async with self.sessionmaker() as session, session.begin():
            session.add(
                ToolCallRow(
                    id=ctx.tool_call_id,
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    turn=ctx.turn,
                    tool=tool[:64],
                    arguments=compact_arguments(self.redactor, args),
                    status="running",
                    started_at=datetime.now(UTC),
                )
            )
            await self._events(session, ctx, [event])

    async def finish(
        self,
        ctx: CallContext,
        *,
        status: str,
        summary: str,
        error_code: str | None,
        duration_ms: int,
        events: list[PendingEvent],
    ) -> None:
        async with self.sessionmaker() as session, session.begin():
            row = await session.get(ToolCallRow, ctx.tool_call_id)
            if row is not None:
                row.status = status
                row.result_summary = clip_head(self.redactor.text(summary), SUMMARY_CHARS)[0]
                row.error_code = error_code
                row.duration_ms = duration_ms
            await self._events(session, ctx, events, duration_ms=duration_ms)

    async def command_run(
        self,
        ctx: CallContext,
        *,
        command: str,
        cwd: str,
        classification: str,
        exit_code: int | None,
        timed_out: bool,
        network: bool,
        stdout: str,
        stderr: str,
        duration_ms: int,
        target: str,
        worker_id: str | None,
        events: list[PendingEvent],
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        async with self.sessionmaker() as session, session.begin():
            session.add(
                CommandRun(
                    id=run_id,
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    tool_call_id=ctx.tool_call_id,
                    worker_id=worker_id,
                    target=target[:32],
                    command=self.redactor.text(command),
                    classification=classification,
                    cwd=cwd,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    network=network,
                    stdout_excerpt=excerpt(self.redactor.text(stdout), EXCERPT_CHARS),
                    stderr_excerpt=excerpt(self.redactor.text(stderr), EXCERPT_CHARS),
                    duration_ms=duration_ms,
                )
            )
            await self._events(
                session, ctx, [PendingEvent(e.event_type, e.payload, e.severity, True) for e in events], duration_ms=duration_ms
            )
        return run_id

    async def test_run(
        self,
        ctx: CallContext,
        *,
        command: str,
        framework: str,
        status: str,
        passed: int,
        failed: int,
        errors: int,
        skipped: int,
        output: str,
        duration_ms: int,
        events: list[PendingEvent],
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        async with self.sessionmaker() as session, session.begin():
            session.add(
                TestRun(
                    id=run_id,
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    command=self.redactor.text(command),
                    framework=framework,
                    status=status,
                    passed=passed,
                    failed=failed,
                    errors=errors,
                    skipped=skipped,
                    output_excerpt=excerpt(self.redactor.text(output), EXCERPT_CHARS),
                    duration_ms=duration_ms,
                )
            )
            await self._events(
                session, ctx, [PendingEvent(e.event_type, e.payload, e.severity, True) for e in events], duration_ms=duration_ms
            )
        return run_id

    async def save_checkpoint(self, ctx: CallContext, checkpoint: dict[str, Any], events: list[PendingEvent]) -> bool:
        """Store ``checkpoint`` on the step row (row lock, status untouched). False if the step is unknown."""
        if ctx.step_id is None:
            return False
        async with self.sessionmaker() as session, session.begin():
            step = (await session.execute(select(Step).where(Step.id == ctx.step_id).with_for_update())).scalar_one_or_none()
            if step is None:
                return False
            step.checkpoint = self.redactor.obj(checkpoint)
            await self._events(session, ctx, events)
        return True
