"""Step handler for plan kind ``research`` (Bauplan §23, P12 in the runtime).

The research engine runs the full pipeline (queries → search → fetch → claims → contradictions → synthesis) and
persists runs, sources and claims itself; this handler maps the outcome onto the step lifecycle:

* ``completed`` / ``partial`` → step completed; ``Step.result`` carries the run id, counts and a compact cited
  summary (``worker_summary``) that dependent work steps receive as constraints
  (:func:`hermclaw.research.findings.research_findings`).
* ``failed`` (no usable sources – search provider down, every source unreachable) → retried with backoff while
  attempts remain; on the last attempt the step blocks with ``missing_dependency`` so the replanner (Gemma) decides
  whether the job can proceed without this research (P35.17).

Deep synthesis (planner model) is used for high/critical risk steps or when the step's acceptance asks for it.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import HermclawConfig
from hermclaw.core.errors import ValidationFailed
from hermclaw.persistence.models import Step
from hermclaw.research.engine import ResearchOutcome, format_for_worker
from hermclaw.research.text import clip
from hermclaw.runtime.transitions import emit_status
from hermclaw.scheduler.handlers import StepOutcome, StepRunContext

_GOAL_PREFIX = re.compile(r"^\s*research and answer with sources:\s*", re.IGNORECASE)
_REASON_SUFFIX = re.compile(r"\s+Reason:\s.*$", re.DOTALL)
DEEP_RISKS = frozenset({"high", "critical"})


class ResearchRunner(Protocol):
    async def run_detailed(
        self, question: str, *, job_id: uuid.UUID | None = None, step_id: uuid.UUID | None = None, deep: bool = False
    ) -> ResearchOutcome: ...


def research_question(row: Step) -> str:
    """The question of a research step: the generated goal form (``Research and answer with sources: …``) or the
    planner's own goal text."""
    goal = row.goal or ""
    if _GOAL_PREFIX.match(goal):
        return _REASON_SUFFIX.sub("", _GOAL_PREFIX.sub("", goal)).strip()
    return goal.strip() or (row.title or "").removeprefix("Research:").strip()


def wants_deep(row: Step) -> bool:
    if row.risk in DEEP_RISKS:
        return True
    return any(isinstance(a, dict) and a.get("type") == "research" and a.get("deep") for a in row.acceptance or [])


class ResearchStepHandler:
    kinds = frozenset({"research"})

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig,
        *,
        engine: ResearchRunner,
        summary_chars: int = 1400,
    ) -> None:
        self.sm, self.config, self.engine = sessionmaker, config, engine
        self.summary_chars = summary_chars

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        async with self.sm() as s:
            row = (await s.execute(select(Step).where(Step.id == ctx.step_id))).scalar_one()
        question = research_question(row)
        deep = wants_deep(row)
        async with self.sm() as s:
            await emit_status(s, ctx.job_id, f"Research {row.step_key}: {clip(question, 160)}", step_id=ctx.step_id)
            await s.commit()
        try:
            out = await self.engine.run_detailed(question, job_id=ctx.job_id, step_id=ctx.step_id, deep=deep)
        except ValidationFailed as exc:
            return StepOutcome("failed", error_code="RESEARCH_INVALID", error_message=str(exc)[:2000], retryable=False)
        contract = out.contract
        result: dict[str, Any] = {
            "research_run_id": str(out.run_id),
            "status": contract.status,
            "deep": deep,
            "queries": list(contract.queries)[:10],
            "sources": len(contract.sources),
            "claims": len(contract.claims),
            "contradictions": len(contract.contradictions),
        }
        if contract.status == "failed":
            errors = [{k: e.get(k) for k in ("stage", "code", "message")} for e in out.errors][:10]
            message = f"research found no usable sources for: {clip(question, 300)}"
            if ctx.attempt_no >= row.max_attempts:
                return StepOutcome(
                    "blocked",
                    error_code="RESEARCH_NO_SOURCES",
                    error_message=message,
                    result=result,
                    replan_reason="missing_dependency",
                    replan_evidence={"research_run_id": str(out.run_id), "question": question, "errors": errors},
                )
            return StepOutcome("failed", error_code="RESEARCH_NO_SOURCES", error_message=message, result=result, retryable=True)
        result["worker_summary"] = format_for_worker(contract, max_chars=self.summary_chars)
        result["synthesis"] = clip(contract.synthesis, 4000)
        summary = f"Research {contract.status}: {len(contract.sources)} Quellen, {len(contract.claims)} Claims"
        return StepOutcome("completed", summary=summary, result=result)


__all__ = ["DEEP_RISKS", "ResearchRunner", "ResearchStepHandler", "research_question", "wants_deep"]
