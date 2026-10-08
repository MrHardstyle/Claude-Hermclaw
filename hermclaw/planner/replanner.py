"""Gemma replanner (P24, Bauplan §16).

``Replanner.replan(job_id, trigger, inputs)``:

1. (24.1) lock the job, enforce ``policies.correction.max_replans_per_job`` (``ReplanLimitReached``), load the
   current plan version and active steps, build the failure package (goal, current plan, completed steps,
   failed step + deterministic evidence, open steps, repository facts, research evidence); emit
   ``replan.started``;
2. (24.2) call Gemma (role ``planner``) with the ReplanContract schema; validation and repair budget are the
   planner's (schema -> replan rules -> merged-DAG schema -> semantic rules -> enrichment, ≤ 2 repairs);
3. (24.3) persist a new plan version (source ``replanner``, or ``fallback`` if the gateway used the 12B fallback);
4. (24.4) completed steps stay completed and active; only a step the model lists with the completed id *and* a
   ``rerun_reason`` is re-run (old row superseded, new row pending). All other old steps are superseded: in-flight
   or pending ones are cancelled through the step state machine, failed ones keep ``failed`` as history;
5. (24.5) dependencies of new steps may reference kept completed steps – they are mapped to the existing rows;
6. (24.6) new steps have no scope yet (the scope engine creates it); active scope contracts of superseded steps
   are marked ``superseded``.

``jobs.replan_count`` is incremented and ``replan.created`` emitted in the same transaction. A concurrent change of
the plan (other version, other completed set) aborts with ``PLAN_CHANGED`` instead of overwriting.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import StepStatus
from hermclaw.contracts.events import EventType
from hermclaw.contracts.plan import PlanContract, PlanStep
from hermclaw.contracts.scope import normalise_path
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext
from hermclaw.persistence.models import Job, ScopeContractRow, Step
from hermclaw.planner.enrich import EnrichmentResult, RiskPolicy, enrich_plan
from hermclaw.planner.errors import NO_PLAN, PLAN_CHANGED, PlanConflict, PlanInvalid, ReplanLimitReached
from hermclaw.planner.failure_package import ReplanState, build_failure_package
from hermclaw.planner.inputs import PlannerInput, collect_known_paths
from hermclaw.planner.loop import AttemptRecord, LoopOutcome, run_structured_loop
from hermclaw.planner.parsing import validate_schema
from hermclaw.planner.persist import (
    SOURCE_TYPE,
    PlanResult,
    active_steps,
    insert_version,
    job_constraints,
    job_document,
    load_plan,
    load_version,
    lock_job,
    materialize_steps,
    merge_job_metadata,
    step_dependency_keys,
)
from hermclaw.planner.planner import PlannerBase
from hermclaw.planner.prompt import PromptBuild, build_messages, fit_to_budget, replan_user_payload, replanner_system_prompt
from hermclaw.planner.replan_contract import APPROACH_FAILURE_REASONS, ReplanContract, ReplanTrigger
from hermclaw.planner.validation import ValidationContext, normalise_text, semantic_errors
from hermclaw.runtime.transitions import transition_step

STEP_FINAL_STATES = frozenset({StepStatus.completed.value, StepStatus.failed.value, StepStatus.cancelled.value})


def replan_json_schema() -> dict[str, Any]:
    return ReplanContract.model_json_schema()


@dataclass
class ReplanValidated:
    enriched: EnrichmentResult
    kept_keys: frozenset[str]
    reruns: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplanSnapshot:
    """What the replan was computed against; the persist transaction refuses to apply it to anything else."""

    base_version: int
    replan_count: int
    completed_keys: frozenset[str]
    active_keys: frozenset[str]
    failed_key: str | None
    failed_signature: tuple[Any, ...] | None


def step_from_row(row: Step, deps: list[str]) -> PlanStep:
    """A persisted (completed) step as PlanStep for the merged DAG; tolerant of rows written by other components."""
    data: dict[str, Any] = {
        "id": row.step_key,
        "title": row.title,
        "kind": row.kind,
        "capability": row.capability,
        "goal": row.goal,
        "depends_on": deps,
        "repo_hints": list(row.repo_hints or []),
        "constraints": list(row.constraints or []),
        "acceptance": list(row.acceptance or []),
        "preferred_worker_capabilities": list(row.preferred_worker_capabilities or []),
        "risk": row.risk,
        "allowed_new_paths": list(row.allowed_new_paths or []),
        "forbidden_paths": list(row.forbidden_paths or []),
        "network": row.network,
    }
    try:
        return PlanStep.model_validate(data)
    except ValidationError:
        minimal = {
            "id": row.step_key,
            "title": (row.title or row.step_key)[:200].ljust(3, "."),
            "kind": row.kind,
            "capability": row.capability[:64].ljust(2, "_"),
            "goal": (row.goal or row.title or row.step_key)[:4000].ljust(5, "."),
            "depends_on": deps,
        }
        return PlanStep.model_validate(minimal)


def merge_plan(contract: ReplanContract, kept: list[PlanStep]) -> dict[str, Any]:
    """Kept completed steps (only their mutual dependencies) followed by the model's new/re-run steps."""
    kept_ids = {s.id for s in kept}
    kept_dumped = []
    for step in kept:
        dumped = step.model_dump(mode="json")
        dumped["depends_on"] = [d for d in dumped["depends_on"] if d in kept_ids]
        kept_dumped.append(dumped)
    new_dumped = [s.model_dump(mode="json", exclude={"rerun_reason"}) for s in contract.steps]
    return {
        "goal": contract.goal,
        "summary": contract.summary,
        "assumptions": list(contract.assumptions),
        "risks": list(contract.risks),
        "research_needed": [r.model_dump(mode="json") for r in contract.research_needed],
        "steps": kept_dumped + new_dumped,
    }


