"""Persistence of plans, plan versions and materialised steps (P14/P24, Bauplan §10, §15, §16).

* ``plans`` – one row per job (``current_version`` mirrors ``jobs.current_plan_version``).
* ``plan_versions`` – immutable history: version, source (planner|replanner|fallback), model alias, the complete
  enriched plan JSON, the validation error history of the repair loop, repair attempts and the replan reason.
* ``steps`` + ``step_dependencies`` – the executable DAG of a version. New steps start ``pending``; scheduling,
  leasing and scope creation belong to other components.

All writes happen inside the caller's transaction; the job row is locked ``FOR UPDATE`` by the caller so that
planning and replanning of one job are serialised.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.events import EventType
from hermclaw.contracts.plan import PlanContract, PlanStep
from hermclaw.core.config import HermclawConfig
from hermclaw.core.errors import NotFoundError
from hermclaw.events.store import append_event
from hermclaw.persistence.models import Job, JobInput, Plan, PlanVersion, Repository, Step, StepDependency
from hermclaw.planner.errors import PlannerError

SOURCE_TYPE = "planner"
CODER_MODEL_ROLE = "coder"
FALLBACK_METADATA_KEY = "planner_model_fallback"


@dataclass
class PlanResult:
    """Outcome of a planner or replanner run (the persisted plan version and its active steps)."""

    job_id: uuid.UUID
    plan_id: uuid.UUID
    plan_version_id: uuid.UUID
    version: int
    source: str
    model_alias: str
    plan: PlanContract
    step_ids: dict[str, uuid.UUID]
    created_step_keys: list[str]
    repair_attempts: int
    validation_errors: list[dict[str, Any]]
    fallback_used: bool
    notes: list[str] = field(default_factory=list)
    preserved_step_keys: list[str] = field(default_factory=list)
    superseded_step_keys: list[str] = field(default_factory=list)
    rerun_reasons: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0


def coding_kinds(config: HermclawConfig) -> frozenset[str]:
    """Step kinds executed by the coder tool loop (their capability uses the ``coder`` model role)."""
    caps = {c.name: c for c in config.capabilities.capabilities}
    out = set()
    for kind, cap_name in config.capabilities.step_kind_capability.items():
        cap = caps.get(cap_name)
        if cap is not None and cap.model_role == CODER_MODEL_ROLE:
            out.add(kind)
    return frozenset(out)


async def lock_job(session: AsyncSession, job_id: uuid.UUID) -> Job:
    job = (await session.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None:
        raise NotFoundError(f"job {job_id} not found", details={"job_id": str(job_id)})
    return job


async def load_job(session: AsyncSession, job_id: uuid.UUID) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"job {job_id} not found", details={"job_id": str(job_id)})
    return job


async def load_plan(session: AsyncSession, job_id: uuid.UUID, *, for_update: bool = False) -> Plan | None:
    stmt = select(Plan).where(Plan.job_id == job_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalars().first()


async def load_version(session: AsyncSession, plan: Plan, version: int) -> PlanVersion | None:
    stmt = select(PlanVersion).where(PlanVersion.plan_id == plan.id, PlanVersion.version == version)
    return (await session.execute(stmt)).scalar_one_or_none()


async def job_document(session: AsyncSession, job: Job) -> dict[str, Any]:
    """The ``job`` section of the planner input (goal = the user's prompt)."""
    doc: dict[str, Any] = {"id": str(job.id), "title": job.title, "goal": job.prompt, "priority": job.priority}
    if job.base_branch:
        doc["base_branch"] = job.base_branch
    if job.repository_id is not None:
        repo = await session.get(Repository, job.repository_id)
        if repo is not None:
            doc["repository"] = repo.name
            doc["default_branch"] = repo.default_branch
    return doc


async def job_constraints(session: AsyncSession, job: Job, extra: Sequence[str]) -> list[str]:
    rows = (
        await session.execute(
            select(JobInput.content).where(JobInput.job_id == job.id, JobInput.kind == "constraint").order_by(JobInput.created_at)
        )
    ).scalars()
    out: dict[str, None] = {}
    for item in [*rows, *extra]:
        text = " ".join(str(item).split())
        if text:
            out.setdefault(text, None)
    return list(out)


async def active_steps(session: AsyncSession, job_id: uuid.UUID, *, for_update: bool = False) -> list[Step]:
    stmt = select(Step).where(Step.job_id == job_id, Step.superseded.is_(False)).order_by(Step.step_key)
    if for_update:
        stmt = stmt.with_for_update()
    return list((await session.execute(stmt)).scalars())


async def step_dependency_keys(session: AsyncSession, steps: Sequence[Step]) -> dict[uuid.UUID, list[str]]:
    """Dependency step keys of ``steps`` (by step row id)."""
    if not steps:
        return {}
    ids = [s.id for s in steps]
    rows = (
        await session.execute(
            select(StepDependency.step_id, Step.step_key)
            .join(Step, Step.id == StepDependency.depends_on_step_id)
            .where(StepDependency.step_id.in_(ids))
            .order_by(Step.step_key)
        )
    ).all()
    out: dict[uuid.UUID, list[str]] = {i: [] for i in ids}
    for step_id, key in rows:
        out[step_id].append(key)
    return out


def merge_job_metadata(job: Job, updates: dict[str, Any]) -> None:
    job.metadata_ = {**(job.metadata_ or {}), **updates}


def _step_row(job: Job, version: PlanVersion, step: PlanStep, config: HermclawConfig, coder_kinds: frozenset[str]) -> Step:
    kind = step.kind.value
    return Step(
        id=uuid.uuid4(),
        job_id=job.id,
        plan_version_id=version.id,
        step_key=step.id,
        title=step.title,
        kind=kind,
        capability=step.capability,
        goal=step.goal,
        status="pending",
        risk=step.risk.value,
        priority=job.priority,
        constraints=list(step.constraints),
        acceptance=[a.model_dump(mode="json") for a in step.acceptance],
        repo_hints=list(step.repo_hints),
        preferred_worker_capabilities=list(step.preferred_worker_capabilities),
        allowed_new_paths=list(step.allowed_new_paths),
        forbidden_paths=list(step.forbidden_paths),
        network=step.network,
        max_attempts=config.policies.correction.max_attempts_per_step,
        turn_budget=config.policies.coder.max_turns if kind in coder_kinds else 0,
        superseded=False,
        current_scope_version=None,
    )


async def insert_version(
    session: AsyncSession,
    *,
    plan: Plan,
    job: Job,
    version: int,
    source: str,
    model_alias: str,
    contract: PlanContract,
    validation_errors: list[dict[str, Any]],
    repair_attempts: int,
    reason: str | None,
) -> PlanVersion:
    row = PlanVersion(
        id=uuid.uuid4(),
        plan_id=plan.id,
        job_id=job.id,
        version=version,
        source=source,
        model_alias=model_alias,
        plan_json=contract.model_dump(mode="json"),
        validation_errors=validation_errors,
        repair_attempts=repair_attempts,
        reason=reason,
    )
    session.add(row)
    await session.flush()
    return row


async def materialize_steps(
    session: AsyncSession,
    *,
    job: Job,
    version: PlanVersion,
    steps: Sequence[PlanStep],
    existing: dict[str, Step],
    config: HermclawConfig,
    actor: str,
) -> dict[str, Step]:
    """Insert step rows + dependency edges for ``steps``; dependencies may point to ``existing`` (kept) rows."""
    coder_kinds = coding_kinds(config)
    rows: dict[str, Step] = {}
    for step in steps:
        row = _step_row(job, version, step, config, coder_kinds)
        session.add(row)
        rows[step.id] = row
    await session.flush()
    for step in steps:
        for dep in dict.fromkeys(step.depends_on):
            target = rows.get(dep) or existing.get(dep)
            if target is None:  # pragma: no cover - the merged plan is a validated DAG
                raise PlannerError(f"step {step.id} depends on unknown step {dep}", details={"step": step.id, "dependency": dep})
            session.add(StepDependency(step_id=rows[step.id].id, depends_on_step_id=target.id))
    await session.flush()
    for step in steps:
        row = rows[step.id]
        await append_event(
            session,
            EventType.STEP_CREATED,
            source_type=SOURCE_TYPE,
            source_id=actor,
            job_id=job.id,
            step_id=row.id,
            payload={
                "step_key": step.id,
                "title": step.title,
                "kind": step.kind.value,
                "capability": step.capability,
                "risk": step.risk.value,
                "depends_on": list(step.depends_on),
                "plan_version": version.version,
                "acceptance_types": [a.type for a in step.acceptance],
            },
        )
    return rows
