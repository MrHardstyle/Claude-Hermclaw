"""Route registration (P30)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from hermclaw.api import events, jobs, system


class SseHeadersMiddleware:
    """Pure ASGI middleware: disable proxy buffering for SSE (Nginx honours X-Accel-Buffering, research 20261008-014)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/event-stream"):
                    present = {k.lower() for k, _ in headers}
                    for name, value in ((b"x-accel-buffering", b"no"), (b"cache-control", b"no-cache")):
                        if name not in present:
                            headers.append((name, value))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


def register_routes(app: FastAPI) -> None:
    app.add_middleware(SseHeadersMiddleware)
    app.include_router(system.router)
    app.include_router(jobs.router)
    app.include_router(events.router)
    # orchestrator worker API (P07): signed heartbeats from .222/.224; the read/admin views need the admin scope
    from fastapi import Depends

    from hermclaw.api.auth import require
    from hermclaw.workers.api import create_workers_router

    app.include_router(create_workers_router(admin_dependencies=[Depends(require("admin"))]))
