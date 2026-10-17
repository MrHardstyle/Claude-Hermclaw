"""Live job acceptance metrics and battery runner against real PostgreSQL (analysis 2026-10-16, §5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.acceptance.job_metrics import Expectation, collect, evaluate
from hermclaw.acceptance.runner import load_spec, report, run_battery
from hermclaw.persistence.models import Event, GitOperation, Job, Plan, PlanVersion, ResearchRun, Step, ToolCallRow, VerificationRun

pytestmark = pytest.mark.integration
SM = async_sessionmaker[AsyncSession]


def _call(job: Job, step: Step, n: int, tool: str, args: dict[str, Any], status: str = "succeeded") -> ToolCallRow:
    return ToolCallRow(
        job_id=job.id,
        step_id=step.id,
        turn=n,
        tool=tool,
        arguments=args,
        status=status,
        started_at=datetime(2026, 10, 16, 12, 0, tzinfo=UTC) + timedelta(seconds=n),
    )


async def _live_like_job(sm: SM) -> uuid.UUID:
    """The shape of live job e3cab55e: research about the repository, a big first plan, a server started again and
    again, a dependency manifest, a verifier failure, two replans."""
    async with sm() as s:
        start = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        job = Job(
            title="health endpoint", prompt="p", status="failed", error_code="REPLAN_LIMIT", finished_at=start + timedelta(minutes=90)
        )
        s.add(job)
        await s.flush()
        job.created_at = start
        plan = Plan(job_id=job.id)
        s.add(plan)
        await s.flush()
        first = {
            "steps": [{"id": f"S00{i}", "kind": k} for i, k in enumerate(["research", "research", "implement", "implement", "verify"], 1)]
        }
        for v in (1, 2, 3):
            s.add(PlanVersion(plan_id=plan.id, job_id=job.id, version=v, source="planner" if v == 1 else "replanner", plan_json=first))
        impl = Step(job_id=job.id, step_key="S003", title="impl", kind="implement", capability="coding", goal="g", status="blocked")
        s.add(impl)
        await s.flush()
        s.add_all(
            [
                _call(job, impl, 1, "run_test", {"command": "node src/app.js"}, "failed"),
                _call(job, impl, 2, "run_test", {"command": "node src/app.js"}, "failed"),
                _call(job, impl, 3, "run_test", {"command": "node src/app.js"}, "failed"),
                _call(job, impl, 4, "write_file", {"path": "src/app.js"}),
                _call(job, impl, 5, "run_test", {"command": "node src/app.js"}, "failed"),
            ]
        )
        s.add(ResearchRun(job_id=job.id, question="What is the programming language used in this repository?", status="completed"))
        s.add(ResearchRun(job_id=job.id, question="How do I configure CORS in Flask 3.1?", status="completed"))
        s.add(
            VerificationRun(
                job_id=job.id,
                step_id=impl.id,
                status="failed",
                summary="expected changes matching ['*'] but none were found",
                changed_files=["src/app.js", "requirements.txt"],
            )
        )
        await s.commit()
        return job.id


async def test_metrics_of_a_live_like_failed_job_fail_the_criteria(sessionmaker: SM) -> None:
    job_id = await _live_like_job(sessionmaker)
    m = await collect(sessionmaker, job_id)
    assert (m.status, m.replans, m.first_plan_steps, m.first_plan_research_steps) == ("failed", 2, 5, 2)
    assert m.duration_minutes == 90.0
    assert m.unanswerable_research == ["What is the programming language used in this repository?"]
    assert m.max_identical_repeats == 3 and "node src/app.js" in (m.worst_repeat or "")  # the write resets the count
    assert m.dependency_manifests_changed == ["requirements.txt"] and len(m.verifier_failures) == 1
    failed = {c.name for c in evaluate(m, Expectation()) if not c.passed}
    assert failed == {"status", "duration", "replans", "first_plan_steps", "research_answerable", "no_new_dependencies", "repeats"}
    assert "no_new_dependencies" not in {c.name for c in evaluate(m, Expectation(allow_dependencies=True)) if not c.passed}
    # rc11 battery: J4 expects a failure and was counted as passed although the model host was down (BUG-176)
    m.status, m.error_code = "failed", "MODEL_UNAVAILABLE"
    outage = {c.name: c for c in evaluate(m, Expectation(status=("failed",), allow_dependencies=True))}
    assert outage["status"].passed and not outage["infrastructure"].passed


async def test_metrics_of_a_good_job_pass(sessionmaker: SM) -> None:
    async with sessionmaker() as s:
        start = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        job = Job(title="ok", prompt="p", status="succeeded", finished_at=start + timedelta(minutes=20))
        s.add(job)
        await s.flush()
        job.created_at = start
        plan = Plan(job_id=job.id)
        s.add(plan)
        await s.flush()
        s.add(
            PlanVersion(
                plan_id=plan.id, job_id=job.id, version=1, source="planner", plan_json={"steps": [{"id": "S001", "kind": "implement"}]}
            )
        )
        impl = Step(job_id=job.id, step_key="S001", title="impl", kind="implement", capability="coding", goal="g", status="completed")
        # rc16 battery J3: a test step is verifier-only and completes without the coder, hence without complete_step (BUG-185)
        check = Step(job_id=job.id, step_key="S002", title="run tests", kind="test", capability="testing", goal="g", status="completed")
        s.add_all([impl, check])
        await s.flush()
        s.add_all([_call(job, impl, 1, "run_test", {"command": "python -m pytest -q"}), _call(job, impl, 2, "complete_step", {})])
        s.add(GitOperation(job_id=job.id, operation="merge_request.create", status="ok"))
        await s.commit()
        job_id = job.id
    m = await collect(sessionmaker, job_id)
    criteria = evaluate(m, Expectation())
    assert all(c.passed for c in criteria), [c for c in criteria if not c.passed]
    assert m.merge_request and m.implementing_steps_completed == 1 and m.implementing_steps_completed_without_complete_step == 0


async def test_battery_submits_waits_and_reports(sessionmaker: SM, tmp_path: Path) -> None:
    spec = tmp_path / "jobs.yaml"
    spec.write_text(
        "repository: demo\n"
        "defaults: {max_minutes: 45, timeout_minutes: 1}\n"
        "jobs:\n"
        "  - {id: J1, title: endpoint, prompt: 'Add an endpoint with a test.'}\n"
        "  - {id: J4, title: impossible, prompt: 'Use a package that does not exist.', expect: {status: [blocked, waiting_for_user, failed]}}\n",
        encoding="utf-8",
    )
    specs = load_spec(spec)
    assert specs[1].expect.status == ("blocked", "waiting_for_user", "failed") and specs[0].repository == "demo"
    created: list[dict[str, Any]] = []

    async def create(job: dict[str, Any]) -> uuid.UUID:
        created.append(job)
        async with sessionmaker() as s:
            row = Job(title=job["title"], prompt=job["prompt"], status="blocked", finished_at=datetime.now(UTC))
            s.add(row)
            await s.commit()
            return row.id

    outcomes = await run_battery(sessionmaker, specs, create, poll_s=0.01, log=lambda _m: None)
    assert [c["title"] for c in created] == ["[acceptance J1] endpoint", "[acceptance J4] impossible"]
    rep = report(outcomes)
    assert [j["passed"] for j in rep["jobs"]] == [False, True] and rep["passed"] is False


def test_spec_rejects_unknown_expectations(tmp_path: Path) -> None:
    spec = tmp_path / "bad.yaml"
    spec.write_text("jobs:\n  - {id: J1, prompt: p, expect: {max_minuts: 3}}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_minuts"):
        load_spec(spec)


@pytest.mark.parametrize("name", ["acceptance-jobs.example.yaml", "acceptance-jobs-extended.example.yaml"])
def test_shipped_batteries_load(name: str) -> None:
    specs = load_spec(Path(__file__).resolve().parents[2] / "config" / name)
    ids = [s.id for s in specs]
    assert ids and len(ids) == len(set(ids))
    assert all(s.base_branch.startswith("acceptance/") for s in specs)


async def test_a_job_waiting_for_the_user_gets_a_duration(sessionmaker: SM) -> None:
    """rc17 battery J4 asked the user after 3.1 min but was counted FAIL with 'None min': waiting_for_user has no
    finished_at; the run ends with its last job transition (BUG-190)."""
    from hermclaw.events.store import append_event

    async with sessionmaker() as s:
        start = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
        job = Job(title="asks", prompt="p", status="waiting_for_user", error_code="USER_INPUT_REQUIRED")
        s.add(job)
        await s.flush()
        job.created_at = start
        await append_event(s, "job.transition", source_type="runtime", job_id=job.id, payload={"to": "waiting_for_user"})
        await s.flush()
        await s.execute(update(Event).where(Event.job_id == job.id).values(ts=start + timedelta(minutes=3)))
        await s.commit()
        job_id = job.id
    m = await collect(sessionmaker, job_id)
    assert m.duration_minutes == 3.0
    exp = Expectation(status=("blocked", "waiting_for_user", "failed"), max_minutes=30, require_merge_request=False)
    assert {c.name: c.passed for c in evaluate(m, exp)}["duration"]
