"""Learning after jobs (D-019 part 3): what a finished job teaches goes into the knowledge.

A pass reads finished jobs that were not read yet (``jobs.metadata.memory_learned_at``) and records:

* **Refusals** – one tool refusing with one error code at least ``min_repeats`` times in a job. The entry says how often,
  with which models, the typical message and what helped (the calls up to the next success of the same tool on the same
  file in that attempt). Signature ``tool:<tool>:<error code>``.
* **Stalls** – every ``stagnation.detected`` with a diagnosis or a stop: cause, reasons, the repeated signal and how the
  step ended. Signature ``stall:<cause>:<tool or signal>:<error code>``.
* **Failed jobs** – signature ``job:<error code>``.
* **Research** – each completed research run becomes an active ``research`` entry (question, synthesis, confident
  claims, sources) that ages out after ``research_days``.
* **Lessons** (D-025, :mod:`hermclaw.memory.distill`) – each fix of a finished or cancelled job (a correction that
  passed, a red run turned green) goes to one model call that writes a general lesson or none. Needs the model lease;
  while the GPU is busy the job waits for a later pass (``jobs.metadata.memory_lessons_at`` marks it done).

An incident starts as a candidate. Seen in ``promote_after`` jobs and with something that helped, it becomes active
and reaches the models. A refusal that models of different kinds could not get past in ``promote_after`` jobs is a
tool-bug suspicion: a ``tool_bug`` candidate and an open bug record for the operator – a tool is never changed by
itself. Entries the operator edited (``source = operator``) or retired keep their text and status.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core import job_metadata
from hermclaw.core.config import HermclawConfig, MemoryPolicy, ModelProfileConfig
from hermclaw.core.errors import ConfigError, HermclawError
from hermclaw.core.logging import get_logger
from hermclaw.memory.distill import FixEpisode, distill, fix_episodes
from hermclaw.memory.store import MemoryStore, entry_text
from hermclaw.models.protocols import ChatModel
from hermclaw.persistence.models import (
    BugRecord,
    Event,
    Job,
    ModelInvocation,
    Repository,
    ResearchClaim,
    ResearchRun,
    ResearchSource,
    Step,
    StepAttempt,
    ToolCallRow,
)
from hermclaw.tools import errors as E

log = get_logger(__name__)

LEARNED_KEY = job_metadata.MEMORY_LEARNED_AT  # reserved: a client cannot keep its job out of learning (BUG-308)
LESSONS_KEY = job_metadata.MEMORY_LESSONS_AT
FINISHED = ("succeeded", "failed")
# a fix that passed the checks teaches the same in a job cancelled later (the tournament's time limit)
LESSON_JOBS = (*FINISHED, "cancelled")
# refusals that say nothing about a tool or the task: the step was over, the budget spent, the call cancelled; and
# runs that executed and failed – a red test run is work in progress, not a refusal (BUG-306)
_NOT_LEARNED = frozenset({"CANCELLED", "STEP_FINISHED", "TURN_BUDGET_EXHAUSTED", E.SANDBOX_ERROR, *E.RUN_FAILED})
# refusals that are a guard doing its job (a repeated command or read, an unchanged edit, a forbidden command, a check
# that still fails, a policy): models stuck on them say something about the model, not the tool. The dry run against
# the production jobs raised suspicions for COMMAND_REPEATED and READ_REPEATED (BUG-307). Scope verdicts on commands
# stay suspicious: the Pester report counted as a violation was a tool bug (BUG-305).
_GUARDS = frozenset(
    {
        E.COMMAND_REPEATED, E.READ_REPEATED, E.NO_CHANGE, E.CHECK_STILL_FAILING, E.REDACTED_PLACEHOLDER, E.SCOPE_VIOLATION,
        E.SCOPE_EXPANSION_DENIED, E.COMMAND_FORBIDDEN, E.COMMAND_DESTRUCTIVE, E.DEPENDENCY_NOT_REQUESTED,
        E.PACKAGE_INSTALL_REFUSED, E.TOOL_NOT_ALLOWED, E.PATH_FORBIDDEN, E.PATH_OUTSIDE_WORKSPACE, E.RESEARCH_REFUSED,
        E.INSTALL_UNCHANGED,
    }
)  # fmt: skip
_STALL_DIRECTIVES = ("diagnose", "stop")
_MAX_EXAMPLES = 5


@dataclass
class LearnReport:
    jobs: int = 0
    incidents: int = 0
    stalls: int = 0
    research: int = 0
    promoted: int = 0
    suspicions: int = 0
    lessons: int = 0  # new lesson candidates
    lessons_seen: int = 0  # fixes that repeated a known lesson
    no_lesson: int = 0  # fixes the model judged specific to their task
    errors: list[str] = field(default_factory=list)


@dataclass
class LessonModel:
    """What writing lessons needs (D-025): the model and its lease (``resources`` is a ResourceManager or None)."""

    chat: ChatModel
    profile: ModelProfileConfig
    resources: Any = None


@dataclass
class _Refusal:
    tool: str
    code: str
    count: int = 0
    models: set[str] = field(default_factory=set)
    steps: set[str] = field(default_factory=set)
    message: str = ""
    helped: str | None = None


def lesson_model(config: HermclawConfig, services: Any) -> LessonModel | None:
    """The coder profile (or ``policies.memory.lesson_alias``) with the runtime's chat and resource manager; None
    when there is no enabled one."""
    alias = config.policies.memory.lesson_alias
    try:
        profile = config.models.by_alias(alias) if alias else config.models.by_role("coder")
    except ConfigError:
        return None
    if not profile.enabled:
        return None
    return LessonModel(services.chat, profile, getattr(services, "resources", None))


def _clip(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _path(args: dict[str, Any]) -> str | None:
    value = args.get("path") or args.get("file")
    return str(value) if value else None


def helped_after(calls: list[ToolCallRow], tool: str, code: str) -> str | None:
    """What got the attempt past the last refusal of ``tool``/``code``: the tools called up to the next success of
    the same tool (on the same file, when it names one), e.g. ``read_file → replace_text``; None if it never got past."""
    last = max((i for i, c in enumerate(calls) if c.tool == tool and c.error_code == code), default=None)
    if last is None:
        return None
    path = _path(calls[last].arguments or {})
    between: list[str] = []
    for c in calls[last + 1 :]:
        if c.tool == tool and c.error_code is None and c.status == "succeeded" and (path is None or _path(c.arguments or {}) == path):
            return " → ".join([*between[-3:], tool])
        if c.error_code is None and (not between or between[-1] != c.tool):
            between.append(c.tool)
    return None


def refusal_content(tool: str, code: str, examples: list[dict[str, Any]], jobs: int) -> str:
    """The text of a refusal incident as a model reads it, from its latest examples."""
    models = sorted({m for e in examples for m in e.get("models", [])})
    helped = Counter(e["helped"] for e in examples if e.get("helped"))
    unresolved = sum(1 for e in examples if not e.get("helped"))
    by = f" (models: {', '.join(models)})" if models else ""
    lines = [f"`{tool}` refused with {code} in {jobs} job(s){by}."]
    message = next((e["message"] for e in reversed(examples) if e.get("message")), "")
    if message:
        lines.append(f"Typical message: {message}")
    if helped:
        best, n = helped.most_common(1)[0]
        lines.append(f"What helped: {best} ({n} of {len(examples)} recent cases).")
    if unresolved:
        lines.append(f"Not got past in {unresolved} of {len(examples)} recent cases.")
    return "\n".join(lines)


def stall_content(examples: list[dict[str, Any]], jobs: int) -> str:
    last = examples[-1]
    lines = [f"Stall ({last.get('cause') or 'unknown cause'}) seen in {jobs} job(s)."]
    for e in examples[-3:]:
        outcome = {"succeeded": "the step succeeded later", "failed": "the step failed"}.get(e.get("outcome") or "", "open")
        where = f'step "{e.get("step", "?")}", model {e.get("model") or "?"}, turn {e.get("turn")}'
        lines.append(f"- {where}: {e.get('reasons', '')} → {outcome}")
    if last.get("signal"):
        lines.append(f"Repeated signal: {last['signal']}")
    return "\n".join(lines)


def _sha(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


class MemoryLearner:
    """Reads finished jobs into the knowledge; ``run_once`` is one pass, ``start`` runs passes in the background."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: MemoryStore,
        policy: MemoryPolicy,
        *,
        lesson_model: LessonModel | None = None,
    ) -> None:
        self.sm, self.store, self.policy = sessionmaker, store, policy
        self.lesson_model = lesson_model
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------------------------------ passes
    async def run_once(self) -> LearnReport:
        report = LearnReport()
        if not self.policy.learn:
            return report
        for job_id in await self._pending():
            try:
                await self.learn_job(job_id, report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad job must not stop learning from the others
                log.warning("learning from a job failed", extra={"job_id": str(job_id), "error_type": type(exc).__name__})
                report.errors.append(f"{job_id}: {type(exc).__name__}")
            await self._mark(job_id)
            report.jobs += 1
        if self.policy.lessons and self.lesson_model is not None and self.policy.lessons_per_job:
            await self._lessons(report)
        return report

    async def _pending(self) -> list[uuid.UUID]:
        stmt = (
            select(Job.id)
            .where(Job.status.in_(FINISHED), ~Job.metadata_.has_key(LEARNED_KEY))
            .order_by(Job.finished_at.asc().nulls_last(), Job.created_at.asc(), Job.id)  # evidence in the order it arrived
            .limit(self.policy.learn_batch)
        )
        if self.policy.learn_since_days:
            since = datetime.now(UTC) - timedelta(days=self.policy.learn_since_days)
            stmt = stmt.where(or_(Job.finished_at.is_(None), Job.finished_at >= since))
        async with self.sm() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def _mark(self, job_id: uuid.UUID, *, lessons: bool = False) -> None:
        when = datetime.now(UTC).isoformat()
        stamp = func.jsonb_build_object(LESSONS_KEY, when) if lessons else func.jsonb_build_object(LEARNED_KEY, when)
        async with self.sm() as s, s.begin():  # a JSON merge: no row version, a control on the job is not disturbed
            await s.execute(update(Job).where(Job.id == job_id).values(metadata_=Job.metadata_.op("||")(stamp)))

    # ------------------------------------------------------------------------------------------ one job
    async def learn_job(self, job_id: uuid.UUID, report: LearnReport | None = None) -> LearnReport:
        report = report or LearnReport()
        async with self.sm() as s:
            job = await s.get(Job, job_id)
            if job is None:
                return report
            attempts = list((await s.execute(select(StepAttempt).where(StepAttempt.job_id == job_id))).scalars())
            models = {a.id: a.model_alias for a in attempts if a.model_alias}
            # attempts before BUG-307 carry no model; their coder turns recorded the alias that answered
            turns = select(ModelInvocation.attempt_id, ModelInvocation.alias).where(
                ModelInvocation.job_id == job_id, ModelInvocation.purpose == "coder_turn", ModelInvocation.attempt_id.is_not(None)
            )
            for attempt_id, alias in (await s.execute(turns.order_by(ModelInvocation.started_at))).all():
                if attempt_id is not None and alias:
                    models.setdefault(attempt_id, alias)
            steps = {st.id: st for st in (await s.execute(select(Step).where(Step.job_id == job_id))).scalars()}
            calls = list(
                (
                    await s.execute(
                        select(ToolCallRow).where(ToolCallRow.job_id == job_id).order_by(ToolCallRow.started_at, ToolCallRow.turn)
                    )
                ).scalars()
            )
            stalls = list(
                (
                    await s.execute(
                        select(Event)
                        .where(Event.job_id == job_id, Event.event_type == EventType.STAGNATION_DETECTED)
                        .order_by(Event.sequence)
                    )
                ).scalars()
            )
            runs = list(
                (await s.execute(select(ResearchRun).where(ResearchRun.job_id == job_id, ResearchRun.status == "completed"))).scalars()
            )
            job_error, job_message, job_status = job.error_code, job.error_message, job.status
            repo = await s.get(Repository, job.repository_id) if job.repository_id else None
            # research is kept for the repository it was asked for: "which framework does this repository use" answered
            # for one job is no knowledge for another (dry run against the production jobs, BUG-307)
            research_scope = f"repo:{repo.name}" if repo is not None else None
        await self._refusals(job_id, calls, models, steps, report)
        await self._stalls(job_id, stalls, models, steps, report)
        if job_status == "failed" and job_error:
            await self.store.record_incident(
                signature=f"job:{job_error}",
                title=f"Jobs failed with {job_error}",
                content=f"A job failed with {job_error}: {_clip(job_message, 400)}",
                job_id=job_id,
                evidence={"job": str(job_id), "message": _clip(job_message, 300)},
                tags=["job-failed"],
            )
        for run in runs:
            await self._research(job_id, run, report, scope=research_scope)
        return report

    async def _refusals(
        self,
        job_id: uuid.UUID,
        calls: list[ToolCallRow],
        models: dict[uuid.UUID, str],
        steps: dict[uuid.UUID, Step],
        report: LearnReport,
    ) -> None:
        found: dict[tuple[str, str], _Refusal] = {}
        by_attempt: dict[uuid.UUID | None, list[ToolCallRow]] = {}
        for c in calls:
            by_attempt.setdefault(c.attempt_id, []).append(c)
        for attempt_id, seq in by_attempt.items():
            model = models.get(attempt_id) if attempt_id else None
            for c in seq:
                if not c.error_code or c.error_code in _NOT_LEARNED:
                    continue
                r = found.setdefault((c.tool, c.error_code), _Refusal(c.tool, c.error_code))
                r.count += 1
                r.message = r.message or _clip(c.result_summary, 300)
                if model:
                    r.models.add(model)
                if c.step_id in steps:
                    r.steps.add(steps[c.step_id].step_key)
            for (tool, code), r in found.items():
                if r.helped is None and any(c.tool == tool and c.error_code == code for c in seq):
                    r.helped = helped_after(seq, tool, code)
        for r in found.values():
            if r.count < self.policy.min_repeats:
                continue
            example = {
                "job": str(job_id),
                "models": sorted(r.models),
                "steps": sorted(r.steps),
                "count": r.count,
                "message": r.message,
                "helped": r.helped,
            }
            signature = f"tool:{r.tool}:{r.code}"
            entry_id, seen = await self.store.record_incident(
                signature=signature,
                title=f"{r.tool} refuses with {r.code}",
                content=refusal_content(r.tool, r.code, [example], 1),
                job_id=job_id,
                evidence=example,
                tags=["refusal", r.tool],
            )
            report.incidents += 1
            examples = await self._settle(entry_id, seen, lambda ex, n, t=r.tool, c=r.code: refusal_content(t, c, ex, n), report)
            await self._suspect(signature, tool=r.tool, code=r.code, examples=examples, seen=seen, job_id=job_id, report=report)

    async def _stalls(
        self,
        job_id: uuid.UUID,
        events: list[Event],
        models: dict[uuid.UUID, str],
        steps: dict[uuid.UUID, Step],
        report: LearnReport,
    ) -> None:
        seen_here: set[str] = set()
        for ev in events:
            p = ev.payload or {}
            if p.get("directive") not in _STALL_DIRECTIVES:
                continue
            cause = str(p.get("cause") or "unknown")
            signal = (p.get("signals") or [{}])[0] or {}
            what = signal.get("tool") or signal.get("kind") or "?"
            code = signal.get("error_code") or ""
            signature = f"stall:{cause}:{what}:{code}".rstrip(":")
            if signature in seen_here:  # one example per stall kind and job: the first diagnosis says it
                continue
            seen_here.add(signature)
            step = steps.get(ev.step_id) if ev.step_id else None
            example = {
                "job": str(job_id),
                "step": step.title if step else None,
                "outcome": step.status if step else None,
                "model": models.get(ev.attempt_id) if ev.attempt_id else None,
                "turn": p.get("turn"),
                "cause": cause,
                "directive": p.get("directive"),
                "reasons": _clip("; ".join(str(x) for x in (p.get("reasons") or [])[:3]), 400),
                "signal": _clip(f"{signal.get('label') or what} ×{signal.get('count', '?')}", 200) if signal else None,
                "helped": "the step succeeded later" if step is not None and step.status == "succeeded" else None,
            }
            entry_id, seen = await self.store.record_incident(
                signature=signature,
                title=f"Stall: {cause} ({what}{f' {code}' if code else ''})",
                content=stall_content([example], 1),
                job_id=job_id,
                evidence=example,
                tags=["stall", cause],
            )
            report.stalls += 1
            await self._settle(entry_id, seen, stall_content, report)

    async def _settle(self, entry_id: uuid.UUID, seen: int, render: Any, report: LearnReport) -> list[dict[str, Any]]:
        """Rewrite a system entry's text from its latest examples; activate it once it is seen in ``promote_after`` jobs
        and something helped in at least half of the recent cases. Operator text and retired or active status stay as
        they are."""
        row = await self.store.get(entry_id)
        if row is None:
            return []
        examples = [e for e in (row.evidence or {}).get("examples", []) if isinstance(e, dict)][-_MAX_EXAMPLES:]
        changes: dict[str, Any] = {}
        if row.source == "system" and seen > 1:
            changes["content"] = render(examples, seen)
        # a way past it in 1 of 5 cases is no lesson: the dry run against the production jobs activated
        # "COMMAND_REPEATED – what helped: read_file → run_command (1 of 5)" for every repeated command (BUG-307)
        helped = sum(1 for e in examples if e.get("helped"))
        if row.status == "candidate" and seen >= self.policy.promote_after and helped and helped * 2 >= len(examples):
            changes["status"] = "active"
            report.promoted += 1
        if changes:
            await self.store.update(entry_id, **changes)
        return examples

    async def _suspect(
        self,
        signature: str,
        *,
        tool: str,
        code: str,
        examples: list[dict[str, Any]],
        seen: int,
        job_id: uuid.UUID,
        report: LearnReport,
    ) -> None:
        """Models of different kinds that could not get past the same refusal in ``promote_after`` jobs: the tool may be
        wrong (BUG-301 and BUG-305 were such cases). Not for a guard's refusal, and only when most recent cases did not
        get past it. A tool_bug candidate and an open bug record for the operator."""
        models = {m for e in examples for m in e.get("models", [])}
        unresolved = [e for e in examples if not e.get("helped")]
        if code in _GUARDS or seen < self.policy.promote_after or len(models) < 2 or len(unresolved) * 2 < len(examples):
            return
        if await self.store.by_signature(signature, kinds=("tool_bug",)):
            return
        content = (
            f"Suspected tool problem: `{tool}` refused with {code} for {len(models)} models ({', '.join(sorted(models))}) "
            f"in {seen} jobs, and they did not get past it in {len(unresolved)} of {len(examples)} recent cases. "
            "Check whether the tool rejects a correct action before blaming the model."
        )
        await self.store.record_incident(
            signature=signature,
            title=f"Suspected tool bug: {tool} {code}",
            content=content,
            job_id=job_id,
            evidence={"jobs_seen": seen, "examples": unresolved[-3:]},
            kind="tool_bug",
            tags=["suspicion", tool],
        )
        bug_key = f"AUTO-{_sha(signature, 10).upper()}"
        async with self.sm() as s, s.begin():
            if (await s.execute(select(BugRecord.id).where(BugRecord.bug_key == bug_key))).first() is None:
                s.add(
                    BugRecord(
                        bug_key=bug_key,
                        title=f"Verdacht (automatisch): {tool} lehnt wiederholt mit {code} ab"[:300],
                        severity="P3",
                        component="tools",
                        status="open",
                        job_id=job_id,
                        reproduction="\n".join(
                            f"Job {e.get('job')} ({', '.join(e.get('models', []))}): {e.get('count')}× – {e.get('message', '')}"
                            for e in unresolved[-3:]
                        ),
                        expected=f"{tool} führt eine richtige Aktion aus",
                        actual=f"{code} bei {len(models)} Modellen in {seen} Jobs, nicht überwunden",
                        evidence={"signature": signature, "memory": True},
                    )
                )
        report.suspicions += 1

    async def _research(self, job_id: uuid.UUID, run: ResearchRun, report: LearnReport, scope: str | None = None) -> None:
        if not (run.synthesis or "").strip():
            return
        async with self.sm() as s:
            claims = list(
                (
                    await s.execute(
                        select(ResearchClaim)
                        .where(ResearchClaim.research_run_id == run.id, ResearchClaim.confidence >= 0.6)
                        .order_by(ResearchClaim.confidence.desc())
                        .limit(5)
                    )
                ).scalars()
            )
            sources = list(
                (
                    await s.execute(
                        select(ResearchSource)
                        .where(ResearchSource.research_run_id == run.id, ResearchSource.status == "read")
                        .order_by(ResearchSource.authority_score.desc())
                        .limit(5)
                    )
                ).scalars()
            )
        parts = [f"Question: {_clip(run.question, 300)}", _clip(run.synthesis, 2000)]
        if claims:
            parts.append("Claims:\n" + "\n".join(f"- {_clip(c.claim, 300)} (confidence {c.confidence:.1f})" for c in claims))
        if sources:
            parts.append("Sources: " + ", ".join(src.url for src in sources))
        content = "\n\n".join(parts)
        signature = f"research:{_sha(' '.join(run.question.lower().split()), 16)}"
        expires = datetime.now(UTC) + timedelta(days=self.policy.research_days)
        known = await self.store.by_signature(signature, kinds=("research",))
        if known:
            row = known[0]
            if row.source == "research" and row.status != "retired":
                await self.store.update(row.id, content=content, expires_at=expires)
        else:
            await self.store.add(
                kind="research",
                title=_clip(run.question, 300),
                content=content,
                status="active",
                source="research",
                signature=signature,
                scope=scope,
                job_id=job_id,
                evidence={"research_run": str(run.id), "sources": [src.url for src in sources]},
                expires_at=expires,
                tags=["research"],
            )
        report.research += 1

    # ------------------------------------------------------------------------------------------ lessons (D-025)
    async def _lessons(self, report: LearnReport) -> None:
        stmt = (
            select(Job.id)
            .where(Job.status.in_(LESSON_JOBS), ~Job.metadata_.has_key(LESSONS_KEY))
            .order_by(Job.finished_at.asc().nulls_last(), Job.created_at.asc(), Job.id)
            .limit(self.policy.learn_batch)
        )
        if self.policy.lessons_since_days:
            since = datetime.now(UTC) - timedelta(days=self.policy.lessons_since_days)
            stmt = stmt.where(or_(Job.finished_at.is_(None), Job.finished_at >= since))
        async with self.sm() as s:
            pending = list((await s.execute(stmt)).scalars().all())
        for job_id in pending:
            episodes = await self._episodes(job_id)
            if episodes:
                try:
                    async with self._lease():
                        for episode in episodes:
                            await self._lesson(job_id, episode, report)
                except HermclawError as exc:  # the GPU is busy (or the profile unusable): this job waits for a later pass
                    if exc.code != "RESOURCE_WAIT_TIMEOUT":
                        report.errors.append(f"{job_id}: lesson lease {exc.code}")
                    return
            await self._mark(job_id, lessons=True)

    async def _episodes(self, job_id: uuid.UUID) -> list[FixEpisode]:
        async with self.sm() as s:
            attempts = list((await s.execute(select(StepAttempt).where(StepAttempt.job_id == job_id))).scalars())
            goals = {st.id: st.goal for st in (await s.execute(select(Step).where(Step.job_id == job_id))).scalars()}
            calls = list((await s.execute(select(ToolCallRow).where(ToolCallRow.job_id == job_id))).scalars())
        return fix_episodes(attempts, calls, goals, limit=self.policy.lessons_per_job)

    @contextlib.asynccontextmanager
    async def _lease(self) -> AsyncIterator[None]:
        assert self.lesson_model is not None
        resources = self.lesson_model.resources
        if resources is None:
            yield
            return
        async with resources.hold_model(self.lesson_model.profile, wait_timeout=self.policy.lesson_wait_seconds, preemptible=False):
            yield

    async def _lesson(self, job_id: uuid.UUID, episode: FixEpisode, report: LearnReport) -> None:
        assert self.lesson_model is not None
        profile = self.lesson_model.profile
        try:
            draft = await distill(
                self.lesson_model.chat,
                profile.alias,
                episode,
                job_id=job_id,
                max_tokens=min(profile.max_output_tokens, 1024),
                temperature=profile.temperature,
            )
        except HermclawError as exc:  # an unusable answer costs this fix, not the pass
            report.errors.append(f"{job_id}: lesson {exc.code}")
            return
        if not draft.usable():
            report.no_lesson += 1
            return
        evidence = {"job": str(job_id), "attempt": str(episode.attempt_id), "source": episode.source, "model": profile.alias}
        hits = await self.store.search(
            entry_text(draft.title, draft.content()),
            k=1,
            kinds=("lesson", "convention"),
            statuses=("active", "candidate", "retired"),
            min_score=self.policy.lesson_similarity,
            count=False,
        )
        if hits:  # a known lesson: one more job; a retired one stays retired and is not written again
            hit = hits[0]
            seen = await self.store.note_job(hit.id, job_id, evidence)
            row = await self.store.get(hit.id)
            if row is not None and row.status == "candidate" and row.source == "system" and seen >= self.policy.promote_after:
                await self.store.update(hit.id, status="active")
                report.promoted += 1
            report.lessons_seen += 1
            return
        await self.store.add(
            kind="lesson",
            title=draft.title,
            content=draft.content(),
            signature=f"lesson:{_sha(' '.join(draft.title.lower().split()), 16)}",
            job_id=job_id,
            tags=["learned", *[t[:40] for t in draft.applies_to]],
            evidence={"jobs": [str(job_id)], "examples": [evidence]},
        )
        report.lessons += 1

    # ------------------------------------------------------------------------------------------ loop
    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                report = await self.run_once()
                if report.jobs:
                    log.info("learned from finished jobs", extra={k: v for k, v in report.__dict__.items() if k != "errors"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # learning must never take the scheduler down
                log.warning("memory learning failed", extra={"error_type": type(exc).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.policy.learn_interval_seconds)

    def start(self) -> None:
        if not self.policy.learn:
            log.info("learning after jobs disabled (policies.memory.learn = false)")
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="memory-learner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
