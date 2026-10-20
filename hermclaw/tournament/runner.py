"""Coder tournament runner: one job per (model, task, repetition), measured and graded with hidden checks.

Every run submits the task's job with the prescribed plan and the candidate's coder alias
(``jobs.metadata[TOURNAMENT]``), waits for it, then grades a copy of the delivered workspace: the task's hidden checks
(never visible to the coder) run in the same sandbox as the job's own commands, next to the fixture's original files
(``_hidden/_base``). Results are appended to ``runs.jsonl`` as they come, so an interrupted tournament resumes where it
stopped; runs that ended on an infrastructure error are repeated (up to ``infra_retries``) and never scored.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fnmatch
import json
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.acceptance.job_metrics import INFRASTRUCTURE_ERRORS, collect, is_terminal
from hermclaw.contracts.common import JobStatus
from hermclaw.core.interfaces import CommandExecutor, ExecutionRequest, WorkspaceHandle
from hermclaw.persistence.models import Event, Job, ModelInvocation
from hermclaw.runtime.transitions import transition_job
from hermclaw.tools.testparse import summarise
from hermclaw.tournament.scoring import RunRecord, render_markdown, score_runs
from hermclaw.tournament.settings import TournamentSettings
from hermclaw.tournament.spec import TaskSpec, TournamentSpec

CreateJob = Callable[[dict[str, Any]], Awaitable[uuid.UUID]]
ExecutorFor = Callable[[WorkspaceHandle], Awaitable[CommandExecutor]]
Workspace = Callable[[uuid.UUID], Awaitable[tuple[Path, str] | None]]  # job -> (work tree, base sha)

# ``FAILED path::name`` (pytest) and ``not ok 3 - name`` (node --test / TAP)
_FAILED = re.compile(r"^(?:FAILED\s+(\S+)|not ok \d+ - (.+?))(?:\s+-\s.*)?$", re.MULTILINE)
_WAITING = (
    JobStatus.queued,
    JobStatus.blocked,
    JobStatus.waiting_for_user,
    JobStatus.waiting_for_resources,
    JobStatus.waiting_for_worker,
)


def _matches(path: str, globs: list[str]) -> bool:
    # fnmatch lets ``*`` cross ``/``: ``data/**`` and ``data/*`` both cover the whole directory
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def changed_files(tree: Path, base_sha: str) -> list[str]:
    """Files the job changed against the fixture: committed, uncommitted and new (``.git`` itself never)."""

    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]

    files = set(git("diff", "--name-only", base_sha)) | set(git("ls-files", "--others", "--exclude-standard"))
    return sorted(files)


def export_base(tree: Path, base_sha: str, dest: Path) -> None:
    """The fixture's original files (``git archive`` of the base commit) for "unchanged" checks."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(tree), "archive", "--format=tar", base_sha], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)


def failed_names(output: str) -> list[str]:
    return [(m.group(1) or m.group(2) or "").strip() for m in _FAILED.finditer(output)][:50]


@dataclasses.dataclass
class Grade:
    passed: int = 0
    failed: int = 0
    failed_names: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None
    output: str = ""


async def grade(  # noqa: PLR0917 - the grading inputs of one run
    spec: TournamentSpec, task: TaskSpec, job_id: uuid.UUID, tree: Path, base_sha: str, work: Path, executor_for: ExecutorFor
) -> Grade:
    """Run the hidden checks of ``task`` on a copy of the delivered work tree."""
    copy = work / f"grade-{job_id}"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(tree, copy, ignore=shutil.ignore_patterns(".git"), symlinks=True)
    hidden = copy / "_hidden"
    shutil.copytree(spec.hidden_dir(task), hidden, dirs_exist_ok=True)
    (hidden / "__init__.py").touch()
    export_base(tree, base_sha, hidden / "_base")
    handle = WorkspaceHandle(uuid.uuid4(), job_id, copy, "grading", "grading", base_sha, f"tournament-{task.id}")
    executor = await executor_for(handle)
    try:
        res = await executor.run(
            handle,
            ExecutionRequest(
                command=task.hidden.command,
                timeout_seconds=task.hidden.timeout_seconds,
                image=task.hidden.image,
                job_id=job_id,
                purpose="test",
            ),
        )
    except Exception as exc:  # the grading sandbox is infrastructure: the run is repeated, never scored
        return Grade(error=f"grading failed: {type(exc).__name__}: {exc}"[:500])
    finally:
        drop = getattr(executor, "drop", None) or getattr(getattr(executor, "inner", None), "drop", None)
        if drop is not None:
            with contextlib.suppress(Exception):
                await drop(handle)
        shutil.rmtree(copy, ignore_errors=True)
    output = f"{res.stdout}\n{res.stderr}"
    summary = summarise(output, res.exit_code, timed_out=res.timed_out)
    names = failed_names(output)
    if summary.passed + summary.failed + summary.errors == 0:
        return Grade(error=f"hidden checks produced no result (exit {res.exit_code})", output=output[-4000:])
    return Grade(summary.passed, summary.failed + summary.errors, names, None, output[-4000:])


