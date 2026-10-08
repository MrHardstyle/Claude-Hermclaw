"""System API: health, workers, models, resources, repositories, bugs (Bauplan §34, P30 30.3-30.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw import __version__
from hermclaw.api.auth import Principal, require
from hermclaw.api.jobs import _rows, get_session
from hermclaw.core.config import get_config
from hermclaw.persistence.db import db_health
from hermclaw.persistence.models import (
    BugRecord,
    Job,
    ModelInvocation,
    ModelProfile,
    Repository,
    ResourceLease,
    ResourceRequest,
    Worker,
    WorkerCapability,
)

router = APIRouter(prefix="/api", tags=["system"])


async def _litellm_health() -> dict[str, Any]:
    cfg = get_config().models.litellm
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(cfg.base_url.rstrip("/") + "/health/liveliness")
        return {"ok": r.status_code == 200, "status_code": r.status_code}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": type(exc).__name__}


@router.get("/health")
async def health(deep: bool = Query(False, description="also probe LiteLLM")) -> dict[str, Any]:
    """Unauthenticated liveness/readiness (no secrets, no job data)."""
    try:
        db = await db_health()
    except Exception as exc:
        db = {"ok": False, "error": type(exc).__name__}
    out: dict[str, Any] = {
        "status": "ok" if db.get("ok") else "degraded",
        "version": __version__,
        "database": db,
        "time": datetime.now(UTC).isoformat(),
    }
    if deep:
        out["litellm"] = await _litellm_health()
        if not out["litellm"].get("ok"):
            out["status"] = "degraded"
    return out


@router.get("/workers")
async def list_workers(_: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    workers = (await session.execute(select(Worker).order_by(Worker.id))).scalars().all()
    caps = (await session.execute(select(WorkerCapability))).scalars().all()
    by_worker: dict[str, list[str]] = {}
    for c in caps:
        by_worker.setdefault(c.worker_id, []).append(c.capability)
    out = []
    for w in _rows(workers):
        w["capabilities"] = sorted(by_worker.get(w["id"], []))
        w["display_state"] = str(w["state"]).upper()
        out.append(w)
    return out


@router.get("/models")
async def list_models(_: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    cfg = get_config().models
    db_profiles = {p.alias: p for p in (await session.execute(select(ModelProfile))).scalars()}
    stats_rows = (
        await session.execute(
            select(
                ModelInvocation.alias,
                func.count(),
                func.avg(ModelInvocation.latency_ms),
                func.sum(ModelInvocation.completion_tokens),
                func.count().filter(ModelInvocation.status != "succeeded"),
            ).group_by(ModelInvocation.alias)
        )
    ).all()
    stats = {
        r[0]: {"calls": r[1], "avg_latency_ms": float(r[2] or 0), "completion_tokens": int(r[3] or 0), "errors": int(r[4] or 0)}
        for r in stats_rows
    }
    profiles = []
    for p in cfg.profiles:
        d = p.model_dump()
        d["persisted"] = p.alias in db_profiles
        d["stats"] = stats.get(p.alias, {"calls": 0})
        profiles.append(d)
    return {"litellm": {"base_url": cfg.litellm.base_url}, "profiles": profiles}


@router.get("/resources")
async def list_resources(_: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    leases = (
        (
            await session.execute(
                select(ResourceLease).where(ResourceLease.state.in_(["active", "preempting"])).order_by(ResourceLease.priority.desc())
            )
        )
        .scalars()
        .all()
    )
    waiting = (
        (
            await session.execute(
                select(ResourceRequest)
                .where(ResourceRequest.state == "waiting")
                .order_by(ResourceRequest.priority.desc(), ResourceRequest.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "active_leases": _rows(leases),
        "waiting_requests": _rows(waiting),
        "model_host_capacity_gb": get_config().models.model_host_capacity_gb,
    }


@router.get("/repositories")
async def list_repositories(_: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    return _rows((await session.execute(select(Repository).order_by(Repository.name))).scalars().all())


@router.get("/bugs")
async def list_bugs(
    status: str | None = None, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    stmt = select(BugRecord).order_by(BugRecord.severity, BugRecord.created_at)
    if status:
        stmt = stmt.where(BugRecord.status == status)
    return _rows((await session.execute(stmt)).scalars().all())


@router.get("/stats")
async def stats(_: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    by_status = dict((await session.execute(select(Job.status, func.count()).group_by(Job.status))).all())
    return {"jobs_by_status": by_status, "jobs_total": sum(by_status.values())}
