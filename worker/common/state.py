"""Runtime state of a worker daemon: effective :class:`WorkerState`, active work, readiness.

Effective state (reported in heartbeats and ``/health``), first match wins:

``offline``  – shutdown in progress (final heartbeat)
``error``    – a readiness check failed (e.g. Ollama unreachable, sandbox engine missing)
``draining`` – the daemon was asked to drain (finishes running work, accepts nothing new)
``starting`` – startup not finished yet
``busy``     – at least one unit of work is running
``ready``    – otherwise
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from hermclaw import __version__
from hermclaw.contracts.common import WorkerKind, WorkerState


@dataclass(frozen=True)
class ActiveWork:
    key: str
    kind: str
    job_id: str | None
    step_id: str | None
    started_monotonic: float


class DaemonState:
    def __init__(self, worker_id: str, kind: WorkerKind, *, version: str = __version__) -> None:
        self.worker_id = worker_id
        self.kind = kind
        self.version = version
        self.started_at = datetime.now(UTC)
        self._started_monotonic = time.monotonic()
        self.starting = True
        self.draining = False
        self.shutting_down = False
        self.readiness_error: str | None = None
        self.orchestrator_reachable: bool | None = None
        self.orchestrator_compatible: bool | None = None
        self._active: dict[str, ActiveWork] = {}

    # ------------------------------------------------------------------ work tracking
    @contextmanager
    def work(self, key: str, *, kind: str, job_id: str | None = None, step_id: str | None = None) -> Iterator[ActiveWork]:
        item = ActiveWork(key=key, kind=kind, job_id=job_id, step_id=step_id, started_monotonic=time.monotonic())
        self._active[key] = item
        try:
            yield item
        finally:
            self._active.pop(key, None)

    @property
    def active(self) -> list[ActiveWork]:
        return list(self._active.values())

    @property
    def active_job(self) -> str | None:
        for item in self._active.values():
            if item.job_id:
                return item.job_id
        return None

    @property
    def active_step(self) -> str | None:
        for item in self._active.values():
            if item.step_id:
                return item.step_id
        return None

    @property
    def accepting_work(self) -> bool:
        return not (self.draining or self.shutting_down)

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> WorkerState:
        if self.shutting_down:
            return WorkerState.offline
        if self.readiness_error:
            return WorkerState.error
        if self.draining:
            return WorkerState.draining
        if self.starting:
            return WorkerState.starting
        if self._active:
            return WorkerState.busy
        return WorkerState.ready

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self._started_monotonic)
