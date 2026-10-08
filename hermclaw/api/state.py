"""Per-process API state: DB engine, event broadcaster (extended in P30)."""

from __future__ import annotations

from hermclaw.core.config import HermclawConfig, get_config
from hermclaw.events.store import EventBroadcaster
from hermclaw.persistence.db import dispose_engine, get_sessionmaker, init_engine


class AppState:
    def __init__(self) -> None:
        self.config: HermclawConfig = get_config()
        self.broadcaster = EventBroadcaster()

    async def startup(self) -> None:
        init_engine()
        await self.broadcaster.start()

    async def shutdown(self) -> None:
        await self.broadcaster.stop()
        await dispose_engine()

    @property
    def sessionmaker(self):  # type: ignore[no-untyped-def]
        return get_sessionmaker()
