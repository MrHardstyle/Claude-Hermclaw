"""Learning after jobs (D-019 part 3): refusals with what helped, stalls with their cause, failed jobs and research
become knowledge; an incident is activated by evidence, a refusal no model gets past becomes a tool-bug suspicion."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update

from hermclaw.contracts.events import EventType
from hermclaw.core.config import MemoryPolicy
from hermclaw.memory.learner import LEARNED_KEY, MemoryLearner, helped_after
from hermclaw.memory.store import MemoryStore
from hermclaw.persistence.models import (
    BugRecord,
    Event,
    Job,
    MemoryEntry,
    ModelInvocation,
    Repository,
    ResearchClaim,
    ResearchRun,
    ResearchSource,
    Step,
    StepAttempt,
    ToolCallRow,
)
from tests.integration.test_repo_intelligence_support import HashingEmbedder

SM = Any


@pytest.fixture
async def learner(sessionmaker: SM) -> MemoryLearner:
    async with sessionmaker() as s:
        await s.execute(delete(MemoryEntry))
        await s.execute(delete(BugRecord).where(BugRecord.bug_key.like("AUTO-%")))
        # jobs of other tests are not this test's evidence
        await s.execute(update(Job).values(metadata_=Job.metadata_.op("||")(func.jsonb_build_object(LEARNED_KEY, "earlier"))))
        await s.commit()
    policy = MemoryPolicy(min_score=0.2, promote_after=2, learn_since_days=0)
    return MemoryLearner(sessionmaker, MemoryStore(sessionmaker, HashingEmbedder(), policy), policy)


async def _job(
    sessionmaker: SM,
    *,
    model: str,
    calls: list[tuple[str, str | None, str]],
    status: str = "succeeded",
    error: str | None = None,
    model_in_turns: bool = False,
    repository_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A finished job with one step and one attempt; ``calls`` = (tool, error code, path) in turn order.
    ``model_in_turns``: the model is only known from the coder turns (attempts before BUG-307)."""
    async with sessionmaker() as s:
        job = Job(
            title="learn",
            prompt="p",
            status=status,
            error_code=error,
            error_message="checks failed" if error else None,
            repository_id=repository_id,
        )
        s.add(job)
        await s.flush()
        step = Step(job_id=job.id, step_key="S001", title="Write the parser", kind="implement", capability="code", goal="g", status=status)
        s.add(step)
        await s.flush()
        attempt = StepAttempt(step_id=step.id, job_id=job.id, attempt_no=1, model_alias=None if model_in_turns else model, status=status)
        s.add(attempt)
        await s.flush()
        if model_in_turns:
            s.add(
                ModelInvocation(
                    job_id=job.id, step_id=step.id, attempt_id=attempt.id, alias=model, model=model, role="coder", purpose="coder_turn"
                )
            )
        for turn, (tool, code, path) in enumerate(calls, start=1):
            s.add(
                ToolCallRow(
                    job_id=job.id,
                    step_id=step.id,
                    attempt_id=attempt.id,
                    turn=turn,
                    tool=tool,
                    arguments={"path": path},
                    status="refused" if code else "succeeded",
                    error_code=code,
                    result_summary=f"error {code}: old text not found in {path}" if code else "ok",
                )
            )
        await s.commit()
        return job.id


REFUSED_THEN_READ = [
    ("replace_text", "TEXT_NOT_FOUND", "app.py"),
    ("replace_text", "TEXT_NOT_FOUND", "app.py"),
    ("read_file", None, "app.py"),
    ("replace_text", None, "app.py"),
]
REFUSED_ONLY = [("replace_text", "TEXT_NOT_FOUND", "app.py")] * 3


async def _entries(sessionmaker: SM, kind: str) -> list[MemoryEntry]:
    async with sessionmaker() as s:
        return list((await s.execute(select(MemoryEntry).where(MemoryEntry.kind == kind))).scalars())


def test_what_helped_is_the_way_to_the_next_success_on_the_same_file() -> None:
    def row(tool: str, code: str | None, path: str) -> ToolCallRow:
        return ToolCallRow(tool=tool, error_code=code, status="refused" if code else "succeeded", arguments={"path": path})

    calls = [row(*c) for c in REFUSED_THEN_READ]
    assert helped_after(calls, "replace_text", "TEXT_NOT_FOUND") == "read_file → replace_text"
    other_file = [*calls[:3], row("replace_text", None, "other.py")]
    assert helped_after(other_file, "replace_text", "TEXT_NOT_FOUND") is None


