"""Measured criteria of one finished job (root-cause analysis 2026-10-16, §5).

The end-to-end tests replay ideal, scripted model answers; they could not see what the real models do. These
metrics are computed from the job's persisted record (plans, steps, tool calls, research, verification, git) after a
real job ran and turn the success criteria of the analysis into numbers that a release must meet:

- the job reached its expected terminal status, with a merge request when it succeeded;
- few replans and a small first plan for a small job;
- no web research the web cannot answer (questions about the repository itself or about a technology choice);
- no external dependency manifest when the job did not ask for one;
- no identical, ineffective tool call repeated more than ``max_repeats`` times on an unchanged workspace;
- every implementing step that the runtime completed was finished with ``complete_step``.

Nothing here knows a particular job: the expectations come from the job specification.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.tools import MUTATING_TOOLS, ToolName
from hermclaw.persistence.models import GitOperation, Job, PlanVersion, ResearchRun, Step, ToolCallRow, VerificationRun
from hermclaw.research.questions import unanswerable_reason

#: dependency manifests of the ecosystems the sandbox installs from (D-014) and their common siblings
DEPENDENCY_MANIFESTS = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "composer.json",
        "Gemfile",
    }
)
#: tools whose identical repetition on an unchanged workspace cannot produce a new result
REPEAT_TOOLS = frozenset({ToolName.run_test, ToolName.run_command, ToolName.install_dependencies, ToolName.request_research})
WRITE_TOOL_NAMES = frozenset(t.value for t in MUTATING_TOOLS)
IMPLEMENTING_KINDS = frozenset({"implement", "test", "documentation", "docker", "database"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "blocked", "waiting_for_user"})
#: errors of the infrastructure, not of the job: a job ending with one proves nothing, even an expected failure
INFRASTRUCTURE_ERRORS = frozenset({"MODEL_UNAVAILABLE", "MODEL_TIMEOUT", "MODEL_LOAD_FAILED", "MODEL_NOT_FOUND", "WORKER_ERROR"})


@dataclass
class JobMetrics:
    job_id: str
    title: str
    status: str
    error_code: str | None
    duration_minutes: float | None
    plan_versions: int
    replans: int
    first_plan_steps: int
    first_plan_research_steps: int
    research_questions: list[str] = field(default_factory=list)
    unanswerable_research: list[str] = field(default_factory=list)
    tool_calls: int = 0
    failed_tool_calls: int = 0
    max_identical_repeats: int = 0
    worst_repeat: str | None = None
    implementing_steps_completed: int = 0
    implementing_steps_completed_without_complete_step: int = 0
    complete_step_calls: int = 0
    verifier_failures: list[str] = field(default_factory=list)
    dependency_manifests_changed: list[str] = field(default_factory=list)
    merge_request: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Expectation:
    """What a job specification expects; ``None`` disables a criterion."""

    status: Sequence[str] = ("succeeded",)
    max_minutes: float | None = 45.0
    max_replans: int | None = 1
    max_first_plan_steps: int | None = 3
    allow_dependencies: bool = False
    max_repeats: int = 2
    require_merge_request: bool = True


@dataclass(frozen=True)
class Criterion:
    name: str
    passed: bool
    detail: str


def _repeats(calls: Iterable[ToolCallRow]) -> tuple[int, str | None]:
    """Largest number of identical repeat-tool calls in one step without a successful file write in between."""
    worst, label = 0, None
    seen: dict[tuple[str, str, str], int] = {}
    for c in calls:
        step = str(c.step_id)
        if c.tool in WRITE_TOOL_NAMES and c.status == "succeeded":
            seen = {k: v for k, v in seen.items() if k[0] != step}
            continue
        if c.tool not in {t.value for t in REPEAT_TOOLS}:
            continue
        key = (step, c.tool, json.dumps(c.arguments, sort_keys=True, default=str))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > worst:
            worst, label = seen[key], f"{c.tool} {key[2][:160]}"
    return worst, label


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


async def collect(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> JobMetrics:
    async with sm() as s:
        job = await s.get(Job, job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found")
        plans = list((await s.scalars(select(PlanVersion).where(PlanVersion.job_id == job_id).order_by(PlanVersion.version))).all())
        steps = list((await s.scalars(select(Step).where(Step.job_id == job_id))).all())
        calls = list((await s.scalars(select(ToolCallRow).where(ToolCallRow.job_id == job_id).order_by(ToolCallRow.started_at))).all())
        research = list((await s.scalars(select(ResearchRun).where(ResearchRun.job_id == job_id))).all())
        verifications = list((await s.scalars(select(VerificationRun).where(VerificationRun.job_id == job_id))).all())
        git_ops = list((await s.scalars(select(GitOperation).where(GitOperation.job_id == job_id))).all())

    first = plans[0].plan_json.get("steps", []) if plans else []
    questions = [r.question for r in research]
    duration = None
    if job.finished_at and job.created_at:
        duration = round((job.finished_at - job.created_at).total_seconds() / 60.0, 1)
    worst, label = _repeats(calls)
    completed_by_tool = {c.step_id for c in calls if c.tool == ToolName.complete_step.value and c.status == "succeeded"}
    implementing_done = [st for st in steps if st.kind in IMPLEMENTING_KINDS and st.status == "completed" and not st.superseded]
    changed: set[str] = set()
    for v in verifications:
        changed.update(str(p) for p in v.changed_files or [])
    return JobMetrics(
        job_id=str(job.id),
        title=job.title,
        status=job.status,
        error_code=job.error_code,
        duration_minutes=duration,
        plan_versions=len(plans),
        replans=max(0, len(plans) - 1),
        first_plan_steps=len(first),
        first_plan_research_steps=sum(1 for st in first if st.get("kind") == "research"),
        research_questions=questions,
        unanswerable_research=[q for q in questions if unanswerable_reason(q)],
        tool_calls=len(calls),
        failed_tool_calls=sum(1 for c in calls if c.status != "succeeded"),
        max_identical_repeats=worst,
        worst_repeat=label,
        implementing_steps_completed=len(implementing_done),
        implementing_steps_completed_without_complete_step=sum(1 for st in implementing_done if st.id not in completed_by_tool),
        complete_step_calls=sum(1 for c in calls if c.tool == ToolName.complete_step.value),
        verifier_failures=[(v.summary or "")[:500] for v in verifications if v.status in ("failed", "error")],
        dependency_manifests_changed=sorted(p for p in changed if _basename(p) in DEPENDENCY_MANIFESTS),
        merge_request=any(g.operation == "merge_request.create" and g.status == "ok" for g in git_ops),
    )


def evaluate(m: JobMetrics, exp: Expectation) -> list[Criterion]:
    expected = "/".join(exp.status)
    out = [Criterion("status", m.status in exp.status, f"{m.status} (expected {expected}; error {m.error_code or '-'})")]
    infra = m.error_code in INFRASTRUCTURE_ERRORS
    out.append(Criterion("infrastructure", not infra, f"job ended on {m.error_code}" if infra else "no infrastructure error"))
    if exp.require_merge_request and m.status == "succeeded":
        out.append(Criterion("merge_request", m.merge_request, "merge request created" if m.merge_request else "no merge request"))
    if exp.max_minutes is not None:
        ok = m.duration_minutes is not None and m.duration_minutes <= exp.max_minutes
        out.append(Criterion("duration", ok, f"{m.duration_minutes} min (max {exp.max_minutes:g})"))
    if exp.max_replans is not None:
        out.append(Criterion("replans", m.replans <= exp.max_replans, f"{m.replans} (max {exp.max_replans})"))
    if exp.max_first_plan_steps is not None:
        limit = exp.max_first_plan_steps
        out.append(Criterion("first_plan_steps", m.first_plan_steps <= limit, f"{m.first_plan_steps} (max {limit})"))
    unanswerable = "; ".join(m.unanswerable_research)[:300]
    out.append(Criterion("research_answerable", not m.unanswerable_research, unanswerable or "none unanswerable"))
    if not exp.allow_dependencies:
        manifests = ", ".join(m.dependency_manifests_changed)
        out.append(Criterion("no_new_dependencies", not m.dependency_manifests_changed, manifests or "no manifest changed"))
    repeat = f"{m.max_identical_repeats} (max {exp.max_repeats}) {m.worst_repeat or ''}".strip()
    out.append(Criterion("repeats", m.max_identical_repeats <= exp.max_repeats, repeat))
    out.append(
        Criterion(
            "complete_step",
            m.implementing_steps_completed_without_complete_step == 0,
            f"{m.implementing_steps_completed} implementing step(s) completed, "
            f"{m.implementing_steps_completed_without_complete_step} without complete_step",
        )
    )
    return out


def is_terminal(status: str) -> bool:
    return status in TERMINAL
