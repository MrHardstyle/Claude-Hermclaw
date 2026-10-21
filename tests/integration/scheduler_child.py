"""Child process for recovery tests: ``python -m tests.integration.scheduler_child`` runs the real
:class:`hermclaw.runtime.main.SchedulerProcess` with scripted models (runtime_stack.StackModels) until SIGTERM.

Arguments: ``--data-dir`` (workspaces/mirrors), ``--instance`` (stable instance id), ``--plan`` (script Gemma with
the checked plan), ``--hang-marker PATH`` + ``--hang-on coder|plan`` (block there after writing PATH).
The database comes from ``HERMCLAW_DATABASE_URL`` like in production.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from hermclaw.core.logging import configure_logging
from hermclaw.core.settings import Settings
from hermclaw.runtime.main import SchedulerProcess
from hermclaw.runtime.services import ServiceOverrides
from hermclaw.scheduler.scheduler import SchedulerSettings
from tests.integration.runtime_stack import StackModels, checked_plan, stack_config


async def _main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--instance", required=True)
    p.add_argument("--plan", action="store_true")
    p.add_argument("--hang-marker", type=Path, default=None)
    p.add_argument("--hang-on", default="coder", choices=["coder", "plan"])
    ns = p.parse_args()
    configure_logging("INFO", "scheduler-child", False)
    settings = Settings(env="test", data_dir=ns.data_dir, instance_id=ns.instance, execution_mode="local", wake_on_lan=False)
    models = StackModels([checked_plan()] if ns.plan else [], hang_marker=ns.hang_marker, hang_on=ns.hang_on)
    proc = SchedulerProcess(
        stack_config(),
        settings,
        overrides=ServiceOverrides(chat=models, research_enabled=False, gitlab=None, lease_keeper_interval=0.2),
        scheduler_settings=SchedulerSettings(
            poll_seconds=0.05, retry_backoff_seconds=(0,), step_lease_seconds=30, step_heartbeat_seconds=2
        ),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, proc.stop)
    return await proc.run()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
