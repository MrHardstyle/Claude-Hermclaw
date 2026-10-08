"""Gemma planner (P14, Bauplan §3.2, §15).

``Planner.create_plan(job_id, inputs)`` produces the first plan version of a job:

1. load the job, refuse a second initial plan (``PLAN_EXISTS``), build the budgeted planner input (§15 keys) and
   the system prompt, emit ``planner.invoked``;
2. call the model alias of role ``planner`` with the PlanContract JSON schema (structured output, 14.2);
3. validate: PlanContract schema (14.3), semantic rules (14.5/14.6), then deterministic enrichment – research
   steps (14.9), acceptance generation (14.8), risk floor (14.7). Any error goes back to the model as a repair
   turn with the exact error list (14.4), at most ``max_repair_attempts`` (2) times in total;
4. persist plan, plan version 1, steps and dependencies in one transaction; emit ``planner.plan.created`` and
   ``step.created`` (and ``planner.fallback.used`` if the gateway served the 12B technical fallback).

No database transaction is held while the model runs. Job state transitions belong to the orchestrator.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.plan import PlanContract
from hermclaw.core.config import HermclawConfig, ModelProfileConfig, get_config
from hermclaw.core.errors import HermclawError
from hermclaw.core.logging import get_logger
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext, ChatModel
from hermclaw.persistence.models import Job, Plan
from hermclaw.planner.enrich import EnrichmentResult, RiskPolicy, enrich_plan
from hermclaw.planner.errors import PLAN_EXISTS, PlanConflict, PlanInvalid
from hermclaw.planner.inputs import PlannerInput, PlannerSettings
from hermclaw.planner.loop import AttemptRecord, LoopOutcome, ModelCall, run_structured_loop
from hermclaw.planner.parsing import validate_schema
from hermclaw.planner.persist import (
    FALLBACK_METADATA_KEY,
    SOURCE_TYPE,
    PlanResult,
    insert_version,
    job_constraints,
    job_document,
    load_job,
    load_plan,
    lock_job,
    materialize_steps,
    merge_job_metadata,
)
from hermclaw.planner.prompt import (
    PromptBudget,
    PromptBuild,
    build_messages,
    capability_section,
    fit_to_budget,
    planner_system_prompt,
    planner_user_payload,
)
from hermclaw.planner.validation import ValidationContext, semantic_errors

log = get_logger(__name__)
PLANNER_ROLE = "planner"


def plan_json_schema() -> dict[str, Any]:
    return PlanContract.model_json_schema()


class PlannerBase:
    """Shared wiring of planner and replanner (model profile, events, failure reporting)."""

    actor = "planner"

    def __init__(
        self,
        chat: ChatModel,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig | None = None,
        *,
        settings: PlannerSettings | None = None,
    ) -> None:
        self.chat = chat
        self.sessionmaker = sessionmaker
        self.config = config or get_config()
        self.settings = settings or PlannerSettings()

    @property
    def profile(self) -> ModelProfileConfig:
        return self.config.models.by_role(PLANNER_ROLE)

    def model_call(self, purpose: str, json_schema: dict[str, Any]) -> ModelCall:
        profile = self.profile
        return ModelCall(
            alias=profile.alias,
            json_schema=json_schema,
            max_tokens=profile.max_output_tokens,
            temperature=profile.temperature,
            timeout_seconds=float(profile.timeout_seconds),
            purpose=purpose,
        )

    def budget(self, system_chars: int) -> PromptBudget:
        return PromptBudget.for_profile(self.profile, self.settings, system_chars=system_chars)

    def capabilities_for_prompt(self, vctx: ValidationContext) -> list[dict[str, Any]]:
        caps = [c for c in self.config.capabilities.capabilities if c.name in set(vctx.kind_capability.values())]
        return capability_section(caps, vctx.kind_capability)

    async def emit(
        self,
        event_type: str,
        job_id: uuid.UUID,
        payload: dict[str, Any],
        *,
        severity: Severity = Severity.info,
        session: AsyncSession | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if session is not None:
            await append_event(
                session,
                event_type,
                source_type=SOURCE_TYPE,
                source_id=self.actor,
                job_id=job_id,
                severity=severity,
                payload=payload,
                duration_ms=duration_ms,
            )
            return
        async with self.sessionmaker() as s:
            await append_event(
                s,
                event_type,
                source_type=SOURCE_TYPE,
                source_id=self.actor,
                job_id=job_id,
                severity=severity,
                payload=payload,
                duration_ms=duration_ms,
            )
            await s.commit()

    async def report_failure(self, job_id: uuid.UUID, exc: BaseException, *, mode: str) -> None:
        """``planner.failed`` with the error code and (for invalid output) the validation history – never content."""
        payload: dict[str, Any] = {"mode": mode}
        if isinstance(exc, HermclawError):
            payload |= {"error_code": exc.code, "message": exc.message[:2000]}
            for key in ("attempts", "repair_attempts", "last_errors", "history", "fallback_used", "reason_code"):
                if key in exc.details:
                    payload[key] = exc.details[key]
        else:
            payload |= {"error_code": "PLANNER_ERROR", "message": f"{type(exc).__name__}: {str(exc)[:1000]}"}
        try:
            await self.emit(EventType.PLANNER_FAILED, job_id, payload, severity=Severity.error)
        except Exception:  # pragma: no cover - event store down; the original error is what matters
            log.exception("planner.failed event could not be written", extra={"job_id": str(job_id)})

    async def record_repair(self, job_id: uuid.UUID, mode: str, record: AttemptRecord, remaining: int) -> None:
        await self.emit(
            EventType.PLANNER_REPAIR,
            job_id,
            {
                "mode": mode,
                "attempt": record.attempt,
                "phase": record.phase,
                "errors": list(record.errors),
                "repairs_remaining_after": remaining,
                "alias": record.alias,
                "fallback_used": record.fallback_used,
            },
            severity=Severity.warning,
        )

    async def record_fallback(self, session: AsyncSession, job: Job, outcome: LoopOutcome[Any], *, mode: str) -> None:
        aliases = sorted({a.alias for a in outcome.attempts if a.fallback_used})
        merge_job_metadata(job, {FALLBACK_METADATA_KEY: True, "planner_fallback_alias": aliases[-1] if aliases else None})
        await self.emit(
            EventType.PLANNER_FALLBACK_USED,
            job.id,
            {
                "mode": mode,
                "requested_alias": self.profile.alias,
                "served_aliases": aliases,
                "attempts": [a.attempt for a in outcome.attempts if a.fallback_used],
            },
            severity=Severity.warning,
            session=session,
        )


class Planner(PlannerBase):
    """Gemma planner: one validated, enriched, persisted plan version per call."""

    actor = "planner"

    async def create_plan(self, job_id: uuid.UUID, inputs: PlannerInput) -> PlanResult:
        risk_policy = RiskPolicy.effective(inputs.risk_policy, self.settings)
        async with self.sessionmaker() as session:
            job = await load_job(session, job_id)
            if await load_plan(session, job_id) is not None:
                raise PlanConflict(f"job {job_id} already has a plan; use the replanner", code=PLAN_EXISTS)
            job_doc = await job_document(session, job)
            constraints = await job_constraints(session, job, inputs.constraints)
            vctx = ValidationContext.build(self.config, inputs, self.settings, extra_text="\n".join([job.title, job.prompt, *constraints]))
            prompt = self.build_prompt(job_doc, inputs, constraints, vctx, risk_policy)
            await append_event(
                session,
                EventType.PLANNER_INVOKED,
                source_type=SOURCE_TYPE,
                source_id=self.actor,
                job_id=job_id,
                payload={"mode": "plan", "alias": self.profile.alias, "max_repair_attempts": self.settings.max_repair_attempts}
                | {"input": prompt.stats},
            )
            await session.commit()

        def validate(data: dict[str, Any]) -> EnrichmentResult:
            plan = validate_schema(PlanContract, data, limit=self.settings.max_errors_reported)
            errors = semantic_errors(plan, vctx, limit=self.settings.max_errors_reported)
            if errors:
                raise PlanInvalid("semantic", errors)
            return enrich_plan(plan, vctx, risk_policy)

        async def on_repair(record: AttemptRecord, remaining: int) -> None:
            await self.record_repair(job_id, "plan", record, remaining)

        try:
            outcome = await run_structured_loop(
                self.chat,
                self.model_call("planner", plan_json_schema()),
                prompt.messages,
                validate,
                ctx=CallContext(purpose="planner", job_id=job_id),
                settings=self.settings,
                on_repair=on_repair,
            )
            return await self._persist(job_id, outcome)
        except Exception as exc:
            await self.report_failure(job_id, exc, mode="plan")
            raise

    def build_prompt(
        self,
        job_doc: dict[str, Any],
        inputs: PlannerInput,
        constraints: list[str],
        vctx: ValidationContext,
        risk_policy: RiskPolicy,
    ) -> PromptBuild:
        system = planner_system_prompt(vctx.kind_capability, sorted(set(vctx.kind_capability.values())), vctx.max_steps)
        capabilities = self.capabilities_for_prompt(vctx)
        payload, stats = fit_to_budget(
            lambda b: planner_user_payload(
                job=job_doc,
                inputs=inputs,
                constraints=constraints,
                capabilities=capabilities,
                risk_policy=risk_policy.prompt_view(),
                test_command=vctx.test_command,
                budget=b,
            ),
            self.budget(len(system)),
        )
        return build_messages(system, payload, stats)

    async def _persist(self, job_id: uuid.UUID, outcome: LoopOutcome[EnrichmentResult]) -> PlanResult:
        enriched = outcome.value
        source = "fallback" if outcome.fallback_used else "planner"
        async with self.sessionmaker() as session:
            job = await lock_job(session, job_id)
            if await load_plan(session, job_id, for_update=True) is not None:
                raise PlanConflict(f"job {job_id} was planned concurrently", code=PLAN_EXISTS)
            plan_row = Plan(id=uuid.uuid4(), job_id=job_id, status="active", current_version=1)
            session.add(plan_row)
            await session.flush()
            version = await insert_version(
                session,
                plan=plan_row,
                job=job,
                version=1,
                source=source,
                model_alias=outcome.result.alias,
                contract=enriched.plan,
                validation_errors=outcome.validation_history,
                repair_attempts=outcome.repair_attempts,
                reason=None,
            )
            job.current_plan_version = 1
            job.row_version += 1
            merge_job_metadata(job, {"planner_model_alias": outcome.result.alias})
            if outcome.fallback_used:
                await self.record_fallback(session, job, outcome, mode="plan")
            await append_event(
                session,
                EventType.PLANNER_PLAN_CREATED,
                source_type=SOURCE_TYPE,
                source_id=self.actor,
                job_id=job_id,
                duration_ms=outcome.duration_ms,
                payload={
                    "plan_version": 1,
                    "source": source,
                    "model_alias": outcome.result.alias,
                    "steps": [s.id for s in enriched.plan.steps],
                    "repair_attempts": outcome.repair_attempts,
                    "fallback_used": outcome.fallback_used,
                    "research_steps": enriched.generated_research_steps,
                    "notes": enriched.notes,
                },
            )
            rows = await materialize_steps(
                session, job=job, version=version, steps=enriched.plan.steps, existing={}, config=self.config, actor=self.actor
            )
            await session.commit()
            return PlanResult(
                job_id=job_id,
                plan_id=plan_row.id,
                plan_version_id=version.id,
                version=1,
                source=source,
                model_alias=outcome.result.alias,
                plan=enriched.plan,
                step_ids={k: r.id for k, r in rows.items()},
                created_step_keys=list(rows),
                repair_attempts=outcome.repair_attempts,
                validation_errors=outcome.validation_history,
                fallback_used=outcome.fallback_used,
                notes=enriched.notes,
                duration_ms=outcome.duration_ms,
            )
