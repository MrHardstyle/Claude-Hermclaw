"""Implement step handler: scope → coder loop → scope audit → deterministic verifier → heavy review → runtime commit,
with the bounded correction pipeline (Bauplan §19-§22, Phase 19 + 23).

Correction (P23): a failed verification or a ``fix_required`` review produces a *correction attempt* of the same step
(``step_attempts.kind = correction``) whose input carries the real verifier/review evidence (23.4). Counters live in
PostgreSQL (``steps.correction_count``, ``steps.attempt_count``) and are never lost (23.3). Every attempt is verified
completely again (23.5 regression rerun). When the correction budget is exhausted the step is blocked with
``repeated_verifier_failure`` and the scheduler escalates to the Gemma replanner (23.6).
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.coder.loop import CoderLoop, CoderResult, CoderSettings, StagnationHook, history_from_checkpoint
from hermclaw.context_builder import ContextBuilder, ContextBuilderConfig, CorrectionItem, StepBrief
from hermclaw.contracts.acceptance import AcceptanceCriterion
from hermclaw.contracts.common import StepKind
from hermclaw.contracts.events import EventType
from hermclaw.contracts.review import ReviewContract, enforce_review_invariant
from hermclaw.contracts.scope import ScopeContract, ScopeExpansionRequest
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.verification import VerificationReport
from hermclaw.core.config import HermclawConfig
from hermclaw.core.errors import ConfigError, GitError, HermclawError, ResourceUnavailable
from hermclaw.core.interfaces import CommandExecutor, RepoContextProvider, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.gitops.engine import path_matches
from hermclaw.models.protocols import ChatModel
from hermclaw.persistence.models import Event, Job, JobInput, Step, StepAttempt, StepDependency
from hermclaw.research.findings import research_findings
from hermclaw.runtime.transitions import emit_status
from hermclaw.scheduler.handlers import StepOutcome, StepRunContext
from hermclaw.tools.context import ScopeExpansionOutcome, ToolPermissions
from hermclaw.tools.engine import ToolEngine
from hermclaw.tools.ops import OpsToolProvider
from hermclaw.tournament.settings import TournamentSettings, tournament_of

if TYPE_CHECKING:
    from hermclaw.gitops.engine import GitEngine
    from hermclaw.memory.store import MemoryStore
    from hermclaw.runtime.scratch import ScratchWorkspaces
    from hermclaw.scope.audit import ScopeAuditor
    from hermclaw.scope.engine import ScopeEngine
    from hermclaw.scope.expansion import ScopeExpansionHandler

log = get_logger(__name__)

CODE_STEP_KINDS = frozenset({StepKind.implement.value, StepKind.documentation.value})
#: operations steps (P26–P28): the same coder loop with the capability's operations tools (SSH/DB/container) and the
#: scope engine's rule for the kind (``ssh``/``deploy`` read-only deny-all-write, ``database``/``docker`` scoped files)
OPS_STEP_KINDS = frozenset({StepKind.ssh.value, StepKind.database.value, StepKind.docker.value, StepKind.deploy.value})
TOOL_STEP_KINDS = CODE_STEP_KINDS | OPS_STEP_KINDS
_BLOCK_REASONS = {
    "scope_unavailable": "scope_unavailable",
    "missing_dependency": "missing_dependency",
    "test_conflict": "test_architecture_conflict",
    "requirement_unclear": "step_failed",
    "external_failure": "worker_unavailable",
    "other": "step_failed",
}


def acceptance_defects(report: VerificationReport) -> list[str]:
    """Blocking verifier failures that no change of the code can fix, only a change of the plan: an implementing step
    in a repository with tests whose acceptance has no test criterion at all."""
    out: list[str] = []
    for check in report.failures:
        if check.check_type == "test_evidence" and check.evidence.get("test_evidence_count") == 0:
            out.append(
                f"{check.message} – the plan gave this step no test criterion; give it test evidence that runs the tests "
                "of the files it changes"
            )
    return out


def unmet_change_demands(report: VerificationReport) -> list[str]:
    """The ``must_change`` globs of the failed diff checks when nothing else failed: the step changed files, only not
    every file the plan expected. Empty when any other problem (a test, lint, a forbidden or empty change) failed."""
    globs: list[str] = []
    for check in report.failures:
        ev = check.evidence
        limit = ev.get("max_changed_files")
        if (
            check.check_type != "diff"
            or not ev.get("missing")
            or ev.get("touched_forbidden")
            or not ev.get("changed_count")
            or (isinstance(limit, int) and ev["changed_count"] > limit)
        ):
            return []
        globs.extend(str(g) for g in ev["missing"])
    return list(dict.fromkeys(globs))


def without_change_demands(step: StepContract, globs: Sequence[str]) -> StepContract:
    """``step`` without the ``must_change`` globs, and a constraint that asks the review whether the goal needs them."""
    drop = set(globs)
    acceptance: list[AcceptanceCriterion] = []
    for crit in step.acceptance:
        if crit.type == "diff" and drop & set(crit.must_change):
            keep = [g for g in crit.must_change if g not in drop]
            if keep or crit.must_not_change or crit.max_changed_files is not None:
                acceptance.append(crit.model_copy(update={"must_change": keep}))
            continue
        acceptance.append(crit)
    note = (
        f"The plan also expected changes to {', '.join(globs[:10])}, which this step left unchanged. Judge whether the "
        "step goal requires changing them: if it does, report a blocker finding that names the file and the change it "
        "needs; a file the step only reads (input data, an entry point it runs, tests it runs) needs no change."
    )
    return step.model_copy(update={"acceptance": acceptance, "constraints": [*step.constraints, note]})


# ----------------------------------------------------------------------------- ports (adapters wrap P21/P22/P12)
@dataclass
class VerificationOutcome:
    report: VerificationReport
    run_id: uuid.UUID  # verification_runs.id – required by GitEngine.commit_verified


class VerifierPort(Protocol):
    async def verify(
        self, step: StepContract, workspace: WorkspaceHandle, *, job_id: uuid.UUID, step_id: uuid.UUID, attempt_id: uuid.UUID
    ) -> VerificationOutcome: ...


class ReviewerPort(Protocol):
    def should_review(self, step_kind: str, verifier_passed: bool) -> bool: ...

    async def review(
        self,
        step: StepContract,
        workspace: WorkspaceHandle,
        verification: VerificationReport,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID,
        earlier: Sequence[str] = (),
    ) -> ReviewContract: ...


class ResearchPort(Protocol):
    async def ask(self, question: str, *, job_id: uuid.UUID, step_id: uuid.UUID) -> str: ...


class ModelLeaser(Protocol):
    """The part of :class:`hermclaw.resources.manager.ResourceManager` the handler uses."""

    def hold_model(self, profile: Any, **kw: Any) -> contextlib.AbstractAsyncContextManager[Any]: ...


ExecutorFactory = Callable[[WorkspaceHandle, StepContract], Awaitable[CommandExecutor]]
StagnationFactory = Callable[[StepRunContext], Awaitable[StagnationHook]]


@dataclass
class ImplementDeps:
    git: GitEngine
    scope_engine: ScopeEngine
    scope_auditor: ScopeAuditor
    expansion: ScopeExpansionHandler
    chat: ChatModel
    repo: RepoContextProvider
    executor_factory: ExecutorFactory
    verifier: VerifierPort
    reviewer: ReviewerPort | None = None
    research: ResearchPort | None = None
    stagnation_factory: StagnationFactory | None = None
    coder_settings: CoderSettings = field(default_factory=CoderSettings)
    resources: ModelLeaser | None = None  # P09: GPU model leases (coder during the loop, heavy during review)
    lease_wait_seconds: float = 3600.0
    lease_ttl_seconds: float = 600.0
    lease_keeper_interval: float | None = None  # None: resource manager default (heartbeat-derived)
    ops: OpsToolProvider | None = None  # P26–P28 operations tools (SSH/DB/container), gated by step capability
    scratch: ScratchWorkspaces | None = None  # R-026: scratch workspace for jobs without a repository
    artifacts_dir: Path | None = None  # ops tool outputs are stored as ``ops_output`` step artifacts
    memory: MemoryStore | None = None  # Hermclaw's knowledge (D-019): the recall tool


# ----------------------------------------------------------------------------- helpers
def step_contract(row: Step, scope: ScopeContract | None = None) -> StepContract:
    return StepContract.model_validate(
        {
            "id": row.id,
            "job_id": row.job_id,
            "step_key": row.step_key,
            "title": row.title,
            "kind": row.kind,
            "capability": row.capability,
            "goal": row.goal,
            "status": row.status,
            "risk": row.risk,
            "constraints": list(row.constraints or []),
            "acceptance": list(row.acceptance or []),
            "repo_hints": list(row.repo_hints or []),
            "scope": scope,
            "turn_budget": row.turn_budget,
            "network": row.network,
        }
    )


def _item_dict(i: CorrectionItem) -> dict[str, str]:
    return {
        "source": i.source,
        "label": i.label,
        "message": i.message,
        "path": i.path,
        "suggested_fix": i.suggested_fix,
        "command": i.command,
    }


def corrections_from_input(data: dict[str, Any]) -> list[CorrectionItem]:
    out: list[CorrectionItem] = []
    for d in data.get("items") or []:
        if isinstance(d, dict) and d.get("message"):
            out.append(
                CorrectionItem(
                    source=str(d.get("source", "verifier")),
                    label=str(d.get("label", "")),
                    message=str(d["message"]),
                    path=str(d.get("path", "")),
                    suggested_fix=str(d.get("suggested_fix", "")),
                    command=str(d.get("command", "")),
                )
            )
    return out


@dataclass
class _Run:
    """Per-attempt state passed between the pipeline stages."""

    ctx: StepRunContext
    row: Step
    step: StepContract
    ws: Any
    handle: WorkspaceHandle
    earlier_findings: tuple[str, ...] = ()  # review findings this correction attempt had to address (BUG-284)
    review: bool = True  # False in a coder tournament run: the review would measure a second model


class _Callbacks:
    def __init__(self, deps: ImplementDeps, ctx: StepRunContext, workspace: WorkspaceHandle) -> None:
        self.deps, self.ctx, self.workspace = deps, ctx, workspace

    async def on_research(self, question: str) -> str:
        if self.deps.research is None:
            return "research is not available for this step; continue with repository evidence or block_step"
        return await self.deps.research.ask(question, job_id=self.ctx.job_id, step_id=self.ctx.step_id)

    async def on_scope_expansion(self, request: ScopeExpansionRequest) -> ScopeExpansionOutcome:
        decision = await self.deps.expansion.handle(self.ctx.step_id, request, self.workspace, attempt_id=self.ctx.attempt_id)
        return ScopeExpansionOutcome(granted=decision.granted, message=decision.reason, contract=decision.contract, data=decision.to_dict())

    async def on_replan(self, reason: str) -> None:
        return None  # request_replan is terminal; the handler turns it into a blocked outcome


# ----------------------------------------------------------------------------- handler
class ImplementStepHandler:
    kinds = TOOL_STEP_KINDS

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], config: HermclawConfig, deps: ImplementDeps) -> None:
        self.sm = sessionmaker
        self.config = config
        self.deps = deps

    async def _load(self, step_id: uuid.UUID) -> Step:
        async with self.sm() as s:
            return (await s.execute(select(Step).where(Step.id == step_id))).scalar_one()

    async def _job_text(self, job_id: uuid.UUID) -> str:
        """Title, prompt and constraints of the job: which third-party packages the job itself asks for."""
        async with self.sm() as s:
            job = await s.get(Job, job_id)
            inputs = (await s.execute(select(JobInput.content).where(JobInput.job_id == job_id))).scalars().all()
        if job is None:
            return ""
        return "\n".join([job.title, job.prompt, *(str(i) for i in inputs)])

    async def _job_request(self, job_id: uuid.UUID) -> str:
        """The user's request for the whole job (title and prompt) for the coder's STEP GOAL section (BUG-212)."""
        async with self.sm() as s:
            job = await s.get(Job, job_id)
        return "" if job is None else "\n".join(p for p in (job.title, job.prompt) if p)

    async def _own_changes(self, step_id: uuid.UUID) -> set[str]:
        """Paths this step's own attempts changed (``file.changed`` events): never adopted as another step's leftovers,
        so an out-of-scope change of its own stays a violation."""
        async with self.sm() as s:
            payloads = (
                await s.execute(select(Event.payload).where(Event.step_id == step_id, Event.event_type == EventType.FILE_CHANGED))
            ).scalars()
            return {str(c.get("path")) for p in payloads for c in (p or {}).get("changes") or [] if isinstance(c, dict) and c.get("path")}

    async def _path_claims(self, step_id: uuid.UUID) -> tuple[list[str], list[str]]:
        """(the ``allowed_new_paths`` of the job's other open steps, the paths this step declares – its own
        ``allowed_new_paths`` and ``repo_hints``) – who a leftover file belongs to (BUG-257)."""
        async with self.sm() as s:
            row = await s.get(Step, step_id)
            if row is None:
                return [], []
            others = (
                await s.execute(
                    select(Step.allowed_new_paths).where(
                        Step.job_id == row.job_id,
                        Step.id != step_id,
                        Step.superseded.is_(False),
                        Step.status.not_in(("completed", "cancelled")),
                    )
                )
            ).scalars()
            claims = [str(p) for paths in others for p in paths or [] if p]
            mine = [str(p) for p in [*(row.allowed_new_paths or []), *(row.repo_hints or [])] if p]
        return claims, mine

    async def _dependency_files(self, step_id: uuid.UUID, limit: int = 12) -> list[str]:
        """Files the completed steps ``step_id`` depends on (transitively, nearest first) committed – the interfaces
        this step builds on (BUG-242) – then those of the job's other completed steps, newest first: they share the
        workspace, and the planner left the frontend step without a dependency on the collector step (BUG-250)."""
        async with self.sm() as s:
            job_id = (await s.execute(select(Step.job_id).where(Step.id == step_id))).scalar_one_or_none()
            if job_id is None:
                return []
            rows = (await s.execute(select(Step).where(Step.job_id == job_id, Step.superseded.is_(False)))).scalars().all()
            edges = (await s.execute(select(StepDependency).where(StepDependency.step_id.in_([r.id for r in rows])))).scalars().all()
        by_id = {r.id: r for r in rows}
        deps: dict[uuid.UUID, list[uuid.UUID]] = {}
        for e in edges:
            deps.setdefault(e.step_id, []).append(e.depends_on_step_id)
        out: list[str] = []
        seen: set[uuid.UUID] = {step_id}
        frontier = list(deps.get(step_id, []))
        while frontier and len(out) < limit:
            nxt: list[uuid.UUID] = []
            for sid in sorted(frontier, key=lambda i: by_id[i].step_key if i in by_id else ""):
                if sid in seen or sid not in by_id:
                    continue
                seen.add(sid)
                row = by_id[sid]
                if row.status == "completed":
                    files = (row.result or {}).get("files") or []
                    out.extend(f for f in files if isinstance(f, str) and f not in out)
                nxt.extend(deps.get(sid, []))
            frontier = nxt
        others = sorted(
            (r for r in rows if r.id not in seen and r.status == "completed"),
            key=lambda r: r.finished_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        for row in others:
            if len(out) >= limit:
                break
            files = (row.result or {}).get("files") or []
            out.extend(f for f in files if isinstance(f, str) and f not in out)
        return out[:limit]

    async def _workspace(self, job_id: uuid.UUID) -> tuple[Any, WorkspaceHandle] | None:
        for ws in await self.deps.git.list_workspaces(job_id):
            if ws.status not in ("archived", "cleaned", "removed", "failed"):
                return ws, await self.deps.git.handle(ws)
        return None

    async def _status(self, ctx: StepRunContext, text: str) -> None:
        async with self.sm() as s:
            await emit_status(s, ctx.job_id, text, step_id=ctx.step_id)
            await s.commit()

    async def _correction(  # noqa: PLR0917 - private, called with the evidence tuple
        self, ctx: StepRunContext, row: Step, source: str, items: list[CorrectionItem], code: str, detail: str
    ) -> StepOutcome:
        """Schedule a bounded correction attempt carrying the evidence, or escalate to the replanner."""
        limit = self.config.policies.correction.max_corrections_per_step
        evidence = {"source": source, "items": [_item_dict(i) for i in items[:30]], "detail": DEFAULT_REDACTOR.text(detail)[:2000]}
        if row.correction_count >= limit or row.attempt_count >= row.max_attempts:
            return StepOutcome(
                "blocked",
                summary=(
                    f"correction budget exhausted ({row.correction_count}/{limit} corrections, "
                    f"{row.attempt_count}/{row.max_attempts} attempts)"
                ),
                error_code=code,
                error_message=DEFAULT_REDACTOR.text(detail)[:2000],
                replan_reason="repeated_verifier_failure",
                replan_evidence=evidence,
            )
        async with self.sm() as s:
            await s.execute(
                update(StepAttempt).where(StepAttempt.id == ctx.attempt_id).values(correction_input={"pending": True, **evidence})
            )
            await s.execute(update(Step).where(Step.id == row.id).values(correction_count=Step.correction_count + 1))
            await append_event(
                s,
                EventType.CORRECTION_STARTED,
                source_type="coder",
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                attempt_id=ctx.attempt_id,
                payload={"source": source, "items": len(items), "correction": row.correction_count + 1, "limit": limit},
            )
            await s.commit()
        return StepOutcome(
            "failed",
            summary=f"{source} requires correction",
            error_code=code,
            error_message=detail[:2000],
            retryable=True,
            retry_delay_seconds=0,
        )

    async def run(self, ctx: StepRunContext) -> StepOutcome:
        row = await self._load(ctx.step_id)
        found = await self._workspace(ctx.job_id)
        if found is None and self.deps.scratch is not None:
            scratch = await self.deps.scratch.ensure(ctx.job_id)  # None for jobs with a repository (no workspace yet)
            if scratch is not None:
                found = scratch, await self.deps.git.handle(scratch)
        if found is None:
            return StepOutcome(
                "blocked", error_code="NO_WORKSPACE", error_message="job has no active workspace", replan_reason="missing_dependency"
            )
        ws, handle = found
        # step baseline: changes of *this* step are measured against the HEAD at step start (earlier steps of the
        # job are already committed), so scope checks, diffs and verification never attribute them to this step
        fresh = await self.deps.git.get_workspace(ws.id)
        if fresh.head_sha and fresh.head_sha != handle.base_sha:
            handle = dataclasses.replace(handle, base_sha=fresh.head_sha)
        # scope (P15): reuse the active version on retries/corrections, create it on the first attempt
        contract = await self.deps.scope_engine.current_contract(ctx.step_id)
        if contract is None:
            decision = await self.deps.scope_engine.create_scope(ctx.step_id, handle)
            if not decision.runnable:
                return StepOutcome(
                    "blocked",
                    error_code="SCOPE_UNAVAILABLE",
                    error_message=decision.reason,
                    replan_reason="scope_unavailable",
                    replan_evidence={"reason_code": decision.reason_code, **(decision.evidence or {})},
                )
            contract = decision.contract
        # leftovers another open step of the job declares as its new files are that step's work, not whichever step
        # runs first: set aside until the owner starts (live G1: S002 took over S003's frontend and S004's scripts,
        # BUG-257)
        claims, mine = await self._path_claims(ctx.step_id)
        await self.deps.git.unpark_paths(ws, mine, step_id=ctx.step_id)
        changed = await self.deps.git.changed_files(handle)
        foreign = [p for p in changed if path_matches(p, claims) and not path_matches(p, mine)]
        if foreign and await self.deps.git.park_paths(ws, foreign, step_id=ctx.step_id):
            changed = await self.deps.git.changed_files(handle)
        # uncommitted changes of an earlier unfinished step are this step's to finish, not a scope violation – checked on
        # every attempt: another step can run (and be blocked) between two attempts of this one (live G1, BUG-255)
        own = await self._own_changes(ctx.step_id)
        leftovers = [p for p in changed if p not in own]
        adopted = await self.deps.scope_engine.adopt_leftovers(ctx.step_id, leftovers) if leftovers else None
        contract = adopted or contract
        step = step_contract(row, contract)
        findings = await research_findings(self.sm, ctx.step_id)
        if findings:  # cited results of the research steps this step depends on (P12 → P17)
            step = step.model_copy(update={"constraints": [*step.constraints, *findings]})
        try:
            capability = self.config.capabilities.get(row.capability)
        except ConfigError:
            capability = None
        guard = await self.deps.scope_engine.guard_for(ctx.step_id)
        executor = await self.deps.executor_factory(handle, step)
        engine = ToolEngine(
            self.sm,
            self.config.policies,
            handle,
            guard,
            executor,
            self.deps.git.reader(),
            self.deps.repo,
            _Callbacks(self.deps, ctx, handle),
            permissions=ToolPermissions.for_step(
                kind=row.kind,
                network=row.network,
                turn_budget=row.turn_budget,
                capability=capability,
                dependency_install=self.config.policies.sandbox.egress.enabled,  # D-014
                job_text=await self._job_text(ctx.job_id),
            ),
            ops=self.deps.ops,
            capability=row.capability,
            artifacts_dir=self.deps.artifacts_dir,
            memory=self.deps.memory,
            memory_scopes=[f"repo:{handle.repository_key}"],
        )
        tournament = await self._tournament(ctx.job_id)
        coder_alias = tournament.coder_alias if tournament is not None else None
        builder_cfg = ContextBuilderConfig.from_config(self.config, alias=coder_alias)
        builder = ContextBuilder(self.deps.repo, self.deps.git.reader(), builder_cfg)
        settings = CoderSettings(
            **{
                **self.deps.coder_settings.__dict__,
                "max_turns": min(row.turn_budget, self.config.policies.coder.max_turns),
                # request exactly the output the context budget reserves: the loop's default (6144) cut answers off at
                # 6144 tokens although the profile allowed and the budget reserved 8192 (live G1, BUG-235)
                "max_output_tokens": builder_cfg.max_output_tokens,
                **({"alias": coder_alias} if coder_alias else {}),
            }
        )
        stagnation = await self.deps.stagnation_factory(ctx) if self.deps.stagnation_factory else None
        loop = CoderLoop(self.deps.chat, builder, engine, settings=settings, stagnation=stagnation, sessionmaker=self.sm)
        start, history, last_failure = (
            history_from_checkpoint(ctx.checkpoint.get("coder") or {}) if ctx.checkpoint.get("coder") else (0, [], None)
        )
        pending = ctx.attempt_kind == "correction" or bool(ctx.correction_input.get("pending"))  # a resumed correction too
        corrections = corrections_from_input(ctx.correction_input) if pending else []
        if ctx.attempt_kind == "resume" and any(r.mutated_paths for r in history):
            corrections = await self._fresh_verifier_items(ctx, step, handle, corrections)
        engine.expect_corrections(c.path for c in corrections)  # complete_step reminds of untouched files (BUG-258)
        engine.expect_rechecks(c.command for c in corrections if c.source == "verifier")  # and re-runs failed checks (BUG-276)
        engine.expect_secret_rechecks(  # and re-scans files with flagged secrets (BUG-279)
            c.path for c in corrections if c.source == "verifier" and c.label.startswith(("secrets:", "security:"))
        )
        await self._status(ctx, f"Coder startet {row.step_key} ({ctx.attempt_kind}, max {settings.max_turns} Turns)")
        try:
            async with self._model(ctx, "coder", preemptible=True, alias=coder_alias):
                result = await loop.run(
                    step=dataclasses.replace(
                        StepBrief.from_contract(step),
                        job_request=await self._job_request(ctx.job_id),
                        dependency_files=await self._dependency_files(ctx.step_id),
                    ),
                    workspace=handle,
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    token=ctx.token,
                    history=history,
                    start_turn=start + 1,
                    last_failure=last_failure,
                    correction=corrections,
                )
        except ResourceUnavailable as exc:
            return StepOutcome("failed", error_code=exc.code, error_message=exc.message, retryable=True)
        try:
            earlier = tuple(c.message for c in corrections if c.source == "review")
            review = tournament is None or tournament.review
            return await self._after_loop(_Run(ctx, row, step, ws, handle, earlier, review=review), result)
        except ResourceUnavailable as exc:  # heavy-review lease not granted in time
            return StepOutcome("failed", error_code=exc.code, error_message=exc.message, retryable=True)

    async def _fresh_verifier_items(
        self, ctx: StepRunContext, step: StepContract, handle: WorkspaceHandle, corrections: list[CorrectionItem]
    ) -> list[CorrectionItem]:
        """The verifier items of a resumed correction, re-derived from the current work tree: the interrupted attempt
        changed files and may have fixed some findings already (live G1: the resumed S002 tried to remove the unused
        imports attempt 2 had removed, until the stagnation guard blocked the step, BUG-259). Other items stay."""
        if not any(c.source == "verifier" for c in corrections):
            return corrections
        await self._status(ctx, "Verifier prüft den Stand vor dem Fortsetzen")
        outcome = await self.deps.verifier.verify(step, handle, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id)
        kept = [c for c in corrections if c.source != "verifier"]
        return kept if outcome.report.passed else [*kept, *CorrectionItem.from_verification(outcome.report)]

    async def _tournament(self, job_id: uuid.UUID) -> TournamentSettings | None:
        async with self.sm() as s:
            job = await s.get(Job, job_id)
        return None if job is None else tournament_of(job.metadata_)

    def _reviews(self, run: _Run) -> bool:
        return run.review and self.deps.reviewer is not None and self.deps.reviewer.should_review(run.row.kind, True)

    def _model(
        self, ctx: StepRunContext, role: str, *, preemptible: bool, alias: str | None = None
    ) -> contextlib.AbstractAsyncContextManager[Any]:
        """Hold the GPU model lease for ``role`` (or the profile ``alias`` names); a preemption request (video/image)
        makes the coder checkpoint."""
        if self.deps.resources is None:
            return contextlib.nullcontext()
        profile = self.config.models.by_alias(alias) if alias else self.config.models.by_role(role)

        def on_preempt(*_: Any) -> None:
            ctx.token.request_checkpoint(f"GPU needed by a higher-priority job ({role} lease preempted)")

        return self.deps.resources.hold_model(
            profile,
            on_preempt=on_preempt if preemptible else None,
            job_id=ctx.job_id,
            step_id=ctx.step_id,
            ttl_seconds=self.deps.lease_ttl_seconds,
            wait_timeout=self.deps.lease_wait_seconds,
            preemptible=preemptible,
            keeper_interval=self.deps.lease_keeper_interval,
        )

    async def _after_loop(self, run: _Run, result: CoderResult) -> StepOutcome:
        ctx, row = run.ctx, run.row
        if result.outcome == "cancelled":
            return StepOutcome("cancelled", summary=result.detail or "cancelled")
        if result.outcome == "checkpointed":
            return StepOutcome("checkpointed", summary="paused", checkpoint={"coder": result.checkpoint()})
        if result.outcome == "model_failed":
            return StepOutcome("failed", error_code=result.error_code or "MODEL_FAILED", error_message=result.detail, retryable=True)
        if result.outcome == "blocked":
            block = result.block or {}
            reason = _BLOCK_REASONS.get(str(block.get("reason_code", "other")), "step_failed")
            return StepOutcome(
                "blocked",
                error_code="CODER_BLOCKED",
                error_message=str(block.get("message", ""))[:2000],
                replan_reason=reason,
                replan_evidence={"block": block},
            )
        if result.outcome == "replan":
            return StepOutcome(
                "blocked",
                error_code="CODER_REQUESTED_REPLAN",
                error_message=result.replan_reason,
                replan_reason="step_failed",
                replan_evidence={"coder_reason": result.replan_reason},
            )
        if result.outcome == "stagnated":
            items = [
                CorrectionItem(
                    source="stagnation", label="stagnation", message=f"stagnation detected: {result.detail}. Change the approach."
                )
            ]
            if result.recommendation == "heavy_review" and self.deps.reviewer is not None and run.review:
                return await self._review_and_correct(run, VerificationReport(passed=False, checks=[], summary="stagnation"), items)
            if result.recommendation == "heavy_review":  # no review in a tournament run: the stagnation note is the correction
                return await self._correction(ctx, row, "stagnation", items, "STAGNATION", f"stagnation: {result.detail}")
            if result.recommendation == "research" and self.deps.research is not None:
                question = (
                    f"Step '{row.title}' ({row.kind}) is stuck: {result.detail}. "
                    f"Last failure: {(result.last_failure or 'n/a')[:600]}. Which documented approach or API usage solves this?"
                )
                summary = await self.deps.research.ask(DEFAULT_REDACTOR.text(question), job_id=ctx.job_id, step_id=ctx.step_id)
                items.append(CorrectionItem(source="research", label="research result", message=summary[:3000]))
                return await self._correction(ctx, row, "stagnation", items, "STAGNATION", "stagnation escalated to research")
            return StepOutcome(
                "blocked",
                error_code="STAGNATION",
                error_message=result.detail,
                replan_reason="stagnation",
                replan_evidence={"recommendation": result.recommendation, "history": [r.tool for r in result.history][-10:]},
            )
        if result.outcome == "budget_exhausted":
            items = [
                CorrectionItem(
                    source="runtime",
                    label="turn budget",
                    message="the previous attempt used all turns without complete_step; finish faster",
                )
            ]
            if any(r.mutated_paths for r in result.history):
                # the verifier, not the coder's complete_step, decides whether the step is done: the coder may have
                # finished the work in its last turn (live G1: 17 tests passed in turn 20 of 20, the job failed, BUG-230)
                await self._status(ctx, f"Turn-Budget von {row.step_key} erschöpft – Verifier prüft die Änderungen")
                return await self._verify_review_commit(run, result, budget_items=items)
            if result.last_failure:
                items.append(CorrectionItem(source="runtime", label="last failure", message=result.last_failure[:1500]))
            return await self._correction(ctx, row, "budget", items, "TURN_BUDGET_EXHAUSTED", result.detail)
        return await self._verify_review_commit(run, result)

    async def _verify_review_commit(
        self, run: _Run, result: CoderResult, *, budget_items: Sequence[CorrectionItem] = (), waived: Sequence[str] = ()
    ) -> StepOutcome:
        ctx, row, step, handle = run.ctx, run.row, run.step, run.handle
        audit = await self.deps.scope_auditor.audit_status(
            ctx.step_id, await self.deps.git.reader().status(handle), attempt_id=ctx.attempt_id
        )
        if not audit.ok:
            items = [
                CorrectionItem(source="scope", label="scope violation", message=str(v.get("reason") or v), path=str(v.get("path", "")))
                for v in audit.violations
            ]
            return await self._correction(ctx, row, "scope", items, "SCOPE_VIOLATION", f"{len(audit.violations)} out-of-scope change(s)")
        await self._status(ctx, f"Verifier prüft {row.step_key}")
        outcome = await self.deps.verifier.verify(step, handle, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id)
        if not outcome.report.passed:
            defects = acceptance_defects(outcome.report)
            if defects and len(defects) == len(outcome.report.failures):
                # the code may be right; only the plan can change the step's acceptance: a correction attempt could
                # never pass (live job 04820ff9, analysis 2026-10-16, A6)
                detail = "; ".join(defects)
                return StepOutcome(
                    "blocked",
                    summary="acceptance criteria of the step are incomplete",
                    error_code="ACCEPTANCE_DEFECT",
                    error_message=DEFAULT_REDACTOR.text(detail)[:2000],
                    replan_reason="repeated_verifier_failure",
                    replan_evidence={"source": "verifier", "acceptance_defect": True, "detail": DEFAULT_REDACTOR.text(detail)[:2000]},
                )
            unmet = [] if waived else unmet_change_demands(outcome.report)
            if unmet and self._reviews(run):
                # live M2: the plan demanded changes to the input data and the entry point the step only reads; the code
                # was right (27 of 30 checks passed), two corrections and two replans could not change the plan's demand.
                # Whether the goal needs those files is the review's judgement, on the step without the demand (BUG-278)
                await self._status(ctx, f"{row.step_key}: Review prüft, ob {', '.join(unmet[:3])} geändert werden muss")
                return await self._verify_review_commit(
                    dataclasses.replace(run, step=without_change_demands(step, unmet)), result, budget_items=budget_items, waived=unmet
                )
            items = [*budget_items, *CorrectionItem.from_verification(outcome.report)]
            return await self._correction(ctx, row, "verifier", items, "VERIFIER_FAILED", outcome.report.summary or "verification failed")
        if self.deps.reviewer is not None and self._reviews(run):
            await self._status(ctx, f"Heavy Review für {row.step_key}")
            async with self._model(ctx, "heavy", preemptible=False):
                raw_review = await self.deps.reviewer.review(
                    step,
                    handle,
                    outcome.report,
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    earlier=run.earlier_findings,
                )
            review, _ = enforce_review_invariant(raw_review)
            if review.verdict != "pass":
                return await self._correction(
                    ctx,
                    row,
                    "review",
                    list(CorrectionItem.from_review(review)),
                    "REVIEW_FIX_REQUIRED",
                    review.summary or "review requires fixes",
                )
        if waived:
            async with self.sm() as s:
                await append_event(
                    s,
                    EventType.ACCEPTANCE_WAIVED,
                    source_type="coder",
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    payload={"must_change": list(waived)[:50], "reason": "the review found the step goal met without changing them"},
                )
                await s.commit()
        return await self._commit(run, outcome, result)

    async def _review_and_correct(self, run: _Run, report: VerificationReport, items: list[CorrectionItem]) -> StepOutcome:
        ctx, row, step, handle = run.ctx, run.row, run.step, run.handle
        assert self.deps.reviewer is not None
        async with self._model(ctx, "heavy", preemptible=False):
            raw_review = await self.deps.reviewer.review(
                step, handle, report, job_id=ctx.job_id, step_id=ctx.step_id, attempt_id=ctx.attempt_id
            )
        review, _ = enforce_review_invariant(raw_review)
        return await self._correction(
            ctx, row, "review", items + list(CorrectionItem.from_review(review)), "STAGNATION", "stagnation escalated to heavy review"
        )

    async def _commit(self, run: _Run, outcome: VerificationOutcome, result: CoderResult) -> StepOutcome:
        ctx, row, step, ws = run.ctx, run.row, run.step, run.ws
        summary = str(((result.completion or {}).get("report") or {}).get("summary") or "step completed")
        try:
            staged = await self.deps.git.stage_allowed(ws, step.scope or ScopeContract(), step_id=ctx.step_id)
            if staged.refused:
                items = [
                    CorrectionItem(source="scope", label="staging refused", message=str(r), path=getattr(r, "path", ""))
                    for r in staged.refused
                ]
                return await self._correction(
                    ctx, row, "scope", items, "STAGING_REFUSED", f"{len(staged.refused)} path(s) refused at staging"
                )
            if not staged.staged:
                return StepOutcome(
                    "completed", summary=summary, result={"commit": None, "verification_run_id": str(outcome.run_id), "note": "no changes"}
                )
            commit = await self.deps.git.commit_verified(
                ws, f"{row.step_key}: {row.title}"[:200], outcome.run_id, step_id=ctx.step_id, scope=step.scope
            )
        except GitError as exc:
            return StepOutcome("failed", error_code=exc.code, error_message=DEFAULT_REDACTOR.text(exc.message), retryable=False)
        except HermclawError as exc:  # pragma: no cover - defensive
            return StepOutcome("failed", error_code=exc.code, error_message=DEFAULT_REDACTOR.text(exc.message), retryable=True)
        return StepOutcome(
            "completed",
            summary=summary,
            result={"commit": commit.sha, "files": commit.files, "verification_run_id": str(outcome.run_id), "turns": result.turns},
        )
