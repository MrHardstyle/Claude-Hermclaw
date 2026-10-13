"""Scheduler process for the local soak test (P40): the real ``SchedulerProcess`` with scripted model answers per
job type (the job title names the scenario). Started by scripts/soak/run_soak.py; stops on SIGTERM.

Scenarios: ``value`` (implement → test → review on src/module.py) and ``fastapi`` (endpoint + pytest acceptance).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import signal
import uuid
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import select

from hermclaw.core.config import get_config
from hermclaw.core.logging import configure_logging
from hermclaw.core.settings import Settings
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.persistence.models import Job, Step
from hermclaw.runtime.main import SchedulerProcess
from hermclaw.runtime.services import ServiceOverrides
from hermclaw.scheduler.scheduler import SchedulerSettings
from tests.e2e.harness import done, subprocess_executor
from tests.e2e.test_e2e_code_tasks import fastapi_scenario
from tests.integration.runtime_stack import DONE, EDIT, checked_plan

T = TypeVar("T", bound=BaseModel)


def scenarios() -> dict[str, dict[str, Any]]:
    fa = fastapi_scenario()
    return {
        "value": {"plan": checked_plan(), "coder": {"S001": [EDIT, DONE]}},
        "fastapi": {"plan": fa.plans[0], "coder": fa.coder},
    }


class SoakModels:
    """Gemma/fast router/Qwen3-Coder/heavy reviewer answers by alias, per job (scenario = job title prefix)."""

    def __init__(self, sessionmaker: Any) -> None:
        self.sm = sessionmaker
        self.catalog = scenarios()
        self.queues: dict[tuple[uuid.UUID, str], list[dict[str, Any]]] = {}

    async def _job_and_step(self, ctx: CallContext) -> tuple[str, str]:
        async with self.sm() as s:
            title = (await s.execute(select(Job.title).where(Job.id == ctx.job_id))).scalar_one_or_none() or "value"
            key = "?"
            if ctx.step_id is not None:
                key = (await s.execute(select(Step.step_key).where(Step.id == ctx.step_id))).scalar_one_or_none() or "?"
        return title.split(":", 1)[0], key

    async def chat(self, alias: str, messages: list[ChatMessage], *, ctx: CallContext, **_: Any) -> ChatResult:
        name, _ = await self._job_and_step(ctx)
        plan = copy.deepcopy(self.catalog.get(name, self.catalog["value"])["plan"])
        return ChatResult(content=json.dumps(plan), alias=alias, model="scripted:gemma4:26b")

    async def structured(
        self, alias: str, messages: list[ChatMessage], schema: type[T], *, ctx: CallContext, **_: Any
    ) -> StructuredResult[T]:
        if alias == "fast-router":
            data: dict[str, Any] = {"intent": "code_change"}
        elif alias == "heavy-review":
            data = {"verdict": "pass", "summary": "ok", "findings": []}
        else:
            name, key = await self._job_and_step(ctx)
            q = self.queues.setdefault(
                (ctx.job_id, key), copy.deepcopy(self.catalog.get(name, self.catalog["value"])["coder"].get(key, []))
            )  # type: ignore[index]
            prompt = "\n".join(m.content for m in messages)
            if name == "value" and "+VALUE = 2" in prompt:
                q.clear()
            data = q.pop(0) if q else done(f"{key} complete", [])
        return StructuredResult(
            value=schema.model_validate(data), result=ChatResult(content=json.dumps(data), alias=alias, model=f"scripted:{alias}")
        )


async def _main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--instance", required=True)
    p.add_argument("--concurrency", type=int, default=3)
    ns = p.parse_args()
    configure_logging("INFO", "soak-scheduler", True)
    settings = Settings(env="test", data_dir=ns.data_dir, instance_id=ns.instance, execution_mode="local", wake_on_lan=False)
    proc = SchedulerProcess(
        get_config(),
        settings,
        overrides=ServiceOverrides(chat=None, research_enabled=False, gitlab=None, executor_factory=subprocess_executor),
    )
    proc.overrides.chat = SoakModels(proc.sm)  # needs the process' own sessionmaker
    proc.scheduler_settings = SchedulerSettings(concurrency=ns.concurrency, poll_seconds=0.2, retry_backoff_seconds=(1, 5, 15))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, proc.stop)
    return await proc.run()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
