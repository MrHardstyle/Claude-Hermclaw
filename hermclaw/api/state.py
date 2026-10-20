"""Per-process API state: DB engine, event broadcaster (extended in P30)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hermclaw.core.config import HermclawConfig, get_config
from hermclaw.core.logging import get_logger
from hermclaw.events.store import EventBroadcaster
from hermclaw.persistence.db import dispose_engine, get_sessionmaker, init_engine

if TYPE_CHECKING:
    from hermclaw.memory.store import MemoryStore

log = get_logger(__name__)


class AppState:
    def __init__(self) -> None:
        self.config: HermclawConfig = get_config()
        self.broadcaster = EventBroadcaster()
        self._memory: MemoryStore | None = None

    async def startup(self) -> None:
        init_engine()
        await self.broadcaster.start()

    async def shutdown(self) -> None:
        await self.broadcaster.stop()
        await dispose_engine()

    @property
    def sessionmaker(self):  # type: ignore[no-untyped-def]
        return get_sessionmaker()

    @property
    def memory(self) -> MemoryStore:
        """The knowledge store of the "Wissen" page (D-019); without the gateway it searches by words only."""
        if self._memory is None:
            from hermclaw.memory.store import MemoryStore
            from hermclaw.models.gateway import LiteLLMGateway

            embedder = None
            try:
                embedder = LiteLLMGateway.from_config(self.config, session_factory=self.sessionmaker)
            except Exception as exc:  # no LiteLLM key here: entries are still stored and found by their words
                log.warning("memory without embeddings", extra={"error_type": type(exc).__name__})
            self._memory = MemoryStore(self.sessionmaker, embedder, self.config.policies.memory)
        return self._memory

    @memory.setter
    def memory(self, store: MemoryStore) -> None:
        self._memory = store