async def test_a_refusal_seen_in_two_jobs_with_a_way_past_it_becomes_active(sessionmaker: SM, learner: MemoryLearner) -> None:
    first = await _job(sessionmaker, model="coder-main", calls=REFUSED_THEN_READ)
    await _job(sessionmaker, model="coder-main", calls=[("run_command", "COMMAND_REPEATED", ".")])  # once: no incident
    report = await learner.run_once()
    assert report.jobs == 2 and report.incidents == 1 and report.promoted == 0
    [entry] = await _entries(sessionmaker, "incident")
    assert entry.signature == "tool:replace_text:TEXT_NOT_FOUND" and entry.status == "candidate"
    assert "What helped: read_file → replace_text" in entry.content

    await _job(sessionmaker, model="coder-main", calls=REFUSED_THEN_READ)
    report = await learner.run_once()
    assert report.jobs == 1 and report.promoted == 1  # the first job is not read again
    [entry] = await _entries(sessionmaker, "incident")
    assert entry.status == "active" and entry.occurrences == 2 and "in 2 job(s)" in entry.content
    async with sessionmaker() as s:
        job = await s.get(Job, first)
        assert job is not None and LEARNED_KEY in job.metadata_


async def test_operator_text_and_a_retired_entry_are_kept(sessionmaker: SM, learner: MemoryLearner) -> None:
    await _job(sessionmaker, model="coder-main", calls=REFUSED_THEN_READ)
    await learner.run_once()
    [entry] = await _entries(sessionmaker, "incident")
    await learner.store.update(entry.id, content="operator text", source="operator", status="retired")
    await _job(sessionmaker, model="coder-main", calls=REFUSED_THEN_READ)
    await learner.run_once()
    [entry] = await _entries(sessionmaker, "incident")
    assert (entry.content, entry.status, entry.occurrences) == ("operator text", "retired", 2)


async def test_a_refusal_no_model_gets_past_is_a_tool_bug_suspicion(sessionmaker: SM, learner: MemoryLearner) -> None:
    await _job(sessionmaker, model="coder-main", calls=REFUSED_ONLY, status="failed", error="STEP_FAILED")
    await _job(sessionmaker, model="coder-glm47-flash", calls=REFUSED_ONLY, status="failed", error="STEP_FAILED")
    report = await learner.run_once()
    assert report.suspicions == 1 and report.promoted == 0
    [suspicion] = await _entries(sessionmaker, "tool_bug")
    assert suspicion.status == "candidate" and "coder-glm47-flash, coder-main" in suspicion.content
    [failed] = [e for e in await _entries(sessionmaker, "incident") if e.signature == "job:STEP_FAILED"]
    assert failed.occurrences == 2
    async with sessionmaker() as s:
        [bug] = (await s.execute(select(BugRecord).where(BugRecord.bug_key.like("AUTO-%")))).scalars().all()
    assert bug.status == "open" and bug.component == "tools" and "TEXT_NOT_FOUND" in bug.title

    await _job(sessionmaker, model="coder-ornith", calls=REFUSED_ONLY)
    await learner.run_once()
    assert len(await _entries(sessionmaker, "tool_bug")) == 1  # one suspicion and one bug per signature
    async with sessionmaker() as s:
        assert len((await s.execute(select(BugRecord).where(BugRecord.bug_key.like("AUTO-%")))).scalars().all()) == 1


async def test_stalls_are_documented_with_their_cause_and_research_is_kept(sessionmaker: SM, learner: MemoryLearner) -> None:
    job_id = await _job(sessionmaker, model="coder-agents-a1", calls=[])
    async with sessionmaker() as s:
        step = (await s.execute(select(Step).where(Step.job_id == job_id))).scalar_one()
        attempt = (await s.execute(select(StepAttempt).where(StepAttempt.job_id == job_id))).scalar_one()
        for directive in ("notice", "diagnose", "diagnose"):
            s.add(
                Event(
                    job_id=job_id,
                    step_id=step.id,
                    attempt_id=attempt.id,
                    source_type="stagnation",
                    event_type=EventType.STAGNATION_DETECTED,
                    payload={
                        "turn": 9,
                        "cause": "edit_mechanics",
                        "directive": directive,
                        "reasons": ["replace_text failed 3 times"],
                        "signals": [
                            {"kind": "tool_error", "tool": "replace_text", "error_code": "TEXT_NOT_FOUND", "count": 3, "label": "x"}
                        ],
                    },
                )
            )
        run = ResearchRun(job_id=job_id, question="How does PowerShell serialise a single object?", status="completed")
        run.synthesis = "ConvertTo-Json writes one object as an object, not an array; use -AsArray."
        s.add(run)
        await s.flush()
        s.add(ResearchClaim(research_run_id=run.id, claim="-AsArray forces an array", confidence=0.9))
        s.add(ResearchSource(research_run_id=run.id, url="https://learn.microsoft.com/powershell", domain="learn.microsoft.com"))
        await s.commit()
    report = await learner.run_once()
    assert report.stalls == 1 and report.research == 1  # the notice is not a stall, the second diagnosis no new example
    [stall] = [e for e in await _entries(sessionmaker, "incident") if e.signature and e.signature.startswith("stall:")]
    assert stall.signature == "stall:edit_mechanics:replace_text:TEXT_NOT_FOUND"
    assert 'step "Write the parser", model coder-agents-a1, turn 9: replace_text failed 3 times' in stall.content
    assert stall.evidence["examples"][0]["outcome"] == "succeeded"
    [research] = await _entries(sessionmaker, "research")
    assert research.status == "active" and research.source == "research" and research.expires_at is not None
    assert "-AsArray forces an array" in research.content and "https://learn.microsoft.com/powershell" in research.content