async def request_cancel(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID, reason: str) -> None:
    """The time limit of a run is up: the same cancellation the API performs (``POST /api/jobs/{id}/cancel``)."""
    async with sm() as s:
        job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
        status = JobStatus(job.status)
        if is_terminal(status.value):
            return
        job.cancel_requested = True
        if status in _WAITING:
            await transition_job(s, job, JobStatus.cancelled, reason=reason, actor="tournament")
        await s.commit()


async def _model_usage(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> tuple[int, int, int, float]:
    async with sm() as s:
        row = (
            await s.execute(
                select(
                    func.count(ModelInvocation.id),
                    func.coalesce(func.sum(ModelInvocation.prompt_tokens), 0),
                    func.coalesce(func.sum(ModelInvocation.completion_tokens), 0),
                    func.coalesce(func.sum(ModelInvocation.latency_ms), 0),
                ).where(ModelInvocation.job_id == job_id, ModelInvocation.role != "embedding")
            )
        ).one()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), float(row[3] or 0) / 1000.0


async def _corrections(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> int:
    async with sm() as s:
        return int(
            (
                await s.execute(select(func.count(Event.sequence)).where(Event.job_id == job_id, Event.event_type == "correction.started"))
            ).scalar_one()
        )


class TournamentRunner:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        spec: TournamentSpec,
        out_dir: Path,
        *,
        create: CreateJob,
        workspace: Workspace,
        executor_for: ExecutorFor,
        poll_s: float = 20.0,
        infra_retries: int = 2,
        log: Callable[[str], None] = print,
    ) -> None:
        self.sm, self.spec, self.out = sm, spec, out_dir
        self.create, self.workspace, self.executor_for = create, workspace, executor_for
        self.poll_s, self.infra_retries, self.log = poll_s, infra_retries, log
        self.out.mkdir(parents=True, exist_ok=True)
        self.runs_file = self.out / "runs.jsonl"

    def load_runs(self) -> list[RunRecord]:
        if not self.runs_file.exists():
            return []
        fields = {f.name for f in dataclasses.fields(RunRecord)}
        return [
            RunRecord(**{k: v for k, v in json.loads(line).items() if k in fields})
            for line in self.runs_file.read_text().splitlines()
            if line
        ]

    def _append(self, record: RunRecord) -> None:
        with self.runs_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")

    async def run(self, models: list[str], tasks: list[TaskSpec], reps: int | None = None) -> list[RunRecord]:
        """Models one after another (one model switch each), every task and repetition per model."""
        done = self.load_runs()
        for model in models:
            for task in tasks:
                for rep in range(1, (reps or self.spec.reps(task)) + 1):
                    tries = [r for r in done if (r.model, r.task, r.rep) == (model, task.id, rep)]
                    while not any(r.scored for r in tries) and len(tries) <= self.infra_retries:
                        if tries:
                            self.log(f"{model} {task.id}#{rep}: infrastructure ({tries[-1].infrastructure}) – repeated")
                        record = await self.run_one(model, task, rep)
                        self._append(record)
                        done.append(record)
                        tries.append(record)
                    self.write_report(done, models)
        return done

    async def run_one(self, model: str, task: TaskSpec, rep: int) -> RunRecord:
        settings = TournamentSettings(plan=task.plan, coder_alias=model, task=task.id, run=f"{model}/{task.id}/{rep}")
        job_id = await self.create(
            {
                "prompt": task.prompt,
                "title": f"[tournament {task.id} {model} #{rep}] {task.title}",
                "repo": task.repository or self.spec.repository,
                "base_branch": task.base_branch,
                "tournament": settings.model_dump(),
            }
        )
        self.log(f"{model} {task.id}#{rep}: job {job_id}")
        limit_s = self.spec.limit_minutes(task) * 60
        started = time.monotonic()
        timed_out = False
        status = "queued"
        while True:
            async with self.sm() as s:
                job = await s.get(Job, job_id)
                status = job.status if job else "missing"
            if is_terminal(status) or status == "missing":
                break
            if not timed_out and time.monotonic() - started >= limit_s:
                timed_out = True
                await request_cancel(self.sm, job_id, f"tournament time limit {self.spec.limit_minutes(task):g} min")
            if time.monotonic() - started >= limit_s + 900:  # cancellation must not hang the tournament
                break
            await asyncio.sleep(self.poll_s)
        return await self.measure(model, task, rep, job_id, timed_out)

    async def measure(self, model: str, task: TaskSpec, rep: int, job_id: uuid.UUID, timed_out: bool) -> RunRecord:
        metrics = await collect(self.sm, job_id)
        calls, prompt_tokens, completion_tokens, model_seconds = await _model_usage(self.sm, job_id)
        infra = f"job ended on {metrics.error_code}" if metrics.error_code in INFRASTRUCTURE_ERRORS else None
        found = await self.workspace(job_id)
        g, files = Grade(error="no workspace"), []
        if found is not None:
            tree, base_sha = found
            files = changed_files(tree, base_sha)
            g = await grade(self.spec, task, job_id, tree, base_sha, self.out / "work", self.executor_for)
        if g.error and infra is None:
            infra = g.error
        record = RunRecord(
            model=model,
            task=task.id,
            rep=rep,
            size=task.size,
            job_id=str(job_id),
            status=metrics.status,
            error_code=metrics.error_code,
            infrastructure=infra,
            duration_minutes=round(metrics.duration_minutes or 0.0, 1),
            timed_out=timed_out,
            hidden_passed=g.passed,
            hidden_failed=g.failed,
            hidden_failed_names=g.failed_names,
            hidden_error=g.error,
            changed_files=files,
            unexpected_changes=[f for f in files if task.expected_changes and not _matches(f, task.expected_changes)],
            protected_changes=[f for f in files if _matches(f, task.protected)],
            corrections=await _corrections(self.sm, job_id),
            replans=metrics.replans,
            model_calls=calls,
            tool_calls=metrics.tool_calls,
            failed_tool_calls=metrics.failed_tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_seconds=round(model_seconds, 1),
        )
        (self.out / "grading").mkdir(exist_ok=True)
        (self.out / "grading" / f"{model}-{task.id}-{rep}-{job_id}.txt").write_text(g.output or g.error or "", encoding="utf-8")
        self.log(
            f"{model} {task.id}#{rep}: {record.status}, hidden {g.passed}/{g.passed + g.failed}, {record.duration_minutes} min"
            + (f", infrastructure: {infra}" if infra else "")
        )
        return record

    def write_report(self, runs: list[RunRecord], models: list[str]) -> Path:
        score_runs(runs, self.spec.weights)
        scored = {(r.model, r.task, r.rep): r for r in runs if r.scored}  # repeats after infrastructure errors: keep one
        unscored = [r for r in runs if not r.scored and (r.model, r.task, r.rep) not in scored]
        final = [*scored.values(), *unscored]
        path = self.out / "report.md"
        path.write_text(render_markdown(final, models, "Coder-Turnier"), encoding="utf-8")
        (self.out / "report.json").write_text(json.dumps([r.as_dict() for r in final], ensure_ascii=False, indent=1), encoding="utf-8")
        return path
