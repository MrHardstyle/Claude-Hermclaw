"""Base FastAPI app for the worker daemons + uvicorn runner.

:func:`create_daemon_app` wires what every daemon shares:

- :class:`~worker.common.auth.SignedRequestMiddleware` (everything except ``GET /health`` must be
  signed by the orchestrator with this worker's credential)
- uniform error answers ``{"error": {"code", "message", "details"}}`` for :class:`HermclawError`,
  validation errors (422) and Starlette HTTP errors
- ``GET /health`` (unauthenticated, no sensitive data) built from daemon-specific checks
- lifespan: startup hooks, heartbeat sender start, final ``offline`` heartbeat on shutdown
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION
from hermclaw.core.errors import HermclawError
from hermclaw.core.logging import get_logger
from hermclaw.workers.auth import TokenFile
from hermclaw.workers.schemas import DaemonHealth
from worker.common.auth import SignedRequestMiddleware
from worker.common.heartbeat import ExtrasProvider, HeartbeatSender
from worker.common.settings import WorkerDaemonSettings
from worker.common.state import DaemonState

log = get_logger(__name__)

HealthChecks = Callable[[], Awaitable[dict[str, bool]]]
Hook = Callable[[], Awaitable[None]]


@dataclass
class DaemonContext:
    """Everything a daemon's route handlers need; stored on ``app.state.daemon``."""

    settings: WorkerDaemonSettings
    state: DaemonState
    token_file: TokenFile
    heartbeat: HeartbeatSender | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def get_context(request: Request) -> DaemonContext:
    ctx = request.app.state.daemon
    assert isinstance(ctx, DaemonContext)
    return ctx


def _error_response(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details or {}}})


def install_error_handlers(app: FastAPI) -> None:
    async def hermclaw_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, HermclawError)
        status = int(getattr(exc, "http_status", 500))
        if status >= 500:
            log.warning("daemon request failed", extra={"code": exc.code, "error": exc.message})
        return _error_response(status, exc.code, exc.message, exc.details)

    async def validation_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        errs = [{"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))} for e in exc.errors()[:20]]
        return _error_response(422, "VALIDATION_FAILED", "request validation failed", {"errors": errs})

    async def http_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        return _error_response(exc.status_code, f"HTTP_{exc.status_code}", str(exc.detail))

    app.add_exception_handler(HermclawError, hermclaw_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(StarletteHTTPException, http_error)


def create_daemon_app(
    settings: WorkerDaemonSettings,
    *,
    title: str,
    token_file: TokenFile | None = None,
    state: DaemonState | None = None,
    health_checks: HealthChecks | None = None,
    extras: ExtrasProvider | None = None,
    heartbeat: bool = True,
    heartbeat_transport: httpx.AsyncBaseTransport | None = None,
    on_startup: Sequence[Hook] = (),
    on_shutdown: Sequence[Hook] = (),
) -> FastAPI:
    tokens = token_file or TokenFile(settings.token_file)
    tokens.tokens()  # fail fast: a daemon without a valid credential must not start
    daemon_state = state or DaemonState(settings.worker_id, settings.kind)
    ctx = DaemonContext(settings=settings, state=daemon_state, token_file=tokens)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for hook in on_startup:
            await hook()
        if heartbeat and settings.orchestrator_url:
            ctx.heartbeat = HeartbeatSender(settings, daemon_state, tokens.current, extras=extras, transport=heartbeat_transport)
            ctx.heartbeat.start()
        daemon_state.starting = False
        log.info("worker daemon started", extra={"worker_id": settings.worker_id, "kind": settings.kind.value, "bind": settings.bind})
        try:
            yield
        finally:
            daemon_state.draining = True
            if ctx.heartbeat is not None:
                await ctx.heartbeat.stop(final=True)
            for hook in on_shutdown:
                try:
                    await hook()
                except Exception:
                    log.exception("shutdown hook failed")
            daemon_state.shutting_down = True
            log.info("worker daemon stopped", extra={"worker_id": settings.worker_id})

    app = FastAPI(title=title, version=daemon_state.version, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.daemon = ctx
    install_error_handlers(app)
    app.add_middleware(
        SignedRequestMiddleware,
        worker_id=settings.worker_id,
        tokens=tokens.tokens,
        exempt_paths=("/health",),
        max_skew_seconds=settings.max_skew_seconds,
        max_body_bytes=settings.max_body_bytes,
    )

    @app.get("/health", response_model=DaemonHealth)
    async def health() -> DaemonHealth:
        checks: dict[str, bool] = {}
        if health_checks is not None:
            try:
                checks = await asyncio.wait_for(health_checks(), timeout=10.0)
            except Exception as exc:
                log.warning("health checks failed", extra={"error": f"{type(exc).__name__}: {exc}"})
                checks = {"health_checks": False}
        ok = all(checks.values()) and daemon_state.readiness_error is None
        return DaemonHealth(
            status="ok" if ok else "degraded",
            worker_id=settings.worker_id,
            kind=settings.kind,
            state=daemon_state.state,
            protocol_version=WORKER_PROTOCOL_VERSION,
            worker_version=daemon_state.version,
            uptime_seconds=daemon_state.uptime_seconds,
            checks=checks,
            orchestrator_reachable=daemon_state.orchestrator_reachable,
        )

    return app


def run_daemon(app: FastAPI, settings: WorkerDaemonSettings, *, graceful_timeout_seconds: int = 30) -> None:
    """Serve ``app`` with uvicorn on ``WORKER_BIND`` (SIGTERM -> graceful drain -> final heartbeat)."""
    import uvicorn

    config = uvicorn.Config(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,  # keep our JSON logging
        access_log=False,
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=graceful_timeout_seconds,
        lifespan="on",
    )
    uvicorn.Server(config).run()
