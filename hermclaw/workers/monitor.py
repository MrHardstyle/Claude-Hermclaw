"""Background offline detection (P07 7.8): periodic registry sweep plus health-history pruning.

The scheduler/API process starts one :class:`OfflineMonitor` per orchestrator. Each sweep runs in its
own transaction; ``SKIP LOCKED`` in the sweep makes concurrent monitors (e.g. during a rolling restart)
safe. Errors are logged and retried on the next tick - the loop never dies on a DB hiccup.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.logging import get_logger
from hermclaw.workers.registry import WorkerRegistry

log = get_logger(__name__)


class OfflineMonitor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        registry: WorkerRegistry,
        *,
        interval_seconds: float | None = None,
        prune_every_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self.registry = registry
        # sweep at least twice per offline threshold so detection latency stays below 1.5 x threshold
        default_interval = max(1.0, registry.settings.offline_after.total_seconds() / 2)
        self.interval_seconds = interval_seconds if interval_seconds is not None else default_interval
        self._prune_every = prune_every_seconds
        self._clock = clock
        self._last_prune: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.sweeps = 0
        self.last_error: str | None = None

    async def run_once(self) -> list[str]:
        async with self._sessionmaker() as session, session.begin():
            offline = await self.registry.sweep_offline(session)
        self.sweeps += 1
        now = self._clock()
        if self._last_prune is None or now - self._last_prune >= self._prune_every:
            async with self._sessionmaker() as session, session.begin():
                pruned = await self.registry.prune_health(session)
            self._last_prune = now
            if pruned:
                log.info("pruned worker health history", extra={"rows": pruned})
        return offline

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("worker offline sweep failed", extra={"error": self.last_error})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="worker-offline-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()