async def test_red_test_runs_are_work_in_progress_not_refusals(sessionmaker: SM, learner: MemoryLearner) -> None:
    """BUG-306: failed test and command runs counted as refusals of run_test; two models with red runs would have made
    a tool-bug suspicion against run_test."""
    red = [("run_test", "TESTS_FAILED", "."), ("run_command", "COMMAND_FAILED", "."), ("run_test", "TESTS_FAILED", ".")] * 2
    await _job(sessionmaker, model="coder-main", calls=red, status="failed", error="STEP_FAILED")
    await _job(sessionmaker, model="coder-glm47-flash", calls=red, status="failed", error="STEP_FAILED")
    report = await learner.run_once()
    assert report.suspicions == 0 and await _entries(sessionmaker, "tool_bug") == []
    assert [e.signature for e in await _entries(sessionmaker, "incident")] == ["job:STEP_FAILED"]


async def test_models_are_known_from_the_coder_turns_and_one_way_past_in_three_is_no_lesson(
    sessionmaker: SM, learner: MemoryLearner
) -> None:
    """BUG-307 (dry run against the production jobs): attempts never carried their model, so no tool-bug suspicion could
    name two models; an incident got active when one of five cases got past it."""
    await _job(sessionmaker, model="coder-main", calls=REFUSED_ONLY, model_in_turns=True)
    await _job(sessionmaker, model="coder-glm47-flash", calls=REFUSED_ONLY, model_in_turns=True)
    await _job(sessionmaker, model="coder-main", calls=REFUSED_THEN_READ, model_in_turns=True)
    report = await learner.run_once()
    assert report.suspicions == 1 and report.promoted == 0
    [incident] = await _entries(sessionmaker, "incident")
    assert incident.status == "candidate" and "(1 of 3 recent cases)" in incident.content
    await _job(sessionmaker, model="coder-main", calls=REFUSED_THEN_READ)
    await learner.run_once()
    [incident] = await _entries(sessionmaker, "incident")
    assert incident.status == "active"  # two of four


async def test_research_on_a_repository_stays_with_that_repository(sessionmaker: SM, learner: MemoryLearner) -> None:
    """BUG-307: "which framework does this repository use" was kept as knowledge for every job."""
    async with sessionmaker() as s:
        repo = Repository(name=f"tasks-{uuid.uuid4().hex[:6]}", url="https://example.invalid/tasks.git")
        s.add(repo)
        await s.flush()
        repo_id, name = repo.id, repo.name
        await s.commit()
    job_id = await _job(sessionmaker, model="coder-main", calls=[], repository_id=repo_id)
    async with sessionmaker() as s:
        run = ResearchRun(job_id=job_id, question="Which web framework does this repository use?", status="completed")
        run.synthesis = "The repository uses the standard library http.server."
        s.add(run)
        await s.commit()
    await learner.run_once()
    [research] = await _entries(sessionmaker, "research")
    assert research.scope == f"repo:{name}"
    assert await learner.store.search("web framework repository http.server", scopes=[]) == []


async def test_a_guard_refusal_is_no_tool_bug_suspicion(sessionmaker: SM, learner: MemoryLearner) -> None:
    """BUG-307: models stuck on COMMAND_REPEATED raised a suspicion against run_command although the guard did its job."""
    repeated = [("run_command", "COMMAND_REPEATED", ".")] * 3
    await _job(sessionmaker, model="coder-main", calls=repeated, model_in_turns=True)
    await _job(sessionmaker, model="coder-glm47-flash", calls=repeated, model_in_turns=True)
    report = await learner.run_once()
    assert report.incidents == 2 and report.suspicions == 0
