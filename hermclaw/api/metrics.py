"""Prometheus metrics endpoint (P42.5 monitoring): ``GET /api/metrics`` (text exposition format 0.0.4).

Read scope required (Prometheus: ``authorization: {credentials_file: …}``). Only aggregate runtime state – no job
content, prompts or secrets. Values come straight from PostgreSQL, so the endpoint also works while the scheduler
process is down (then ``hermclaw_steps_in_flight`` and lease ages show it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw import __version__
from hermclaw.api.auth import Principal, require
from hermclaw.api.jobs import get_session

router = APIRouter(prefix="/api", tags=["metrics"])

IN_FLIGHT = ("leased", "running", "verifying", "reviewing", "committing")


def _esc(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def metric(self, name: str, mtype: str, help_text: str, value: float, **labels: Any) -> None:
        if name not in self._declared:
            self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {mtype}"]
            self._declared.add(name)
        lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        self.lines.append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


async def collect(session: AsyncSession) -> str:
    out = _Out()
    now = datetime.now(UTC)
    out.metric("hermclaw_build_info", "gauge", "Hermclaw version", 1, version=__version__)
    for status, n in (await session.execute(text("SELECT status, count(*) FROM jobs GROUP BY status"))).all():
        out.metric("hermclaw_jobs", "gauge", "Jobs by status", n, status=status)
    steps = (
        await session.execute(text("SELECT count(*) FROM steps WHERE superseded = false AND status = ANY(:s)"), {"s": list(IN_FLIGHT)})
    ).scalar_one()
    out.metric("hermclaw_steps_in_flight", "gauge", "Steps currently leased/running/verifying/reviewing/committing", steps)
    rows = (
        await session.execute(
            text("SELECT resource, state, count(*) FROM resource_leases WHERE state IN ('active','preempting') GROUP BY resource, state")
        )
    ).all()
    for resource, state, n in rows:
        out.metric("hermclaw_resource_leases", "gauge", "Active resource leases", n, resource=resource, state=state)
    waiting = (await session.execute(text("SELECT count(*) FROM resource_requests WHERE state = 'waiting'"))).scalar_one()
    out.metric("hermclaw_resource_requests_waiting", "gauge", "Resource requests waiting for a grant", waiting)
    for wid, kind, state, last in (await session.execute(text("SELECT id, kind, state, last_heartbeat_at FROM workers ORDER BY id"))).all():
        out.metric(
            "hermclaw_worker_up",
            "gauge",
            "1 if the worker is ready or busy",
            1 if state in ("ready", "busy") else 0,
            worker=wid,
            kind=kind,
            state=state,
        )
        if last is not None:
            age = max(0.0, (now - last).total_seconds())
            out.metric(
                "hermclaw_worker_heartbeat_age_seconds", "gauge", "Seconds since the last worker heartbeat", round(age, 1), worker=wid
            )
    seq = (await session.execute(text("SELECT coalesce(max(sequence), 0) FROM events"))).scalar_one()
    out.metric("hermclaw_events_sequence", "counter", "Highest event sequence number", seq)
    disk = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (payload->>'path') payload->>'path', payload->>'level', (payload->>'free_mb')::bigint "
                "FROM events WHERE event_type = 'system.disk' ORDER BY payload->>'path', sequence DESC"
            )
        )
    ).all()
    for path, level, free in disk:
        out.metric(
            "hermclaw_disk_free_mb", "gauge", "Free space of a runtime data path (last disk event)", free or 0, path=path, level=level
        )
    since = (
        await session.execute(
            text("SELECT status, count(*) FROM model_invocations WHERE started_at > now() - interval '1 hour' GROUP BY status")
        )
    ).all()
    for status, n in since:
        out.metric("hermclaw_model_invocations_last_hour", "gauge", "Model invocations in the last hour by status", n, status=status)
    return out.render()


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(_: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)) -> PlainTextResponse:
    return PlainTextResponse(await collect(session), media_type="text/plain; version=0.0.4; charset=utf-8")


__all__ = ["collect", "router"]
