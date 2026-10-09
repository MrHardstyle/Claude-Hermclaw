"""Persistence helpers shared by the wake controller and the idle-sleep policy.

* ``wake_events`` rows – the per-stage wake/sleep history of a worker (Bauplan §10, P10 10.8)
* worker state changes the registry API does not offer (restoring ``ready``), mirroring the registry's
  row conventions (``metadata.state_reason`` / ``state_changed_at``) and its ``worker.state`` event.

All functions run in the caller's transaction and only ``flush``.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import Severity, WorkerState
from hermclaw.contracts.events import EventType
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.persistence.models import WakeEvent, Worker

SOURCE_TYPE = "wake_controller"
_MAX_DETAIL_CHARS = 500


def clip(text: str, limit: int = _MAX_DETAIL_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


async def record_wake_event(
    session: AsyncSession,
    *,
    worker_id: str,
    job_id: uuid.UUID | None,
    stage: str,
    status: str,
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> WakeEvent:
    """Insert one ``wake_events`` row. ``details`` pass the redactor (never secrets in the history)."""
    row = WakeEvent(
        worker_id=worker_id,
        job_id=job_id,
        stage=stage[:32],
        status=status[:16],
        error_code=error_code,
        details=dict(DEFAULT_REDACTOR.obj(details or {})),
    )
    session.add(row)
    await session.flush()
    return row


async def force_worker_state(
    session: AsyncSession,
    worker_id: str,
    target: WorkerState,
    *,
    reason: str,
    only_from: Collection[WorkerState],
    now: datetime | None = None,
    source_type: str = SOURCE_TYPE,
) -> WorkerState | None:
    """Set ``workers.state = target`` iff the locked row is currently in ``only_from``.

    Returns the previous state when the row changed, ``None`` otherwise (unknown worker, or a concurrent
    heartbeat already moved the worker to a state not in ``only_from``). Emits ``worker.state``.
    """
    row = (await session.execute(select(Worker).where(Worker.id == worker_id).with_for_update())).scalar_one_or_none()
    if row is None:
        return None
    previous = WorkerState(row.state)
    if previous == target or previous not in only_from:
        return None
    ts = now or datetime.now(UTC)
    row.state = target.value
    row.metadata_ = {**(row.metadata_ or {}), "state_reason": reason, "state_changed_at": ts.isoformat()}
    await append_event(
        session,
        EventType.WORKER_STATE,
        source_type=source_type,
        source_id=worker_id,
        severity=Severity.error if target == WorkerState.error else Severity.info,
        payload={"worker_id": worker_id, "from": previous.value, "to": target.value, "reason": reason},
    )
    await session.flush()
    return previous
