"""Orchestrator worker API ``/api/workers`` (P07; mounted by the API phase).

- ``POST /api/workers/heartbeat`` – worker-authenticated (HMAC, see :mod:`hermclaw.workers.auth`)
- ``GET  /api/workers``             – registry list (``?kind=&state=``)
- ``GET  /api/workers/{worker_id}`` – detail incl. recent health samples (``?health_limit=``)
- ``POST /api/workers/{worker_id}/drain`` – sticky operator drain on/off

The read/admin endpoints carry ``admin_dependencies`` (the API phase passes its user-auth dependency);
the heartbeat endpoint never does, because workers authenticate with their own credential.

Dependencies are resolved from ``request.app.state.workers_api`` (:class:`WorkerApiContext`); if the app
does not set one, a default context is built lazily from process settings (``get_sessionmaker()``,
``Settings.worker_token_refs``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.params import Depends as DependsParam
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION, WorkerHeartbeat
from hermclaw.core.errors import HermclawError
from hermclaw.core.logging import get_logger
from hermclaw.workers.auth import MAX_CLOCK_SKEW_SECONDS, RefTokenStore, ReplayCache, TokenStore, verify_signed_request
from hermclaw.workers.errors import WorkerAuthError
from hermclaw.workers.registry import WorkerRegistry
from hermclaw.workers.schemas import WORKER_ID_PATTERN, DrainRequest, HeartbeatAck, WorkerDetail, WorkerInfo

log = get_logger(__name__)

MAX_HEARTBEAT_BYTES = 256 * 1024
WorkerIdParam = Annotated[str, PathParam(pattern=WORKER_ID_PATTERN, max_length=100)]


@dataclass
class WorkerApiContext:
    sessionmaker: async_sessionmaker[AsyncSession]
    token_store: TokenStore
    registry: WorkerRegistry = field(default_factory=WorkerRegistry)
    replay_cache: ReplayCache = field(default_factory=ReplayCache)
    max_skew_seconds: float = MAX_CLOCK_SKEW_SECONDS
    max_body_bytes: int = MAX_HEARTBEAT_BYTES

    @classmethod
    def from_settings(cls) -> WorkerApiContext:
        from hermclaw.core.settings import get_settings
        from hermclaw.persistence.db import get_sessionmaker

        return cls(sessionmaker=get_sessionmaker(), token_store=RefTokenStore(get_settings().worker_token_refs))


def get_worker_api_context(request: Request) -> WorkerApiContext:
    ctx = getattr(request.app.state, "workers_api", None)
    if ctx is None:
        ctx = WorkerApiContext.from_settings()
        request.app.state.workers_api = ctx
    if not isinstance(ctx, WorkerApiContext):
        raise RuntimeError("app.state.workers_api must be a WorkerApiContext")
    return ctx


def _error(exc: HermclawError, status: int | None = None) -> JSONResponse:
    return JSONResponse(status_code=status or exc.http_status, content={"error": exc.to_dict()})


async def _read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HermclawError("request body too large", code="PAYLOAD_TOO_LARGE")
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            raise HermclawError("request body too large", code="PAYLOAD_TOO_LARGE")
    return bytes(buf)


def create_workers_router(*, admin_dependencies: Sequence[DependsParam] = ()) -> APIRouter:
    router = APIRouter(prefix="/api/workers", tags=["workers"])

    @router.post("/heartbeat", response_model=HeartbeatAck, responses={401: {}, 403: {}, 413: {}, 422: {}})
    async def heartbeat(request: Request, ctx: WorkerApiContext = Depends(get_worker_api_context)) -> Any:
        try:
            body = await _read_limited(request, ctx.max_body_bytes)
        except HermclawError as exc:
            return _error(exc, 413)
        try:
            verified = verify_signed_request(
                method=request.method,
                path=request.scope["path"],
                query=request.scope.get("query_string", b""),
                headers=dict(request.headers),
                body=body,
                tokens_for=ctx.token_store.tokens_for,
                max_skew_seconds=ctx.max_skew_seconds,
                replay_cache=ctx.replay_cache,
            )
        except WorkerAuthError as exc:
            client = request.client.host if request.client else None
            log.warning("worker heartbeat rejected", extra={"code": exc.code, "remote": client})
            return _error(WorkerAuthError("worker authentication failed", code=exc.code, details=exc.details), 401)
        try:
            hb = WorkerHeartbeat.model_validate_json(body)
        except ValidationError as exc:
            errs = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()[:20]]
            return _error(HermclawError("invalid heartbeat", code="VALIDATION_FAILED", details={"errors": errs}), 422)
        if hb.worker_id != verified.worker_id:
            return _error(HermclawError("heartbeat worker_id does not match the credential", code="WORKER_ID_MISMATCH"), 403)
        remote = request.client.host if request.client else None
        try:
            async with ctx.sessionmaker() as session, session.begin():
                outcome = await ctx.registry.ingest_heartbeat(session, hb, remote_addr=remote)
        except HermclawError as exc:
            return _error(exc)
        if not outcome.compatible:
            msg = f"incompatible worker: expected protocol_version {WORKER_PROTOCOL_VERSION}, kind must match registration"
        elif outcome.stale:
            msg = "stale heartbeat ignored"
        else:
            msg = "ok"
        return HeartbeatAck(
            accepted=outcome.accepted,
            worker_id=outcome.worker_id,
            state=outcome.state,
            compatible=outcome.compatible,
            heartbeat_interval_seconds=ctx.registry.settings.heartbeat_interval_seconds,
            server_time=datetime.now(UTC),
            stale=outcome.stale,
            message=msg,
        )

    admin = list(admin_dependencies)

    @router.get("", response_model=list[WorkerInfo], dependencies=admin)
    async def list_workers(
        kind: WorkerKind | None = None,
        state: WorkerState | None = None,
        ctx: WorkerApiContext = Depends(get_worker_api_context),
    ) -> list[WorkerInfo]:
        async with ctx.sessionmaker() as session:
            return await ctx.registry.list_workers(session, kind=kind, state=state)

    @router.get("/{worker_id}", response_model=WorkerDetail, dependencies=admin, responses={404: {}})
    async def get_worker(
        worker_id: WorkerIdParam,
        health_limit: int = Query(default=20, ge=0, le=1000),
        ctx: WorkerApiContext = Depends(get_worker_api_context),
    ) -> Any:
        async with ctx.sessionmaker() as session:
            try:
                return await ctx.registry.get_worker(session, worker_id, health_limit=health_limit)
            except HermclawError as exc:
                return _error(exc)

    @router.post("/{worker_id}/drain", response_model=WorkerInfo, dependencies=admin, responses={404: {}})
    async def drain_worker(
        body: DrainRequest,
        worker_id: WorkerIdParam,
        ctx: WorkerApiContext = Depends(get_worker_api_context),
    ) -> Any:
        try:
            async with ctx.sessionmaker() as session, session.begin():
                await ctx.registry.set_drain(session, worker_id, body.drain, reason=body.reason)
            async with ctx.sessionmaker() as session:
                return await ctx.registry.get_worker(session, worker_id)
        except HermclawError as exc:
            return _error(exc)

    return router


router = create_workers_router()


def install_workers_api(app: FastAPI, ctx: WorkerApiContext | None = None, *, admin_dependencies: Sequence[DependsParam] = ()) -> None:
    """Mount the router on ``app`` and (optionally) bind a context."""
    if ctx is not None:
        app.state.workers_api = ctx
    app.include_router(create_workers_router(admin_dependencies=admin_dependencies))
