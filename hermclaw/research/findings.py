"""Research results of a job's research steps as worker constraints (P12 → P17).

A completed research step stores a compact, cited summary (``Step.result["worker_summary"]``). Work steps that depend
on it – directly or transitively – receive these summaries as additional *constraints* of their step contract, so the
coder works from cited research instead of guessing (Bauplan §23: "keine unsichtbaren Quellen").
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import StepKind
from hermclaw.persistence.models import Step, StepDependency

RESEARCH_KIND = StepKind.research.value


async def research_findings(
    sessionmaker: async_sessionmaker[AsyncSession], step_id: uuid.UUID, *, limit: int = 3, max_chars: int = 2400
) -> list[str]:
    """Cited summaries of the completed research steps ``step_id`` (transitively) depends on, nearest first."""
    async with sessionmaker() as s:
        job_id = (await s.execute(select(Step.job_id).where(Step.id == step_id))).scalar_one_or_none()
        if job_id is None:
            return []
        rows = (await s.execute(select(Step).where(Step.job_id == job_id, Step.superseded.is_(False)))).scalars().all()
        ids = [r.id for r in rows]
        edges = (await s.execute(select(StepDependency).where(StepDependency.step_id.in_(ids)))).scalars().all()
    by_id = {r.id: r for r in rows}
    deps: dict[uuid.UUID, list[uuid.UUID]] = {}
    for e in edges:
        deps.setdefault(e.step_id, []).append(e.depends_on_step_id)
    # breadth-first over the dependency graph: nearest research first, every step once (cycles are impossible by
    # plan validation, the visited set keeps this safe regardless)
    out: list[str] = []
    seen: set[uuid.UUID] = {step_id}
    frontier = sorted(deps.get(step_id, []), key=lambda i: by_id[i].step_key if i in by_id else "")
    while frontier and len(out) < limit:
        nxt: list[uuid.UUID] = []
        for sid in frontier:
            if sid in seen:
                continue
            seen.add(sid)
            row = by_id.get(sid)
            if row is None:
                continue
            if row.kind == RESEARCH_KIND and row.status == "completed":
                summary = str((row.result or {}).get("worker_summary") or "").strip()
                if summary:
                    text = f"Research {row.step_key} (use these cited findings; do not contradict them without evidence): {summary}"
                    out.append(text if len(text) <= max_chars else text[: max_chars - 1] + "…")
                    if len(out) >= limit:
                        break
            nxt.extend(sorted(deps.get(sid, []), key=lambda i: by_id[i].step_key if i in by_id else ""))
        frontier = nxt
    return out


__all__ = ["RESEARCH_KIND", "research_findings"]
