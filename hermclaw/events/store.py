"""Append-only event store with ordered sequence and LISTEN/NOTIFY fan-out (Bauplan §13, P04)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventEnvelope
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.core.settings import get_settings
from hermclaw.persistence.models import Event, Job

NOTIFY_CHANNEL = "hermclaw_events"
MAX_PAYLOAD_CHARS = 64_000
log = get_logger(__name__)


def _clip_payload(payload: dict[str, Any]) -> dict[str, Any]:
    red: dict[str, Any] = dict(DEFAULT_REDACTOR.obj(payload))
    for key, value in list(red.items()):
        if isinstance(value, str) and len(value) > 8000:
            red[key] = value[:8000] + f"…[{len(value) - 8000} chars truncated]"
    return red


async def append_event(
    session: AsyncSession,
    event_type: str,
    *,
    source_type: str,
    source_id: str | None = None,
    job_id: uuid.UUID | None = None,
    step_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
    severity: Severity | str = Severity.info,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    duration_ms: int | None = None,
) -> Event:
    """Insert an event in the caller's transaction. NOTIFY is delivered by PostgreSQL on commit."""
    ev = Event(
        event_id=uuid.uuid4(),
        job_id=job_id,
        step_id=step_id,
        attempt_id=attempt_id,
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        severity=str(severity),
        payload=_clip_payload(payload or {}),
        correlation_id=correlation_id or (str(job_id) if job_id else None),
        duration_ms=duration_ms,
    )
    session.add(ev)
    await session.flush()
    await session.execute(
        text("select pg_notify(:ch, :msg)"),
        {"ch": NOTIFY_CHANNEL, "msg": f"{ev.sequence}:{job_id or ''}"},
    )
    return ev


def to_envelope(ev: Event) -> EventEnvelope:
    return EventEnvelope(
        event_id=ev.event_id,
        sequence=ev.sequence,
        timestamp=ev.ts,
        job_id=ev.job_id,
        step_id=ev.step_id,
        attempt_id=ev.attempt_id,
        source_type=ev.source_type,
        source_id=ev.source_id,
        event_type=ev.event_type,
        severity=Severity(ev.severity),
        payload=ev.payload,
        correlation_id=ev.correlation_id,
        duration_ms=ev.duration_ms,
    )


async def list_events(
    session: AsyncSession,
    *,
    job_id: uuid.UUID | None = None,
    after_sequence: int = 0,
    limit: int = 500,
    event_types: Iterable[str] | None = None,
) -> list[Event]:
    stmt = select(Event).where(Event.sequence > after_sequence).order_by(Event.sequence).limit(min(limit, 5000))
    if job_id is not None:
        stmt = stmt.where(Event.job_id == job_id)
    types = list(event_types or [])
    if types:
        stmt = stmt.where(Event.event_type.in_(types))
    return list((await session.execute(stmt)).scalars())


async def purge_events(session: AsyncSession, *, retention_days: int, now: datetime | None = None) -> int:
    """Retention (P04 4.7): delete events older than N days that belong to finished jobs or no job."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    finished = select(Job.id).where(Job.status.in_(["succeeded", "failed", "cancelled"]))
    stmt = delete(Event).where(Event.ts < cutoff).where((Event.job_id.is_(None)) | (Event.job_id.in_(finished)))
    result = await session.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


class EventBroadcaster:
    """One LISTEN connection per process; wakes subscribers when new events are committed."""

    def __init__(self, database_url: str | None = None) -> None:
        self._url = _psycopg_url(database_url or get_settings().database_url)
        self._subscribers: dict[int, tuple[uuid.UUID | None, asyncio.Event]] = {}
        self._task: asyncio.Task[None] | None = None
        self._next = 0
        self._latest = 0
        self.connected = asyncio.Event()

    @property
    def latest_sequence(self) -> int:
        return self._latest

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="event-broadcaster")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

    def subscribe(self, job_id: uuid.UUID | None) -> tuple[int, asyncio.Event]:
        self._next += 1
        flag = asyncio.Event()
        self._subscribers[self._next] = (job_id, flag)
        return self._next, flag

    def unsubscribe(self, token: int) -> None:
        self._subscribers.pop(token, None)

    def _dispatch(self, payload: str) -> None:
        seq_s, _, job_s = payload.partition(":")
        with contextlib.suppress(ValueError):
            self._latest = max(self._latest, int(seq_s))
        for job_id, flag in self._subscribers.values():
            if job_id is None or str(job_id) == job_s:
                flag.set()

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self._url, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    self.connected.set()
                    backoff = 1.0
                    # wake everybody once after (re)connect so they re-query the DB (no lost events)
                    for _, flag in self._subscribers.values():
                        flag.set()
                    async for notify in conn.notifies():
                        self._dispatch(notify.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected.clear()
                log.warning("event listener disconnected", extra={"error": str(exc)})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


async def stream_events(
    sessionmaker: Any,
    broadcaster: EventBroadcaster,
    *,
    job_id: uuid.UUID | None,
    last_event_id: int = 0,
    heartbeat_seconds: float = 15.0,
    stop: asyncio.Event | None = None,
) -> AsyncIterator[EventEnvelope | None]:
    """Replay events after ``last_event_id`` and then follow live; yields ``None`` as heartbeat."""
    token, flag = broadcaster.subscribe(job_id)
    cursor = last_event_id
    try:
        while stop is None or not stop.is_set():
            flag.clear()
            async with sessionmaker() as session:
                batch = await list_events(session, job_id=job_id, after_sequence=cursor, limit=500)
            for ev in batch:
                cursor = ev.sequence
                yield to_envelope(ev)
            if len(batch) == 500:
                continue
            try:
                await asyncio.wait_for(flag.wait(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield None
    finally:
        broadcaster.unsubscribe(token)
