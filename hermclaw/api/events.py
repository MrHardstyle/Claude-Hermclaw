"""Event API + SSE (Bauplan §34, P04 4.5, P30 30.7)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.api.auth import Principal, require
from hermclaw.api.jobs import get_session
from hermclaw.api.schemas import EventOut
from hermclaw.core.config import get_config
from hermclaw.core.errors import NotFoundError
from hermclaw.events.store import EventBroadcaster, list_events, stream_events
from hermclaw.persistence.db import get_sessionmaker
from hermclaw.persistence.models import Job

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/jobs/{job_id}/events", response_model=list[EventOut])
async def get_job_events(
    job_id: uuid.UUID,
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
    types: str | None = Query(None, description="comma separated event types"),
    _: Principal = Depends(require("read")),
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    if (await session.execute(select(Job.id).where(Job.id == job_id))).scalar_one_or_none() is None:
        raise NotFoundError(f"job {job_id} not found")
    rows = await list_events(
        session, job_id=job_id, after_sequence=after, limit=limit, event_types=[t for t in (types or "").split(",") if t]
    )
    return [EventOut.model_validate(r) for r in rows]


def _broadcaster(request: Request) -> EventBroadcaster:
    state = getattr(request.app.state, "hermclaw", None)
    if state is None:  # pragma: no cover - app always has state
        raise RuntimeError("app state missing")
    b: EventBroadcaster = state.broadcaster
    return b


async def _sse(request: Request, job_id: uuid.UUID | None, last_event_id: int, heartbeat: float) -> AsyncIterator[ServerSentEvent]:
    broadcaster = _broadcaster(request)
    await broadcaster.start()
    stop = asyncio.Event()
    yield ServerSentEvent(comment="connected", retry=3000)
    async for env in stream_events(
        get_sessionmaker(), broadcaster, job_id=job_id, last_event_id=last_event_id, heartbeat_seconds=heartbeat, stop=stop
    ):
        if await request.is_disconnected():
            stop.set()
            break
        if env is None:
            yield ServerSentEvent(comment="heartbeat")
            continue
        data: dict[str, Any] = env.model_dump(mode="json")
        yield ServerSentEvent(data=data, event=env.event_type, id=str(env.sequence))


@router.get("/jobs/{job_id}/events/stream", response_class=EventSourceResponse)
async def stream_job_events(
    request: Request,
    job_id: uuid.UUID,
    last_event_id: int = Query(0, ge=0),
    last_event_id_header: str | None = Header(None, alias="Last-Event-ID"),
    _: Principal = Depends(require("read")),
) -> AsyncIterator[ServerSentEvent]:
    start = int(last_event_id_header) if last_event_id_header and last_event_id_header.isdigit() else last_event_id
    async for ev in _sse(request, job_id, start, float(get_config().policies.events.heartbeat_seconds)):
        yield ev


@router.get("/events/stream", response_class=EventSourceResponse)
async def stream_all_events(
    request: Request,
    last_event_id: int = Query(0, ge=0),
    last_event_id_header: str | None = Header(None, alias="Last-Event-ID"),
    _: Principal = Depends(require("read")),
) -> AsyncIterator[ServerSentEvent]:
    start = int(last_event_id_header) if last_event_id_header and last_event_id_header.isdigit() else last_event_id
    if start == 0:
        # dashboards start "now" unless they ask for replay explicitly
        async with get_sessionmaker()() as s:
            latest = await list_events(s, after_sequence=0, limit=1)
            if latest:
                from sqlalchemy import func

                from hermclaw.persistence.models import Event

                start = int((await s.execute(select(func.max(Event.sequence)))).scalar_one() or 0)
    async for ev in _sse(request, None, start, float(get_config().policies.events.heartbeat_seconds)):
        yield ev
