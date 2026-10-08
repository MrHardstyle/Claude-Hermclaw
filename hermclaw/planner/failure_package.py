"""Failure package for the replanner (P24 24.1, Bauplan §16).

Collects, from PostgreSQL only, what Gemma needs to replan: the current plan, completed steps with their
summaries, the failed step with its deterministic evidence (verifier failures, test output excerpts, failing
commands, the scope decision, attempt outcomes, review findings), other open steps, and research evidence of the
job. Everything is redacted and size-bounded (tail-truncation for outputs, never mid-line). Model reasoning is not
stored anywhere and therefore can never appear here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import StepStatus
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.persistence.models import (
    CommandRun,
    Job,
    Plan,
    PlanVersion,
    ResearchRun,
    ReviewFindingRow,
    ReviewRun,
    ScopeContractRow,
    Step,
    StepAttempt,
    TestRun,
    VerificationCheckRow,
    VerificationRun,
)
from hermclaw.planner.prompt import shrink_json, truncate_lines, truncate_lines_tail
from hermclaw.planner.replan_contract import ReplanTrigger

MAX_CHECKS = 20
MAX_TEST_RUNS = 3
MAX_COMMANDS = 5
MAX_ATTEMPTS = 3
MAX_FINDINGS = 10
MAX_RESEARCH_RUNS = 5
TEST_OUTPUT_CHARS = 2500
STDERR_CHARS = 800
STDOUT_CHARS = 400
MESSAGE_CHARS = 600
EVIDENCE_CHARS = 800


@dataclass
class ReplanState:
    """The persisted state a replan starts from (read inside one transaction)."""

    job: Job
    plan: Plan
    version: PlanVersion
    steps: list[Step]
    dependencies: dict[uuid.UUID, list[str]]
    failed: Step | None = None
    completed: dict[str, Step] = field(default_factory=dict)

    @property
    def completed_keys(self) -> frozenset[str]:
        return frozenset(self.completed)

    @property
    def open_steps(self) -> list[Step]:
        return [s for s in self.steps if s.status != StepStatus.completed.value]


def _text(value: str | None, limit: int) -> str:
    return truncate_lines(DEFAULT_REDACTOR.text(value or ""), limit)[0]


def _tail(value: str | None, limit: int) -> str:
    return truncate_lines_tail(DEFAULT_REDACTOR.text(value or ""), limit)[0]


def _obj(value: Any, limit: int) -> Any:
    return shrink_json(DEFAULT_REDACTOR.obj(value), limit)[0]


async def _latest_attempts(session: AsyncSession, step_ids: list[uuid.UUID], per_step: int) -> dict[uuid.UUID, list[StepAttempt]]:
    if not step_ids:
        return {}
    rows = (
        await session.execute(
            select(StepAttempt).where(StepAttempt.step_id.in_(step_ids)).order_by(StepAttempt.step_id, StepAttempt.attempt_no.desc())
        )
    ).scalars()
    out: dict[uuid.UUID, list[StepAttempt]] = {}
    for row in rows:
        bucket = out.setdefault(row.step_id, [])
        if len(bucket) < per_step:
            bucket.append(row)
    return out


def _step_summary(step: Step, attempts: list[StepAttempt]) -> str:
    result = step.result or {}
    for key in ("summary", "result_summary", "message"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return _text(value, MESSAGE_CHARS)
    for attempt in attempts:
        if attempt.summary:
            return _text(attempt.summary, MESSAGE_CHARS)
    return ""


async def verifier_evidence(session: AsyncSession, step_id: uuid.UUID) -> dict[str, Any] | None:
    run = (
        await session.execute(
            select(VerificationRun)
            .where(VerificationRun.step_id == step_id)
            .order_by(VerificationRun.created_at.desc(), VerificationRun.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    checks = (
        await session.execute(
            select(VerificationCheckRow)
            .where(VerificationCheckRow.verification_run_id == run.id, VerificationCheckRow.status.in_(["fail", "error"]))
            .order_by(VerificationCheckRow.blocking.desc(), VerificationCheckRow.check_type, VerificationCheckRow.name)
            .limit(MAX_CHECKS)
        )
    ).scalars()
    return {
        "status": run.status,
        "passed": run.passed,
        "summary": _text(run.summary, MESSAGE_CHARS),
        "changed_files": list(run.changed_files or [])[:50],
        "failed_checks": [
            {
                "check_type": c.check_type,
                "name": c.name,
                "status": c.status,
                "blocking": c.blocking,
                "message": _text(c.message, MESSAGE_CHARS),
                "evidence": _obj(c.evidence or {}, EVIDENCE_CHARS),
            }
            for c in checks
        ],
    }


async def test_evidence(session: AsyncSession, step_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(TestRun).where(TestRun.step_id == step_id).order_by(TestRun.created_at.desc(), TestRun.id).limit(MAX_TEST_RUNS)
        )
    ).scalars()
    return [
        {
            "command": _text(r.command, 300),
            "framework": r.framework,
            "status": r.status,
            "passed": r.passed,
            "failed": r.failed,
            "errors": r.errors,
            "skipped": r.skipped,
            "output_tail": _tail(r.output_excerpt, TEST_OUTPUT_CHARS),
        }
        for r in rows
    ]


async def command_evidence(session: AsyncSession, step_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(CommandRun)
            .where(CommandRun.step_id == step_id, or_(CommandRun.exit_code != 0, CommandRun.timed_out.is_(True)))
            .order_by(CommandRun.created_at.desc(), CommandRun.id)
            .limit(MAX_COMMANDS)
        )
    ).scalars()
    return [
        {
            "command": _text(r.command, 300),
            "target": r.target,
            "exit_code": r.exit_code,
            "timed_out": r.timed_out,
            "stderr_tail": _tail(r.stderr_excerpt, STDERR_CHARS),
            "stdout_tail": _tail(r.stdout_excerpt, STDOUT_CHARS),
        }
        for r in rows
    ]


async def scope_evidence(session: AsyncSession, step_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(ScopeContractRow).where(ScopeContractRow.step_id == step_id).order_by(ScopeContractRow.version.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    contract = row.contract or {}
    return {
        "version": row.version,
        "status": row.status,
        "reason": _text(row.reason, MESSAGE_CHARS),
        "target_paths": list(contract.get("target_paths", []))[:40],
        "allowed_new_paths": list(contract.get("allowed_new_paths", []))[:40],
        "forbidden_paths": list(contract.get("forbidden_paths", []))[:40],
        "evidence": _obj(row.evidence or {}, EVIDENCE_CHARS),
    }


async def review_evidence(session: AsyncSession, step_id: uuid.UUID) -> dict[str, Any] | None:
    run = (
        await session.execute(
            select(ReviewRun).where(ReviewRun.step_id == step_id).order_by(ReviewRun.created_at.desc(), ReviewRun.id).limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    findings = (
        await session.execute(
            select(ReviewFindingRow)
            .where(ReviewFindingRow.review_run_id == run.id)
            .order_by(ReviewFindingRow.severity, ReviewFindingRow.path)
            .limit(MAX_FINDINGS)
        )
    ).scalars()
    return {
        "verdict": run.verdict,
        "summary": _text(run.summary, MESSAGE_CHARS),
        "findings": [{"severity": f.severity, "path": f.path, "summary": _text(f.summary, 300)} for f in findings],
    }


async def research_evidence(session: AsyncSession, job_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(ResearchRun)
            .where(ResearchRun.job_id == job_id, ResearchRun.synthesis.is_not(None))
            .order_by(ResearchRun.created_at.desc(), ResearchRun.id)
            .limit(MAX_RESEARCH_RUNS)
        )
    ).scalars()
    return [
        {
            "question": _text(r.question, 400),
            "status": r.status,
            "synthesis": _text(r.synthesis, 1500),
            "contradictions": _obj(list(r.contradictions or []), 600),
        }
        for r in rows
    ]


def _step_brief(step: Step, deps: list[str]) -> dict[str, Any]:
    return {
        "id": step.step_key,
        "title": step.title,
        "kind": step.kind,
        "capability": step.capability,
        "status": step.status,
        "depends_on": deps,
    }


async def build_failure_package(session: AsyncSession, state: ReplanState, trigger: ReplanTrigger) -> dict[str, Any]:
    """The replan-specific sections of the replanner input (budgeting happens in the prompt builder)."""
    completed_ids = [s.id for s in state.completed.values()]
    attempts = await _latest_attempts(session, completed_ids + ([state.failed.id] if state.failed else []), MAX_ATTEMPTS)
    completed = [
        {
            **_step_brief(s, state.dependencies.get(s.id, [])),
            "goal": _text(s.goal, 400),
            "summary": _step_summary(s, attempts.get(s.id, [])),
        }
        for s in sorted(state.completed.values(), key=lambda x: x.step_key)
    ]
    failed: dict[str, Any] | None = None
    if state.failed is not None:
        f = state.failed
        failed = {
            **_step_brief(f, state.dependencies.get(f.id, [])),
            "goal": _text(f.goal, 1500),
            "repo_hints": list(f.repo_hints or []),
            "allowed_new_paths": list(f.allowed_new_paths or []),
            "acceptance": _obj(list(f.acceptance or []), 2000),
            "attempt_count": f.attempt_count,
            "correction_count": f.correction_count,
            "error_code": f.error_code,
            "error_message": _text(f.error_message, MESSAGE_CHARS),
            "attempts": [
                {
                    "attempt_no": a.attempt_no,
                    "kind": a.kind,
                    "status": a.status,
                    "outcome": a.outcome,
                    "error_code": a.error_code,
                    "turns_used": a.turns_used,
                    "summary": _text(a.summary, MESSAGE_CHARS),
                }
                for a in attempts.get(f.id, [])
            ],
        }
    evidence: dict[str, Any] = {"trigger": _obj(trigger.evidence, 3000)}
    if state.failed is not None:
        evidence["verifier"] = await verifier_evidence(session, state.failed.id)
        evidence["tests"] = await test_evidence(session, state.failed.id)
        evidence["failing_commands"] = await command_evidence(session, state.failed.id)
        evidence["scope_decision"] = await scope_evidence(session, state.failed.id)
        evidence["review"] = await review_evidence(session, state.failed.id)
    open_steps = [
        _step_brief(s, state.dependencies.get(s.id, [])) for s in state.open_steps if state.failed is None or s.id != state.failed.id
    ]
    return {
        "trigger": {
            "reason_code": trigger.reason_code,
            "detail": _text(trigger.detail, 1500),
            "failed_step": state.failed.step_key if state.failed is not None else None,
        },
        "current_plan": {"version": state.version.version, "plan": DEFAULT_REDACTOR.obj(state.version.plan_json)},
        "completed_steps": completed,
        "failed_step": failed,
        "deterministic_evidence": {k: v for k, v in evidence.items() if v not in (None, [], {})},
        "open_steps": open_steps,
        "research_evidence": await research_evidence(session, state.job.id),
    }