def work_signature(*, kind: str, capability: str, goal: str, hints: list[str], deps: list[str], acceptance: list[Any]) -> tuple[Any, ...]:
    return (
        kind,
        capability,
        normalise_text(goal),
        tuple(sorted(hints)),
        tuple(sorted(set(deps))),
        json.dumps(acceptance, sort_keys=True, default=str),
    )


def plan_step_signature(step: PlanStep) -> tuple[Any, ...]:
    return work_signature(
        kind=step.kind.value,
        capability=step.capability,
        goal=step.goal,
        hints=list(step.repo_hints),
        deps=list(step.depends_on),
        acceptance=[a.model_dump(mode="json") for a in step.acceptance],
    )


class Replanner(PlannerBase):
    """Gemma replanner: a new plan version that preserves completed work."""

    actor = "replanner"

    async def replan(self, job_id: uuid.UUID, trigger: ReplanTrigger, inputs: PlannerInput | None = None) -> PlanResult:
        inputs = inputs or PlannerInput()
        risk_policy = RiskPolicy.effective(inputs.risk_policy, self.settings)
        max_replans = self.config.policies.correction.max_replans_per_job
        try:
            async with self.sessionmaker() as session:
                job = await lock_job(session, job_id)
                if job.replan_count >= max_replans:
                    raise ReplanLimitReached(
                        f"job {job_id} reached the replan limit ({job.replan_count}/{max_replans})",
                        details={"replan_count": job.replan_count, "max_replans_per_job": max_replans, "reason_code": trigger.reason_code},
                    )
                state = await self._load_state(session, job, trigger)
                package = await build_failure_package(session, state, trigger)
                job_doc = await job_document(session, job)
                constraints = await job_constraints(session, job, inputs.constraints)
                vctx_inputs = self._inputs_with_completed_paths(inputs, state)
                extra = "\n".join(
                    [job.title, job.prompt, *constraints, trigger.detail, json.dumps(trigger.evidence, default=str)]
                    + [s.goal for s in state.completed.values()]
                )
                vctx = ValidationContext.build(self.config, vctx_inputs, self.settings, extra_text=extra)
                prompt = self.build_prompt(job_doc, inputs, constraints, vctx, risk_policy, package=package)
                snapshot = self._snapshot(job, state)
                completed_rows = sorted(state.completed.values(), key=lambda r: r.step_key)
                completed_steps = [step_from_row(row, state.dependencies.get(row.id, [])) for row in completed_rows]
                await append_event(
                    session,
                    EventType.REPLAN_STARTED,
                    source_type=SOURCE_TYPE,
                    source_id=self.actor,
                    job_id=job_id,
                    step_id=state.failed.id if state.failed is not None else None,
                    payload={
                        "reason_code": trigger.reason_code,
                        "failed_step": snapshot.failed_key,
                        "from_version": snapshot.base_version,
                        "replan_count": job.replan_count,
                        "max_replans_per_job": max_replans,
                        "completed_steps": sorted(snapshot.completed_keys),
                        "alias": self.profile.alias,
                        "input": prompt.stats,
                    },
                )
                await session.commit()

            validate = self._validator(vctx, risk_policy, snapshot, completed_steps, trigger)

            async def on_repair(record: AttemptRecord, remaining: int) -> None:
                await self.record_repair(job_id, "replan", record, remaining)

            outcome = await run_structured_loop(
                self.chat,
                self.model_call("replan", replan_json_schema()),
                prompt.messages,
                validate,
                ctx=CallContext(purpose="replan", job_id=job_id, step_id=trigger.failed_step_id),
                settings=self.settings,
                on_repair=on_repair,
            )
            return await self._persist(job_id, snapshot, outcome, trigger)
        except Exception as exc:
            await self.report_failure(job_id, exc, mode="replan")
            raise

    # ------------------------------------------------------------------------------------------- state / prompt
    async def _load_state(self, session: AsyncSession, job: Job, trigger: ReplanTrigger) -> ReplanState:
        plan = await load_plan(session, job.id, for_update=True)
        if plan is None or job.current_plan_version is None:
            raise PlanConflict(f"job {job.id} has no plan to replan", code=NO_PLAN)
        version = await load_version(session, plan, job.current_plan_version)
        if version is None:
            raise PlanConflict(f"plan version {job.current_plan_version} of job {job.id} is missing", code=NO_PLAN)
        steps = await active_steps(session, job.id, for_update=True)
        deps = await step_dependency_keys(session, steps)
        failed: Step | None = None
        if trigger.failed_step_id is not None:
            failed = next((s for s in steps if s.id == trigger.failed_step_id), None)
            if failed is None:
                raise PlanConflict(
                    f"failed step {trigger.failed_step_id} is not an active step of job {job.id}",
                    code=PLAN_CHANGED,
                    details={"failed_step_id": str(trigger.failed_step_id)},
                )
        completed = {s.step_key: s for s in steps if s.status == StepStatus.completed.value}
        return ReplanState(job=job, plan=plan, version=version, steps=steps, dependencies=deps, failed=failed, completed=completed)

    @staticmethod
    def _inputs_with_completed_paths(inputs: PlannerInput, state: ReplanState) -> PlannerInput:
        """Files created by completed steps exist now, even if the inventory passed in predates them."""
        if not collect_known_paths(inputs):
            return inputs
        extra: list[str] = []
        for row in state.completed.values():
            for raw in row.allowed_new_paths or []:
                if isinstance(raw, str) and not any(c in raw for c in "*?["):
                    try:
                        extra.append(normalise_path(raw))
                    except ValueError:
                        continue
        if not extra:
            return inputs
        return inputs.model_copy(update={"known_paths": [*inputs.known_paths, *extra]})

    def _snapshot(self, job: Job, state: ReplanState) -> ReplanSnapshot:
        failed_sig = None
        if state.failed is not None:
            f = state.failed
            failed_sig = work_signature(
                kind=f.kind,
                capability=f.capability,
                goal=f.goal,
                hints=list(f.repo_hints or []),
                deps=state.dependencies.get(f.id, []),
                acceptance=list(f.acceptance or []),
            )
        return ReplanSnapshot(
            base_version=state.version.version,
            replan_count=job.replan_count,
            completed_keys=state.completed_keys,
            active_keys=frozenset(s.step_key for s in state.steps),
            failed_key=state.failed.step_key if state.failed is not None else None,
            failed_signature=failed_sig,
        )

    def build_prompt(
        self,
        job_doc: dict[str, Any],
        inputs: PlannerInput,
        constraints: list[str],
        vctx: ValidationContext,
        risk_policy: RiskPolicy,
        *,
        package: dict[str, Any],
    ) -> PromptBuild:
        system = replanner_system_prompt(vctx.kind_capability, sorted(set(vctx.kind_capability.values())), vctx.max_steps)
        capabilities = self.capabilities_for_prompt(vctx)
        payload, stats = fit_to_budget(
            lambda b: replan_user_payload(
                job=job_doc,
                inputs=inputs,
                constraints=constraints,
                capabilities=capabilities,
                risk_policy=risk_policy.prompt_view(),
                test_command=vctx.test_command,
                package=package,
                budget=b,
            ),
            self.budget(len(system)),
        )
        return build_messages(system, payload, stats)

    # ------------------------------------------------------------------------------------------- validation
    def _validator(
        self,
        vctx: ValidationContext,
        risk_policy: RiskPolicy,
        snapshot: ReplanSnapshot,
        completed_steps: list[PlanStep],
        trigger: ReplanTrigger,
    ) -> Callable[[dict[str, Any]], ReplanValidated]:
        limit = self.settings.max_errors_reported
        completed_keys = snapshot.completed_keys

        def validate(data: dict[str, Any]) -> ReplanValidated:
            contract = validate_schema(ReplanContract, data, limit=limit)
            errors: list[str] = []
            reruns: dict[str, str] = {}
            for step in contract.steps:
                reason = (step.rerun_reason or "").strip()
                if step.id in completed_keys:
                    if reason:
                        reruns[step.id] = reason
                    else:
                        errors.append(
                            f"step {step.id}: is already completed and is never re-run without a reason; remove it "
                            f"(new steps may depend on {step.id}) or set rerun_reason"
                        )
                elif reason:
                    errors.append(
                        f"step {step.id}: rerun_reason is only allowed for completed steps "
                        f"({', '.join(sorted(completed_keys)) or 'none'}); remove it"
                    )
            if errors:
                raise PlanInvalid("replan", errors[:limit])
            kept_keys = frozenset(completed_keys - reruns.keys())
            kept = [s for s in completed_steps if s.id in kept_keys]
            merged = validate_schema(PlanContract, merge_plan(contract, kept), limit=limit)
            sem = semantic_errors(merged, vctx, preserved=kept_keys, limit=limit)
            if sem:
                raise PlanInvalid("semantic", sem)
            enriched = enrich_plan(merged, vctx, risk_policy, preserved=kept_keys, reserved_ids=set(snapshot.active_keys))
            repeat = self._repeat_errors(enriched.plan, kept_keys, snapshot, trigger)
            if repeat:
                raise PlanInvalid("replan", repeat)
            return ReplanValidated(enriched=enriched, kept_keys=kept_keys, reruns=reruns)

        return validate

    @staticmethod
    def _repeat_errors(plan: PlanContract, kept: frozenset[str], snapshot: ReplanSnapshot, trigger: ReplanTrigger) -> list[str]:
        """Reject repeating the failed step unchanged when the trigger proves that approach does not work."""
        if trigger.reason_code not in APPROACH_FAILURE_REASONS or snapshot.failed_signature is None:
            return []
        if snapshot.failed_key in snapshot.completed_keys:
            return []
        out = []
        for step in plan.steps:
            if step.id not in kept and plan_step_signature(step) == snapshot.failed_signature:
                out.append(
                    f"step {step.id}: repeats the failed step {snapshot.failed_key} unchanged although it failed with "
                    f"{trigger.reason_code}; change the approach (goal, repo_hints, dependencies or acceptance) or remove it"
                )
        return out

    # ------------------------------------------------------------------------------------------- persistence
    async def _persist(
        self, job_id: uuid.UUID, snapshot: ReplanSnapshot, outcome: LoopOutcome[ReplanValidated], trigger: ReplanTrigger
    ) -> PlanResult:
        validated = outcome.value
        enriched = validated.enriched
        source = "fallback" if outcome.fallback_used else "replanner"
        max_replans = self.config.policies.correction.max_replans_per_job
        async with self.sessionmaker() as session:
            job = await lock_job(session, job_id)
            if job.current_plan_version != snapshot.base_version or job.replan_count != snapshot.replan_count:
                raise PlanConflict(
                    f"plan of job {job_id} changed during replanning (version {job.current_plan_version}, "
                    f"expected {snapshot.base_version})",
                    code=PLAN_CHANGED,
                )
            if job.replan_count >= max_replans:  # pragma: no cover - guarded by the snapshot check above
                raise ReplanLimitReached(f"job {job_id} reached the replan limit", details={"replan_count": job.replan_count})
            plan_row = await load_plan(session, job_id, for_update=True)
            if plan_row is None:  # pragma: no cover - plans are never deleted while a job exists
                raise PlanConflict(f"job {job_id} has no plan", code=NO_PLAN)
            steps = await active_steps(session, job_id, for_update=True)
            completed_now = frozenset(s.step_key for s in steps if s.status == StepStatus.completed.value)
            if completed_now != snapshot.completed_keys:
                raise PlanConflict(
                    f"completed steps of job {job_id} changed during replanning",
                    code=PLAN_CHANGED,
                    details={"expected": sorted(snapshot.completed_keys), "actual": sorted(completed_now)},
                )
            by_key = {s.step_key: s for s in steps}
            new_version_no = plan_row.current_version + 1
            reason_text = f"superseded by plan version {new_version_no} ({trigger.reason_code})"

            superseded: list[Step] = []
            for row in steps:
                if row.step_key in validated.kept_keys:
                    continue
                row.superseded = True
                if row.status not in STEP_FINAL_STATES:
                    await transition_step(session, row, StepStatus.cancelled, reason=reason_text, actor=self.actor)
                superseded.append(row)
            await session.flush()  # free the (job_id, step_key) keys before inserting the new rows

            scopes_superseded = 0
            if superseded:
                res = await session.execute(
                    update(ScopeContractRow)
                    .where(ScopeContractRow.step_id.in_([s.id for s in superseded]), ScopeContractRow.status == "active")
                    .values(status="superseded", reason=reason_text)
                )
                scopes_superseded = int(res.rowcount or 0)  # type: ignore[attr-defined]

            version = await insert_version(
                session,
                plan=plan_row,
                job=job,
                version=new_version_no,
                source=source,
                model_alias=outcome.result.alias,
                contract=enriched.plan,
                validation_errors=outcome.validation_history,
                repair_attempts=outcome.repair_attempts,
                reason=self._reason(trigger, validated.reruns),
            )
            new_steps = [s for s in enriched.plan.steps if s.id not in validated.kept_keys]
            kept_rows = {k: by_key[k] for k in validated.kept_keys}
            rows = await materialize_steps(
                session, job=job, version=version, steps=new_steps, existing=kept_rows, config=self.config, actor=self.actor
            )
            job.replan_count += 1
            job.current_plan_version = new_version_no
            job.row_version += 1
            plan_row.current_version = new_version_no
            merge_job_metadata(
                job,
                {
                    "planner_model_alias": outcome.result.alias,
                    "last_replan": {"version": new_version_no, "reason_code": trigger.reason_code},
                },
            )
            if outcome.fallback_used:
                await self.record_fallback(session, job, outcome, mode="replan")
            superseded_keys = sorted(s.step_key for s in superseded)
            await append_event(
                session,
                EventType.REPLAN_CREATED,
                source_type=SOURCE_TYPE,
                source_id=self.actor,
                job_id=job_id,
                step_id=trigger.failed_step_id,
                duration_ms=outcome.duration_ms,
                payload={
                    "reason_code": trigger.reason_code,
                    "from_version": snapshot.base_version,
                    "to_version": new_version_no,
                    "source": source,
                    "model_alias": outcome.result.alias,
                    "replan_count": job.replan_count,
                    "preserved_steps": sorted(validated.kept_keys),
                    "rerun_steps": validated.reruns,
                    "superseded_steps": superseded_keys,
                    "new_steps": [s.id for s in new_steps],
                    "scopes_superseded": scopes_superseded,
                    "repair_attempts": outcome.repair_attempts,
                    "fallback_used": outcome.fallback_used,
                    "notes": enriched.notes,
                },
            )
            await session.commit()
            step_ids = {k: r.id for k, r in kept_rows.items()} | {k: r.id for k, r in rows.items()}
            return PlanResult(
                job_id=job_id,
                plan_id=plan_row.id,
                plan_version_id=version.id,
                version=new_version_no,
                source=source,
                model_alias=outcome.result.alias,
                plan=enriched.plan,
                step_ids=step_ids,
                created_step_keys=list(rows),
                repair_attempts=outcome.repair_attempts,
                validation_errors=outcome.validation_history,
                fallback_used=outcome.fallback_used,
                notes=enriched.notes,
                preserved_step_keys=sorted(validated.kept_keys),
                superseded_step_keys=superseded_keys,
                rerun_reasons=dict(validated.reruns),
                duration_ms=outcome.duration_ms,
            )

    @staticmethod
    def _reason(trigger: ReplanTrigger, reruns: dict[str, str]) -> str:
        text: str = str(trigger.reason_code)
        if trigger.detail.strip():
            text += f": {' '.join(trigger.detail.split())[:1000]}"
        for key, why in sorted(reruns.items()):
            text += f"\nrerun {key}: {' '.join(why.split())[:500]}"
        return text
