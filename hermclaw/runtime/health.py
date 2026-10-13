"""Runtime health of the orchestrator host (P35.9 full disk warning).

:class:`DiskMonitor` watches the free space of the runtime data paths (workspaces, mirrors, artifacts) and
emits ``system.disk`` events when a path crosses the warning or critical threshold – and once more when it
recovers. While any path is *critical* the scheduler admits no new job preparation (clones and workspaces would
fill the disk); running steps continue so they can finish, commit and free space.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.logging import get_logger
from hermclaw.events.store import append_event

log = get_logger(__name__)
Level = Literal["ok", "warning", "critical"]
MB = 1024 * 1024


@dataclass(frozen=True)
class DiskReading:
    path: str
    free_mb: int
    total_mb: int
    level: Level


def _existing(path: Path) -> Path:
    """Nearest existing ancestor (a data dir may not exist yet on a fresh host)."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


class DiskMonitor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        paths: Sequence[Path],
        *,
        warn_free_mb: int = 10 * 1024,
        critical_free_mb: int = 2 * 1024,
        interval_seconds: float = 60.0,
        usage: Callable[[Path], tuple[int, int, int]] | None = None,
        source_id: str = "orchestrator",
    ) -> None:
        if critical_free_mb > warn_free_mb:
            raise ValueError("critical_free_mb must not exceed warn_free_mb")
        self.sm = sessionmaker
        self.paths = list(dict.fromkeys(Path(p) for p in paths))
        self.warn_free_mb, self.critical_free_mb = warn_free_mb, critical_free_mb
        self.interval_seconds = interval_seconds
        self._usage = usage or (lambda p: tuple(shutil.disk_usage(p)))  # type: ignore[arg-type, return-value]
        self.source_id = source_id
        self.levels: dict[str, Level] = {}
        self.readings: list[DiskReading] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def _level(self, free_mb: int) -> Level:
        if free_mb < self.critical_free_mb:
            return "critical"
        if free_mb < self.warn_free_mb:
            return "warning"
        return "ok"

    @property
    def critical(self) -> list[DiskReading]:
        return [r for r in self.readings if r.level == "critical"]

    def admission_block(self) -> str | None:
        """Reason why new jobs must not start (``None`` = admit)."""
        crit = self.critical
        if not crit:
            return None
        return "disk almost full: " + ", ".join(f"{r.path} {r.free_mb} MB free" for r in crit)

    async def check_once(self) -> list[DiskReading]:
        readings: list[DiskReading] = []
        for path in self.paths:
            try:
                total, _used, free = await asyncio.to_thread(self._usage, _existing(path))
            except OSError as exc:
                log.warning("disk usage check failed", extra={"path": str(path), "error": str(exc)})
                continue
            readings.append(DiskReading(str(path), int(free // MB), int(total // MB), self._level(int(free // MB))))
        self.readings = readings
        changed = [r for r in readings if self.levels.get(r.path, "ok") != r.level]
        if changed:
            async with self.sm() as s:
                for r in changed:
                    previous = self.levels.get(r.path, "ok")
                    severity = Severity.error if r.level == "critical" else Severity.warning if r.level == "warning" else Severity.info
                    text = (
                        f"Speicherplatz wieder ausreichend: {r.path} ({r.free_mb} MB frei)"
                        if r.level == "ok"
                        else f"Speicherplatz {'kritisch' if r.level == 'critical' else 'knapp'}: {r.path} ({r.free_mb} MB frei)"
                    )
                    await append_event(
                        s,
                        EventType.SYSTEM_DISK,
                        source_type="runtime",
                        source_id=self.source_id,
                        severity=severity,
                        payload={
                            "text": text,
                            "path": r.path,
                            "free_mb": r.free_mb,
                            "total_mb": r.total_mb,
                            "level": r.level,
                            "previous": previous,
                            "warn_free_mb": self.warn_free_mb,
                            "critical_free_mb": self.critical_free_mb,
                        },
                    )
                await s.commit()
            for r in changed:
                self.levels[r.path] = r.level
        return readings

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the monitor must never take the scheduler down
                log.warning("disk monitor failed", extra={"error": f"{type(exc).__name__}: {exc}"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="disk-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None


__all__ = ["DiskMonitor", "DiskReading"]
