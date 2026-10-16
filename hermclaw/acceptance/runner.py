"""Live job battery: submit each job of a specification, wait for it, measure it (analysis 2026-10-16, §5).

Specification (YAML)::

    repository: hermclaw-test          # default repository of every job
    defaults: {max_minutes: 45, max_replans: 1, max_first_plan_steps: 3}
    jobs:
      - id: J1
        title: endpoint with test
        prompt: "..."
        base_branch: acceptance/empty   # optional
        expect: {status: [succeeded]}   # overrides the defaults per job

The runner only submits through the normal job path and reads the persisted record: it never steers a job.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.acceptance.job_metrics import Criterion, Expectation, JobMetrics, collect, evaluate, is_terminal
from hermclaw.persistence.models import Job

CreateJob = Callable[[dict[str, Any]], Awaitable[uuid.UUID]]
_EXPECTATION_FIELDS = set(Expectation.__dataclass_fields__)


@dataclass
class JobSpec:
    id: str
    title: str
    prompt: str
    repository: str | None
    base_branch: str | None
    constraints: list[str]
    expect: Expectation
    timeout_minutes: float


@dataclass
class JobOutcome:
    spec: JobSpec
    job_id: str
    metrics: JobMetrics | None
    criteria: list[Criterion] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and bool(self.criteria) and all(c.passed for c in self.criteria)


def _expectation(raw: dict[str, Any]) -> Expectation:
    unknown = set(raw) - _EXPECTATION_FIELDS
    if unknown:
        raise ValueError(f"unknown expectation field(s): {', '.join(sorted(unknown))}")
    data = dict(raw)
    if "status" in data:
        data["status"] = tuple([data["status"]] if isinstance(data["status"], str) else data["status"])
    return Expectation(**data)


def load_spec(path: Path) -> list[JobSpec]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = dict(doc.get("defaults") or {})
    timeout_default = float(defaults.pop("timeout_minutes", 120))
    out: list[JobSpec] = []
    for raw in doc.get("jobs") or []:
        expect = _expectation({**defaults, **(raw.get("expect") or {})})
        out.append(
            JobSpec(
                id=str(raw["id"]),
                title=str(raw.get("title") or raw["id"]),
                prompt=str(raw["prompt"]),
                repository=raw.get("repository", doc.get("repository")),
                base_branch=raw.get("base_branch"),
                constraints=[str(c) for c in raw.get("constraints") or []],
                expect=expect,
                timeout_minutes=float(raw.get("timeout_minutes", timeout_default)),
            )
        )
    if not out:
        raise ValueError(f"{path}: no jobs")
    return out


async def wait_terminal(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID, *, timeout_s: float, poll_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while True:
        async with sm() as s:
            job = await s.get(Job, job_id)
            status = job.status if job else "missing"
        if is_terminal(status) or status == "missing" or time.monotonic() >= deadline:
            return status
        await asyncio.sleep(poll_s)


async def run_battery(
    sm: async_sessionmaker[AsyncSession],
    specs: list[JobSpec],
    create: CreateJob,
    *,
    poll_s: float = 30.0,
    log: Callable[[str], None] = print,
) -> list[JobOutcome]:
    outcomes: list[JobOutcome] = []
    for spec in specs:
        job_id = await create(
            {
                "prompt": spec.prompt,
                "title": f"[acceptance {spec.id}] {spec.title}",
                "repo": spec.repository,
                "base_branch": spec.base_branch,
                "constraints": spec.constraints,
            }
        )
        log(f"{spec.id}: job {job_id} submitted")
        status = await wait_terminal(sm, job_id, timeout_s=spec.timeout_minutes * 60, poll_s=poll_s)
        if not is_terminal(status):
            outcomes.append(JobOutcome(spec, str(job_id), None, error=f"not finished after {spec.timeout_minutes:g} min (status {status})"))
            log(f"{spec.id}: TIMEOUT ({status})")
            continue
        metrics = await collect(sm, job_id)
        outcome = JobOutcome(spec, str(job_id), metrics, evaluate(metrics, spec.expect))
        outcomes.append(outcome)
        log(f"{spec.id}: {'PASS' if outcome.passed else 'FAIL'} ({metrics.status}, {metrics.duration_minutes} min)")
    return outcomes


def report(outcomes: list[JobOutcome]) -> dict[str, Any]:
    return {
        "passed": bool(outcomes) and all(o.passed for o in outcomes),
        "jobs": [
            {
                "id": o.spec.id,
                "title": o.spec.title,
                "job_id": o.job_id,
                "passed": o.passed,
                "error": o.error,
                "criteria": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in o.criteria],
                "metrics": o.metrics.as_dict() if o.metrics else None,
            }
            for o in outcomes
        ],
    }


def render_markdown(rep: dict[str, Any]) -> str:
    lines = [f"# Live job acceptance: {'PASS' if rep['passed'] else 'FAIL'}", ""]
    for j in rep["jobs"]:
        lines.append(f"## {j['id']} – {j['title']}: {'PASS' if j['passed'] else 'FAIL'} (job `{j['job_id']}`)")
        if j["error"]:
            lines.append(f"- error: {j['error']}")
        lines += [f"- [{'x' if c['passed'] else ' '}] {c['name']}: {c['detail']}" for c in j["criteria"]]
        lines.append("")
    return "\n".join(lines)


def write_report(rep: dict[str, Any], out_dir: Path, stamp: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    js, md = out_dir / f"{stamp}-jobs.json", out_dir / f"{stamp}-jobs.md"
    js.write_text(json.dumps(rep, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    md.write_text(render_markdown(rep), encoding="utf-8")
    return js, md
