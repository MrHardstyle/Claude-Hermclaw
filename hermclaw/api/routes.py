"""Route registration (health in P02; full API in P30)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from hermclaw import __version__
from hermclaw.persistence.db import db_health


def register_routes(app: FastAPI) -> None:
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        try:
            db = await db_health()
        except Exception as exc:
            db = {"ok": False, "error": type(exc).__name__}
        return {"status": "ok" if db.get("ok") else "degraded", "version": __version__, "database": db}
