"""Research results of a job's research steps as worker constraints (P12 → P17).

A completed research step stores a compact, cited summary (``Step.result["worker_summary"]``) and its concrete facts
(``Step.result["facts"]``: endpoints, headers, field names first). Work steps that depend on it – directly or
transitively – receive them as additional *constraints* of their step contract, and the review of such a step gets
them as RESEARCH FACTS, so the coder works from cited research instead of guessing (Bauplan §23: "keine unsichtbaren
Quellen"). Live G1 on Strata (BUG-345): the research named ``PVEAPIToken=USER@REALM!TOKENID=UUID``, ``x-api-key`` and
the storage field ``storage``, but the summary was cut at 2400 characters and flattened into one line, the final
review never saw it, and the delivered collector got all three wrong.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import StepKind
from hermclaw.contracts.research import ResearchContract
from hermclaw.persistence.models import Step, StepDependency

RESEARCH_KIND = StepKind.research.value
RESEARCH_PREFIX = "Research "  # every research constraint starts with it (the review shows them as RESEARCH FACTS)
MAX_FACTS = 24
# what makes a fact concrete enough to be checked in code: quoted or back-ticked literals, URL paths, key=value,
# header-like or code-like identifiers (x-api-key, max_cpu, maxCpu, API_TOKEN), {placeholders}
_CONCRETE = re.compile(
    r"'[^'\s][^']{0,80}'|\"[^\"\s][^\"]{0,80}\"|`[^`]+`|(?<![\w.])/[\w.{}-]+(?:/[\w.{}-]+)+|\b\w+=\S+"
    r"|\b[a-z]+(?:-[a-z0-9]+)+\b|\b[a-z]+_[a-z0-9_]+\b|\b[a-z]+[A-Z]\w*\b|\b[A-Z]{2,}_[A-Z0-9_]+\b|\{\w+\}"
)


def concrete_facts(contract: ResearchContract, *, limit: int = MAX_FACTS) -> list[str]:
    """The research claims with their source numbers, the concrete ones (literals, paths, field names) first and in
    their order of confidence, then the rest – the part a summary limit must not cut."""
    by_id = {s.source_id: n + 1 for n, s in enumerate(contract.sources)}
    scored: list[tuple[int, int, str]] = []
    for i, claim in enumerate(contract.claims):
        refs = ",".join(f"S{by_id[s]}" for s in claim.source_ids if s in by_id)
        concrete = len(set(_CONCRETE.findall(claim.claim)))
        scored.append((-min(concrete, 3), i, f"{claim.claim.strip()} ({refs})" if refs else claim.claim.strip()))
    return [text for _, _, text in sorted(scored)[:limit]]


def fact_sources(contract: ResearchContract) -> list[str]:
    """``S<n> <url>`` of the research sources, numbered as in ``concrete_facts``."""
    return [f"S{n} {src.url}" for n, src in enumerate(contract.sources, start=1)]


def facts_block(step_key: str, title: str, facts: Sequence[str], max_chars: int, sources: Sequence[str] = ()) -> str:
    """One research step's facts as a multi-line constraint, cut at a fact boundary, with its source URLs."""
    head = (
        f"{RESEARCH_PREFIX}{step_key} ({title.strip()[:160]}) – binding facts from the cited documentation: use endpoints, "
        "auth headers, field names and formats exactly as stated; code must not contradict them, and test fixtures copy "
        "the documented fields and response shapes, never shapes taken from the code itself:"
    )
    lines = [head]
    used = len(head)
    for fact in facts:
        line = f"- {fact}"
        if used + 1 + len(line) > max_chars:
            lines.append(f"(+{len(facts) - len(lines) + 1} more facts not shown)")
            break
        lines.append(line)
        used += 1 + len(line)
    cited = "Sources: " + ", ".join(sources)
    if sources and used + 1 + len(cited) <= max_chars:
        lines.append(cited)
    return "\n".join(lines)


async def research_findings(
    sessionmaker: async_sessionmaker[AsyncSession], step_id: uuid.UUID, *, limit: int = 3, max_chars: int = 3600
) -> list[str]:
    """Facts (or, for results without them, cited summaries) of the completed research steps ``step_id``
    (transitively) depends on, nearest first."""
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
                facts = [str(f) for f in (row.result or {}).get("facts") or [] if str(f).strip()]
                summary = str((row.result or {}).get("worker_summary") or "").strip()
                if facts:
                    sources = [str(s) for s in (row.result or {}).get("fact_sources") or []]
                    out.append(facts_block(row.step_key, row.title or "", facts, max_chars, sources))
                    if len(out) >= limit:
                        break
                elif summary:
                    text = f"Research {row.step_key} (use these cited findings; do not contradict them without evidence): {summary}"
                    out.append(text if len(text) <= max_chars else text[: max_chars - 1] + "…")
                    if len(out) >= limit:
                        break
            nxt.extend(sorted(deps.get(sid, []), key=lambda i: by_id[i].step_key if i in by_id else ""))
        frontier = nxt
    return out


__all__ = ["MAX_FACTS", "RESEARCH_KIND", "RESEARCH_PREFIX", "concrete_facts", "fact_sources", "facts_block", "research_findings"]
