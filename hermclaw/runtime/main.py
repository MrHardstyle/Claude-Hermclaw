"""``hermclaw scheduler`` – the runtime process on ``.225`` (systemd unit ``hermclaw-scheduler``).

Start sequence:

1. logging, configuration, database engine; the database must be reachable and migrated (fail fast otherwise –
   systemd restarts the unit);
2. composition root (:func:`hermclaw.runtime.services.build_services`);
3. workers from ``hosts.yaml`` are registered/refreshed; the offline monitor sweeps stale heartbeats;
4. resource recovery: leases of a previous incarnation of *this* instance are released
   (:meth:`ResourceManager.recover`), expired leases swept periodically (``run_maintenance``);
5. :meth:`Scheduler.run_forever` – startup recovery of in-flight steps/jobs (P34), then the dispatch loop.

SIGTERM/SIGINT stop the loop gracefully: running coder loops are asked to checkpoint, the rest is recovered by the
next start. The instance id is stable (``scheduler@<instance_id>``), so a restarted process reclaims its own
leases immediately instead of waiting for them to expire.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import signal
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import HermclawConfig, get_config
from hermclaw.core.errors import HermclawError
from hermclaw.core.logging import configure_logging, get_logger
from hermclaw.core.settings import Settings, get_settings
from hermclaw.persistence.db import make_engine
from hermclaw.runtime.services import RuntimeServices, ServiceOverrides, build_services
from hermclaw.scheduler.scheduler import Scheduler, SchedulerSettings
from hermclaw.workers.monitor import OfflineMonitor

log = get_logger(__name__)


async def check_database(sm: async_sessionmaker[AsyncSession]) -> str | None:
    """Reachability + migration state; returns the alembic revision (``None`` when the schema was never migrated)."""
    async with sm() as s:
        await s.execute(text("SELECT 1"))
        try:
            rev = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
        except Exception:
            return None
    return str(rev) if rev else None


def _build_wake(config: HermclawConfig, settings: Settings, sm: async_sessionmaker[AsyncSession]) -> Any:
    if not settings.wake_on_lan or not any(h.wake_on_lan and h.wake_on_lan.enabled for h in config.hosts.hosts):
        return None
    try:
        from hermclaw.wol.readiness import WakeController
    except ImportError:  # pragma: no cover - partial installation
        return None
    return WakeController(sm, config)


class SchedulerProcess:
    """The assembled process; :meth:`run` until :meth:`stop` (signal) – usable in-process by tests."""

    def __init__(
        self,
        config: HermclawConfig,
        settings: Settings,
        *,
        overrides: ServiceOverrides | None = None,
        scheduler_settings: SchedulerSettings | None = None,
        database_url: str | None = None,
    ) -> None:
        self.config, self.settings = config, settings
        self.overrides = overrides or ServiceOverrides()
        self.scheduler_settings = scheduler_settings or SchedulerSettings(
            concurrency=settings.scheduler_concurrency,
            poll_seconds=settings.scheduler_poll_seconds,
            step_timeout_seconds=settings.scheduler_step_timeout_seconds,
        )
        self.engine = make_engine(database_url or settings.database_url)
        self.sm = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.services: RuntimeServices | None = None
        self.scheduler: Scheduler | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        if self.scheduler is not None:
            self.scheduler.stop()

    async def run(self) -> int:
        try:  # P10.1: unusable Wake-on-LAN host configuration is fatal, warnings are logged
            from hermclaw.wol.config import assert_wake_config

            for issue in assert_wake_config(self.config):
                log.warning("wake-on-lan config", extra={"host": issue.host_id, "field": issue.field, "issue": issue.message})
        except HermclawError as exc:
            log.error("invalid configuration", extra={"error": exc.message})
            await self.engine.dispose()
            return 3
        revision = await check_database(self.sm)
        if revision is None:
            log.error("database schema missing – run 'hermclaw migrate' first")
            await self.engine.dispose()
            return 2
        overrides = self.overrides
        wake = None
        if overrides.wake is None:
            wake = _build_wake(self.config, self.settings, self.sm)
            if wake is not None:
                overrides = dataclasses.replace(overrides, wake=wake)
        services = build_services(self.config, self.settings, self.sm, overrides=overrides, execution_mode=self.settings.execution_mode)
        if wake is not None:
            services._closers.append(wake.aclose)
        self.services = services
        async with self.sm() as s, s.begin():
            workers = await services.registry.register_from_config(s, self.config.hosts, self.config.capabilities)
        recovered = await services.resources.recover()
        monitor = OfflineMonitor(self.sm, services.registry)
        monitor.start()
        if services.disk is not None:
            await services.disk.check_once()  # admission state is known before the first tick
            services.disk.start()
        maintenance = asyncio.create_task(services.resources.run_maintenance(self._stop), name="resource-maintenance")
        self.scheduler = Scheduler(
            self.sm,
            self.config,
            services.handlers,
            services.driver,
            settings=self.scheduler_settings,
            instance_id=services.holder_id,
            services=services,
        )
        if self._stop.is_set():
            self.scheduler.stop()
        log.info(
            "scheduler started",
            extra={
                "instance": services.holder_id,
                "revision": revision,
                "workers": [w.id for w in workers],
                "kinds": services.handled_kinds,
                "execution": services.execution.mode,
                "recovered_leases": len(recovered.released),
            },
        )
        try:
            await self.scheduler.run_forever()
        finally:
            self._stop.set()
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await maintenance
            await monitor.stop()
            if services.disk is not None:
                await services.disk.stop()
            await services.aclose()
            await self.engine.dispose()
            log.info("scheduler stopped", extra={"instance": services.holder_id})
        return 0


async def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermclaw scheduler", description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--concurrency", type=int, default=None, help="parallel steps (default: settings)")
    ns = parser.parse_args(list(argv or []))
    settings = get_settings()
    configure_logging(settings.log_level, "hermclaw-scheduler", settings.log_json)
    config = get_config()
    sched = None
    if ns.concurrency:
        sched = SchedulerSettings(
            concurrency=ns.concurrency,
            poll_seconds=settings.scheduler_poll_seconds,
            step_timeout_seconds=settings.scheduler_step_timeout_seconds,
        )
    proc = SchedulerProcess(config, settings, scheduler_settings=sched)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, proc.stop)
    return await proc.run()


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - process entry point
    return asyncio.run(run(argv))


__all__ = ["SchedulerProcess", "check_database", "main", "run"]
