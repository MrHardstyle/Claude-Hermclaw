"""FastAPI application factory (P02 2.6/2.10; extended in P30)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hermclaw import PROTOCOL_VERSION, __version__
from hermclaw.core.errors import HermclawError
from hermclaw.core.logging import configure_logging, get_logger
from hermclaw.core.settings import get_settings

log = get_logger(__name__)


def create_app(*, lifespan_hooks: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from hermclaw.api.state import AppState

        state = AppState()
        app.state.hermclaw = state
        if lifespan_hooks:
            await state.startup()
        try:
            yield
        finally:
            if lifespan_hooks:
                await state.shutdown()

    app = FastAPI(title="Hermclaw Next API", version=__version__, lifespan=lifespan)

    @app.exception_handler(HermclawError)
    async def _hermclaw_error(_: Request, exc: HermclawError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    @app.get("/api/version")
    async def version() -> dict[str, Any]:
        return {"version": __version__, "protocol_version": PROTOCOL_VERSION, "env": settings.env, "instance": settings.instance_id}

    from hermclaw.api.routes import register_routes

    register_routes(app)
    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level, "hermclaw-api", settings.log_json)
    uvicorn.run(create_app(), host=settings.api_host, port=settings.api_port, proxy_headers=True, forwarded_allow_ips="*")
