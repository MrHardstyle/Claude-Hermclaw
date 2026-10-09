"""P20 stagnation detection against PostgreSQL, the real coder loop/tool engine, real git and real pytest runs.

Only the coder model is scripted (test-only fake). Covers: state persistence in ``step_attempts.fingerprints``,
restart/resume, stale-write protection, prior escalations, ``stagnation.detected``/``strategy.changed`` events and the
monitor driving the coder loop to warning → forced diagnosis → stop.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, update

from hermclaw.coder import TurnObservation
from hermclaw.contracts.events import EventType
from hermclaw.contracts.tools import CoderAction, ToolName, ToolResult
from hermclaw.core.config import StagnationPolicy, get_config
from hermclaw.core.redaction import REDACTED
from hermclaw.persistence.models import Event, Job, Step, StepAttempt
from hermclaw.scheduler import CancelToken
from hermclaw.scheduler.handlers import StepRunContext
from hermclaw.stagnation.actions import LadderContext, decide
from hermclaw.stagnation.detector import Observation, SignalKind, StagnationDetector, StagnationLevel
from hermclaw.stagnation.monitor import StagnationMonitor, create_monitor, make_stagnation_factory, to_directive
from hermclaw.stagnation.persistence import (
    STATE_KEY,
    apply_decision,
    load_detector,
    load_state,
    prior_escalations,
    record_events,
    save_state,
)
from tests.integration.test_coder_loop import PY, TEST_FILE, _run, _setup, act

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _attempt(sm: Any, *, step: Step | None = None, attempt_no: int = 1, fingerprints: dict[str, Any] | None = None) -> StepAttempt:
    async with sm() as s:
        if step is None:
            job = Job(title="stagnation", prompt="p")
            s.add(job)
            await s.flush()
            step = Step(job_id=job.id, step_key="S001", title="t", kind="implement", capability="coding", goal="g")
            s.add(step)
            await s.flush()
        row = StepAttempt(step_id=step.id, job_id=step.job_id, attempt_no=attempt_no, fingerprints=fingerprints or {})
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def _step(sm: Any, step_id: uuid.UUID) -> Step:
    async with sm() as s:
        step: Step = (await s.execute(select(Step).where(Step.id == step_id))).scalar_one()
        return step


async def _events(sm: Any, attempt_id: uuid.UUID) -> list[Event]:
    async with sm() as s:
        res = await s.execute(
            select(Event).where(Event.attempt_id == attempt_id, Event.source_type == "stagnation").order_by(Event.sequence)
        )
        return list(res.scalars())


async def _fingerprints(sm: Any, attempt_id: uuid.UUID) -> dict[str, Any]:
    async with sm() as s:
        return dict((await s.execute(select(StepAttempt.fingerprints).where(StepAttempt.id == attempt_id))).scalar_one())


def read(turn: int, path: str = "app.py") -> Observation:
    return Observation(turn=turn, tool="read_file", args={"path": path})


# ------------------------------------------------------------------------------------------------- state storage
async def test_state_round_trip_merges_into_fingerprints(sessionmaker: Any) -> None:
    row = await _attempt(sessionmaker, fingerprints={"other": {"keep": 1}})
    det = StagnationDetector()
    for t in (1, 2):
        det.observe(read(t))
    async with sessionmaker() as s:
        assert await save_state(s, row.id, det)
        await s.commit()
    fp = await _fingerprints(sessionmaker, row.id)
    assert fp["other"] == {"keep": 1} and fp[STATE_KEY]["last_turn"] == 2
    async with sessionmaker() as s:
        assert await load_state(s, row.id) == det.to_state()
        restored = await load_detector(s, row.id)
    # the restart keeps counting: the third identical read is the forced diagnosis
    assert restored.observe(read(3)).level is StagnationLevel.diagnose


async def test_stale_writer_cannot_roll_back_state(sessionmaker: Any) -> None:
    row = await _attempt(sessionmaker)
    newer, older = StagnationDetector(), StagnationDetector()
    for t in range(1, 6):
        newer.observe(read(t, f"f{t}.py"))
    for t in range(1, 4):
        older.observe(read(t))
    async with sessionmaker() as s:
        assert await save_state(s, row.id, newer)
        await s.commit()
    async with sessionmaker() as s:
        assert not await save_state(s, row.id, older)
        assert await save_state(s, row.id, newer)  # same turn again is fine (idempotent retry)
        await s.commit()
        assert (await load_state(s, row.id) or {})["last_turn"] == 5
        assert not await save_state(s, uuid.uuid4(), newer)  # unknown attempt


async def test_missing_or_corrupt_state_loads_fresh_and_is_overwritten(sessionmaker: Any) -> None:
    row = await _attempt(sessionmaker, fingerprints={STATE_KEY: {"v": 1, "last_turn": "garbage"}, "keep": [1]})
    async with sessionmaker() as s:
        det = await load_detector(s, row.id)
        assert det.turns_observed == 0
        assert await load_state(s, uuid.uuid4()) is None
        assert (await load_detector(s, uuid.uuid4())).last_turn == -1
        det.observe(read(1))
        assert await save_state(s, row.id, det)  # the corrupt value does not block the fresh state
        await s.commit()
    fp = await _fingerprints(sessionmaker, row.id)
    assert fp[STATE_KEY]["last_turn"] == 1 and fp["keep"] == [1]


async def test_prior_escalations_of_the_step(sessionmaker: Any) -> None:
    first = await _attempt(sessionmaker, fingerprints={STATE_KEY: {"escalations": ["heavy_review"]}})
    step = await _step(sessionmaker, first.step_id)
    await _attempt(sessionmaker, step=step, attempt_no=2, fingerprints={STATE_KEY: {"escalations": ["replan", "bogus", 7]}})
    await _attempt(sessionmaker, step=step, attempt_no=3, fingerprints={"unrelated": True})
    current = await _attempt(sessionmaker, step=step, attempt_no=4, fingerprints={STATE_KEY: {"escalations": ["block"]}})
    async with sessionmaker() as s:
        assert await prior_escalations(s, step.id, exclude_attempt_id=current.id) == ("heavy_review", "replan")
        assert await prior_escalations(s, step.id) == ("heavy_review", "replan", "block")
        assert await prior_escalations(s, uuid.uuid4()) == ()


# ------------------------------------------------------------------------------------------------------- events
async def test_events_per_level(sessionmaker: Any) -> None:
    row = await _attempt(sessionmaker)
    det = StagnationDetector()
    secret = "ghp_" + "B" * 36
    async with sessionmaker() as s:
        for t in range(1, 5):
            v = det.observe(
                Observation(
                    t,
                    "run_command",
                    {"command": f"deploy --token {secret}"},
                    ok=False,
                    error_code="SANDBOX_ERROR",
                    output=f"RuntimeError: sandbox down {secret}",
                )
            )
            d = decide(v, LadderContext())
            prev = apply_decision(det, d)
            await record_events(s, job_id=row.job_id, step_id=row.step_id, attempt_id=row.id, verdict=v, decision=d, previous_strategy=prev)
        await s.commit()
    evs = await _events(sessionmaker, row.id)
    assert [(e.event_type, e.severity) for e in evs] == [
        (EventType.STAGNATION_DETECTED, "info"),
        (EventType.STAGNATION_DETECTED, "warning"),
        (EventType.STRATEGY_CHANGED, "info"),
        (EventType.STAGNATION_DETECTED, "error"),
        (EventType.STRATEGY_CHANGED, "warning"),
    ]
    warn, diag, switch, stop, escalate = evs
    assert warn.payload["level"] == "warning" and warn.payload["directive"] == "notice" and warn.payload["turn"] == 2
    assert diag.payload["cause"] == "external" and switch.payload == {
        "turn": 3,
        "level": "diagnose",
        "cause": "external",
        "from": "default",
        "to": "block_external_failure",
        "recommendation": None,
        "reason": diag.payload["reasons"][0],
    }
    assert (
        stop.payload["recommendation"] == "block"
        and escalate.payload["from"] == "block_external_failure"
        and escalate.payload["to"] == "block_step"
    )
    assert {s["kind"] for s in stop.payload["signals"]} >= {"error", "action"}
    assert all(e.step_id == row.step_id and e.job_id == row.job_id for e in evs)
    blob = str([e.payload for e in evs])
    assert secret not in blob and REDACTED in blob
    assert det.escalations == ["block"] and det.strategies == ["block_external_failure", "block_step"]


async def test_no_events_without_stagnation(sessionmaker: Any) -> None:
    row = await _attempt(sessionmaker)
    det = StagnationDetector()
    v = det.observe(read(1))
    async with sessionmaker() as s:
        assert await record_events(s, job_id=row.job_id, step_id=row.step_id, attempt_id=row.id, verdict=v, decision=decide(v)) == []


# ------------------------------------------------------------------------------------------------------ monitor
def _obs(
    turn: int, tool: str, ok: bool = True, output: str = "", code: str | None = None, mutated: list[str] | None = None, **args: Any
) -> TurnObservation:
    return TurnObservation(
        turn,
        CoderAction(tool=ToolName(tool), args=args, decision=tool),
        ToolResult(tool=ToolName(tool), ok=ok, output=output, error_code=code, mutated_paths=mutated or []),
    )


async def test_monitor_persists_and_survives_restart(sessionmaker: Any) -> None:
    row = await _attempt(sessionmaker)
    mon = await create_monitor(sessionmaker, job_id=row.job_id, step_id=row.step_id, attempt_id=row.id)
    d1 = await mon.observe(_obs(1, "read_file", path="app.py"))
    d2 = await mon.observe(_obs(2, "read_file", path="app.py"))
    assert d1.level == "none" and d2.level == "warning" and "read_file app.py" in d2.message
    # "restart": a new monitor for the same attempt continues from the stored counters
    mon2 = await create_monitor(sessionmaker, job_id=row.job_id, step_id=row.step_id, attempt_id=row.id)
    assert mon2.detector.last_turn == 2
    replay = await mon2.observe(_obs(2, "read_file", path="app.py"))
    assert replay.level == "none"
    d3 = await mon2.observe(_obs(3, "read_file", path="app.py"))
    d4 = await mon2.observe(_obs(4, "read_file", path="app.py"))
    assert d3.level == "diagnose" and d3.message.startswith("Forced diagnosis")
    assert d4.level == "stop" and d4.recommendation == "heavy_review" and d4.reasons
    state = (await _fingerprints(sessionmaker, row.id))[STATE_KEY]
    assert state["last_turn"] == 4 and state["escalations"] == ["heavy_review"] and state["stop"]["level"] == "stop"
    # a monitor restored after the stop repeats it without recording a second escalation or new events
    n_events = len(await _events(sessionmaker, row.id))
    mon3 = await create_monitor(sessionmaker, job_id=row.job_id, step_id=row.step_id, attempt_id=row.id)
    again = await mon3.observe(_obs(5, "list_files", path="."))
    assert again.level == "stop" and again.recommendation == "heavy_review"
    assert len(await _events(sessionmaker, row.id)) == n_events
    assert (await _fingerprints(sessionmaker, row.id))[STATE_KEY]["escalations"] == ["heavy_review"]


async def test_monitor_uses_prior_escalations_for_the_next_rung(sessionmaker: Any) -> None:
    first = await _attempt(sessionmaker, fingerprints={STATE_KEY: {"escalations": ["heavy_review"]}})
    step = await _step(sessionmaker, first.step_id)
    second = await _attempt(sessionmaker, step=step, attempt_no=2)
    mon = await create_monitor(sessionmaker, job_id=step.job_id, step_id=step.id, attempt_id=second.id)
    assert mon.ladder.used == ("heavy_review",)
    directive = None
    for t in range(1, 5):
        directive = await mon.observe(_obs(t, "read_file", path="app.py"))
    assert directive is not None and directive.level == "stop" and directive.recommendation == "replan"


async def test_monitor_never_breaks_the_loop_on_persistence_or_diff_failure(sessionmaker: Any) -> None:
    def broken_sm() -> Any:
        raise RuntimeError("database down")

    async def broken_diff() -> str:
        raise OSError("git gone")

    det = StagnationDetector()
    mon = StagnationMonitor(
        det,
        job_id=uuid.uuid4(),
        step_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        sessionmaker=cast(Any, broken_sm),
        diff_provider=broken_diff,
    )
    d1 = await mon.observe(_obs(1, "write_file", path="a.py", content="x", mutated=["a.py"]))
    d2 = await mon.observe(_obs(2, "write_file", path="a.py", content="x"))
    assert d1.level == "none" and d2.level == "warning" and mon.persist_failures == 2
    # monitor without a sessionmaker works purely in memory
    mem = StagnationMonitor(StagnationDetector(), job_id=uuid.uuid4(), step_id=uuid.uuid4(), attempt_id=uuid.uuid4())
    assert (await mem.observe(_obs(1, "git_status"))).level == "none"


async def test_monitor_serialises_concurrent_observations(sessionmaker: Any) -> None:
    row = await _attempt(sessionmaker)
    mon = await create_monitor(sessionmaker, job_id=row.job_id, step_id=row.step_id, attempt_id=row.id)
    directives = await asyncio.gather(*(mon.observe(_obs(t, "read_file", path="app.py")) for t in range(1, 5)))
    assert [d.level for d in directives] == ["none", "warning", "diagnose", "stop"]
    assert (await _fingerprints(sessionmaker, row.id))[STATE_KEY]["last_turn"] == 4


async def test_factory_inherits_counters_for_resume_attempts(sessionmaker: Any) -> None:
    first = await _attempt(sessionmaker)
    step = await _step(sessionmaker, first.step_id)
    mon1 = await create_monitor(sessionmaker, job_id=step.job_id, step_id=step.id, attempt_id=first.id)
    for t in (1, 2):
        await mon1.observe(_obs(t, "read_file", path="app.py"))
    second = await _attempt(sessionmaker, step=step, attempt_no=2)
    factory = make_stagnation_factory(policy=StagnationPolicy())

    def ctx(attempt: StepAttempt, kind: str) -> StepRunContext:
        return StepRunContext(
            job_id=step.job_id,
            step_id=step.id,
            attempt_id=attempt.id,
            attempt_no=attempt.attempt_no,
            attempt_kind=kind,
            step_key="S001",
            kind="implement",
            capability="coding",
            sessionmaker=sessionmaker,
            config=get_config(),
            token=CancelToken(),
        )

    resumed = await factory(ctx(second, "resume"))
    assert resumed.detector.last_turn == 2 and resumed.detector.escalations == []
    assert (await resumed.observe(_obs(3, "read_file", path="app.py"))).level == "diagnose"
    third = await _attempt(sessionmaker, step=step, attempt_no=3)
    fresh = await make_stagnation_factory()(ctx(third, "correction"))
    assert fresh.detector.turns_observed == 0 and fresh.detector.policy == get_config().policies.stagnation


async def test_directive_mapping() -> None:
    det = StagnationDetector()
    levels = [to_directive(decide(det.observe(read(t)))).level for t in range(1, 5)]
    assert levels == ["none", "warning", "diagnose", "stop"]


# --------------------------------------------------------------------------------- end-to-end with the coder loop
async def test_coder_loop_repeating_reads_is_stopped_by_the_monitor(sessionmaker: Any, tmp_repo: Path) -> None:
    h, loop, chat = await _setup(sessionmaker, tmp_repo, [act("read_file", path="app.py")] * 10)
    loop.stagnation = await create_monitor(sessionmaker, job_id=h.step.job_id, step_id=h.step.id, attempt_id=h.attempt_id)
    res = await _run(h, loop)
    assert res.outcome == "stagnated" and res.turns == 4 and res.recommendation == "heavy_review" and res.error_code == "STAGNATION"
    assert any("Stagnation warning" in m.content for m in chat.prompts[2])
    assert any("Forced diagnosis" in m.content for m in chat.prompts[3])
    assert not any("Stagnation" in m.content or "Forced diagnosis" in m.content for m in chat.prompts[1])
    types = [e.event_type for e in await _events(sessionmaker, h.attempt_id)]
    assert types == [
        EventType.STAGNATION_DETECTED,
        EventType.STAGNATION_DETECTED,
        EventType.STRATEGY_CHANGED,
        EventType.STAGNATION_DETECTED,
        EventType.STRATEGY_CHANGED,
    ]
    fp = await _fingerprints(sessionmaker, h.attempt_id)
    assert fp[STATE_KEY]["last_turn"] == 4 and fp[STATE_KEY]["escalations"] == ["heavy_review"]


async def _git(repo: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err
    return out.decode()


async def test_coder_loop_same_failing_test_after_changes_escalates(sessionmaker: Any, tmp_repo: Path) -> None:
    """Real pytest runs in the git workspace: ineffective edits keep the same test red → heavy review recommended."""
    cmd = f"{PY} -m pytest -q -p no:cacheprovider tests/test_app.py"
    actions = [
        act("write_file", path="tests/test_app.py", content=TEST_FILE),
        act("run_test", command=cmd),
        act("replace_text", path="app.py", old="return a + b", new="return (a + b)"),
        act("run_test", command=cmd),
        act("replace_text", path="app.py", old="return (a + b)", new="return ((a + b))"),
        act("run_test", command=cmd),
        act("replace_text", path="app.py", old="return ((a + b))", new="return (((a + b)))"),
        act("run_test", command=cmd),
        act("read_file", path="app.py"),
    ]
    h, loop, chat = await _setup(sessionmaker, tmp_repo, actions)
    base = (await _git(tmp_repo, "rev-parse", "HEAD")).strip()

    async def workspace_diff() -> str:  # full diff incl. untracked files (as the runtime's GitReader provides)
        await _git(tmp_repo, "add", "-A", "-N")
        return await _git(tmp_repo, "diff", base)

    loop.stagnation = await create_monitor(
        sessionmaker, job_id=h.step.job_id, step_id=h.step.id, attempt_id=h.attempt_id, diff_provider=workspace_diff
    )
    res = await _run(h, loop)
    assert res.outcome == "stagnated" and res.turns == 8 and res.recommendation == "heavy_review", res
    assert any("did not resolve" in m.content for m in chat.prompts[4])
    assert any("Forced diagnosis" in m.content and "tests/test_app.py::test_add3" in m.content for m in chat.prompts[6])
    mon = loop.stagnation
    assert isinstance(mon, StagnationMonitor) and mon.last_verdict is not None
    dom = mon.last_verdict.dominant
    assert (
        dom is not None
        and dom.kind is SignalKind.failing_tests
        and dom.after_code_change
        and dom.tests == ("tests/test_app.py::test_add3",)
    )
    assert mon.last_decision is not None and mon.last_decision.cause is not None and mon.last_decision.cause.value == "test_after_change"
    assert mon.detector.epoch == 4  # the test file and three real (but useless) edits were progress on the workspace
    stop = [e for e in await _events(sessionmaker, h.attempt_id) if e.event_type == EventType.STRATEGY_CHANGED][-1]
    assert stop.payload["to"] == "request_heavy_review" and stop.payload["recommendation"] == "heavy_review"


async def test_non_object_fingerprints_column_is_replaced_by_an_object(sessionmaker: Any) -> None:
    row = await _attempt(sessionmaker)
    async with sessionmaker() as s:
        await s.execute(update(StepAttempt).where(StepAttempt.id == row.id).values(fingerprints=["legacy"]))
        await s.commit()
    det = StagnationDetector()
    det.observe(read(1))
    async with sessionmaker() as s:
        assert await save_state(s, row.id, det)
        await s.commit()
    fp = await _fingerprints(sessionmaker, row.id)
    assert list(fp) == [STATE_KEY] and fp[STATE_KEY]["last_turn"] == 1
