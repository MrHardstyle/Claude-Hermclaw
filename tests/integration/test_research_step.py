# ruff: noqa: F811  (the imported ``server`` fixture is used as a test argument)
"""Research step handler (plan kind ``research``): the real ResearchEngine against PostgreSQL and the local fixture
HTTP server; only the models are scripted. Asserts the step outcome mapping, the cited worker summary in
``Step.result`` and that dependent work steps receive it as a constraint (research_findings)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import get_config
from hermclaw.persistence.models import Job, Step, StepAttempt, StepDependency
from hermclaw.research.errors import SearchUnavailable
from hermclaw.research.findings import research_findings
from hermclaw.research.search import DisabledSearchProvider
from hermclaw.runtime.research_step import ResearchStepHandler, research_question, wants_deep
from hermclaw.scheduler import CancelToken, StepRunContext
from tests.integration.test_research_engine import QUESTION, engine_for, happy_chat, server, static_search  # noqa: F401
from tests.integration.test_research_support import FixtureServer

pytestmark = pytest.mark.integration


async def _job(sm: async_sessionmaker[AsyncSession], *, max_attempts: int = 3) -> tuple[Job, Step, Step]:
    async with sm() as s:
        job = Job(title="research job", prompt=QUESTION, status="cancelled")  # never picked up by scheduler tests
        s.add(job)
        await s.flush()
        research = Step(
            job_id=job.id,
            step_key="S001",
            title=f"Research: {QUESTION}",
            kind="research",
            capability="research",
            goal=f"Research and answer with sources: {QUESTION} Reason: the install docs pin the interpreter",
            status="running",
            network=True,
            max_attempts=max_attempts,
        )
        work = Step(job_id=job.id, step_key="S002", title="pin python", kind="implement", capability="python", goal="Pin python")
        s.add_all([research, work])
        await s.flush()
        s.add(StepDependency(step_id=work.id, depends_on_step_id=research.id))
        await s.commit()
    return job, research, work


async def _ctx(sm: async_sessionmaker[AsyncSession], job: Job, step: Step, attempt_no: int = 1) -> StepRunContext:
    async with sm() as s:
        att = StepAttempt(step_id=step.id, job_id=job.id, attempt_no=attempt_no, kind="initial", status="running")
        s.add(att)
        await s.commit()
    return StepRunContext(
        job_id=job.id,
        step_id=step.id,
        attempt_id=att.id,
        attempt_no=attempt_no,
        attempt_kind="initial",
        step_key=step.step_key,
        kind=step.kind,
        capability=step.capability,
        sessionmaker=sm,
        config=get_config(),
        token=CancelToken(),
    )


async def _complete(sm: async_sessionmaker[AsyncSession], step_id: uuid.UUID, result: dict[str, Any]) -> None:
    async with sm() as s:
        row = await s.get(Step, step_id)
        assert row is not None
        row.status, row.result = "completed", result
        await s.commit()


def test_question_and_depth_from_the_step() -> None:
    generated = Step(goal="Research and answer with sources: Which TLS ciphers? Reason: hardening", title="x", risk="low")
    own = Step(goal="Find the current LTS of Node.js", title="Research: node", risk="high", acceptance=[])
    assert research_question(generated) == "Which TLS ciphers?" and not wants_deep(generated)
    assert research_question(own) == "Find the current LTS of Node.js" and wants_deep(own)
    assert wants_deep(Step(goal="q", title="t", risk="low", acceptance=[{"type": "research", "deep": True}]))


async def test_research_step_completes_with_cited_summary_for_dependent_steps(
    sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer
) -> None:
    job, research, work = await _job(sessionmaker)
    engine = engine_for(sessionmaker, happy_chat(), static_search(server))
    try:
        out = await ResearchStepHandler(sessionmaker, get_config(), engine=engine).run(await _ctx(sessionmaker, job, research))
    finally:
        await engine.aclose()
    assert out.outcome == "completed", (out.error_code, out.error_message)
    assert out.result["sources"] >= 2 and out.result["claims"] >= 1 and out.result["research_run_id"]
    summary = out.result["worker_summary"]
    assert "Sources:" in summary and "docs.toolkit.test" in summary and len(summary) <= 1400
    assert await research_findings(sessionmaker, work.id) == [], "not completed yet → nothing to hand on"
    await _complete(sessionmaker, research.id, out.result)
    findings = await research_findings(sessionmaker, work.id)
    assert len(findings) == 1 and findings[0].startswith("Research S001") and "docs.toolkit.test" in findings[0]
    assert await research_findings(sessionmaker, research.id) == [], "a research step does not receive itself"


class _DownSearch:
    name = "searxng"

    async def search(self, query: str, *, limit: int) -> list[Any]:
        raise SearchUnavailable("no results; engines unavailable: brave, duckduckgo")

    async def aclose(self) -> None:
        return None


async def test_search_outage_is_a_step_outage_not_missing_sources(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """BUG-238: with every search engine suspended the step reports SEARCH_UNAVAILABLE (a scheduler outage code: retried
    with backoff, attempts not used up) instead of RESEARCH_NO_SOURCES and, at the last attempt, a replan."""
    from hermclaw.scheduler.scheduler import STEP_OUTAGE_CODES

    job, research, _ = await _job(sessionmaker, max_attempts=1)
    engine = engine_for(sessionmaker, happy_chat(), _DownSearch())
    handler = ResearchStepHandler(sessionmaker, get_config(), engine=engine)
    try:
        out = await handler.run(await _ctx(sessionmaker, job, research, attempt_no=1))
    finally:
        await engine.aclose()
    assert out.outcome == "failed" and out.retryable and out.error_code == "SEARCH_UNAVAILABLE"
    assert out.error_code in STEP_OUTAGE_CODES


async def test_research_without_sources_retries_then_blocks_for_the_replanner(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    job, research, _ = await _job(sessionmaker, max_attempts=2)
    engine = engine_for(sessionmaker, happy_chat(), DisabledSearchProvider())
    handler = ResearchStepHandler(sessionmaker, get_config(), engine=engine)
    try:
        first = await handler.run(await _ctx(sessionmaker, job, research, attempt_no=1))
        last = await handler.run(await _ctx(sessionmaker, job, research, attempt_no=2))
    finally:
        await engine.aclose()
    assert first.outcome == "failed" and first.retryable and first.error_code == "RESEARCH_NO_SOURCES"
    assert last.outcome == "blocked" and last.replan_reason == "missing_dependency"
    assert last.replan_evidence["errors"][0]["code"] == "SEARCH_DISABLED" and last.replan_evidence["question"] == QUESTION
