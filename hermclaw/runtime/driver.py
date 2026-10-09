"""Runtime job driver: job phases before and after the step DAG (Bauplan §1, §11, §15, §16, §27).

``prepare``  queued → inventory (workspace) → discovering (triage, inventory, retrieval) → planning (Gemma) → running
``finalize`` committing: base check / rebase + regression rerun → push + merge request → final report
``replan``   failure package → Gemma replanner (new plan version, completed steps preserved)

The scheduler owns *when* these run; this driver owns *what* they do. Every phase is idempotent so a crashed
orchestrator can call it again (scheduler startup recovery).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import JobStatus, Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.config import HermclawConfig
from hermclaw.core.errors import GitError, HermclawError, MergeConflictError, ModelError
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.core.settings import get_settings
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext, ChatMessage, ChatModel
from hermclaw.persistence.models import Job, Step
from hermclaw.planner import ContextSnippet, PlanConflict, PlannerError, PlannerInput, ReplanLimitReached, ReplanTrigger
from hermclaw.planner.replan_contract import REPLAN_REASONS
from hermclaw.runtime.ports import RegressionCheck, RepoIntel
from hermclaw.runtime.report import collect_report_data, render_report, write_report_artifact
from hermclaw.runtime.state_machines import can_transition_job
from hermclaw.runtime.transitions import emit_status, transition_job
from hermclaw.scheduler.handlers import CancelToken, JobPhaseResult

if TYPE_CHECKING:
    from hermclaw.gitops.engine import GitEngine
    from hermclaw.gitops.types import WorkspaceInfo
    from hermclaw.planner import Planner, Replanner

log = get_logger(__name__)

_PREPARE_ORDER = (JobStatus.queued, JobStatus.inventory, JobStatus.discovering, JobStatus.researching, JobStatus.planning)
_TEST_FRAMEWORKS = ("pytest", "unittest", "npm", "phpunit", "go", "cargo", "generic")


class TriageResult(BaseModel):
    """Fast-router task triage (Bauplan §3.1): classification only, never architecture decisions."""

    intent: Literal["code_change", "research", "administration", "database", "container", "media", "documentation", "mixed"]
    needs_repository: bool = True
    needs_research: bool = False
    risk: Literal["low", "medium", "high"] = "medium"
    summary: str = Field(default="", max_length=600)


@dataclass
class DriverSettings:
    context_budget_chars: int = 24_000
    triage_alias: str = "fast-router"
    triage_timeout_seconds: float = 120.0
    triage_enabled: bool = True
    update_strategy: Literal["rebase", "merge"] = "rebase"
    create_merge_request: bool | None = None  # None: GitLab policy default of the git engine
    max_known_paths: int = 20_000
    artifacts_dir: Path | None = None  # default: settings.artifacts_dir


def _paths_from_inventory(inv: dict[str, Any], limit: int) -> list[str]:
    """Repository file list from an inventory (``files`` as strings or ``{"path": …}`` objects)."""
    out: list[str] = []
    for key in ("files", "paths"):
        for item in inv.get(key) or []:
            p = item.get("path") if isinstance(item, dict) else item
            if isinstance(p, str) and p and p not in out:
                out.append(p)
                if len(out) >= limit:
                    return out
    return out


def _test_hints(inv: dict[str, Any]) -> tuple[str | None, str | None, list[str]]:
    raw_tests = inv.get("tests")
    tests: dict[str, Any] = raw_tests if isinstance(raw_tests, dict) else {}
    cmd = inv.get("test_command") or tests.get("command")
    fw = inv.get("test_framework") or tests.get("framework")
    if isinstance(fw, list):
        fw = fw[0] if fw else None
    framework = fw if fw in _TEST_FRAMEWORKS else ("generic" if fw else None)
    files = [f for f in (tests.get("files") or []) if isinstance(f, str)][:200]
    return (cmd if isinstance(cmd, str) else None), framework, files


def replan_reason_code(reason: str, evidence: dict[str, Any]) -> str:
    """Map scheduler evidence to a planner ``ReplanReason`` deterministically (generic, no task rules)."""
    if reason and reason != "blocked_or_failed_steps":
        return "operator_request"
    codes: list[str] = []
    for f in evidence.get("failed_steps") or []:
        res = f.get("result") or {}
        rr = res.get("replan_reason")
        if isinstance(rr, str) and rr in REPLAN_REASONS:
            return rr
        if f.get("error_code"):
            codes.append(str(f["error_code"]).upper())
    table = (
        (("SCOPE",), "scope_unavailable"),
        (("STAGNATION",), "stagnation"),
        (("VERIFIER", "CORRECTION_LIMIT", "REVIEW"), "repeated_verifier_failure"),
        (("WORKER", "WOL", "NO_HANDLER", "RESOURCE"), "worker_unavailable"),
        (("MERGE_CONFLICT", "STALE_BASE", "BASE_"), "repository_changed"),
        (("RESEARCH",), "research_changed_assumptions"),
        (("TEST_ARCH",), "test_architecture_conflict"),
    )
    for code in codes:
        if code == "DEPENDENCY_FAILED":
            continue
        for needles, mapped in table:
            if any(n in code for n in needles):
                return mapped
    if codes and all(c == "DEPENDENCY_FAILED" for c in codes):
        return "missing_dependency"
    return "step_failed"


class RuntimeJobDriver:
    """Implements :class:`hermclaw.scheduler.handlers.JobDriver`."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig,
        *,
        planner: Planner,
        replanner: Replanner,
        git: GitEngine | None = None,
        repo_intel: RepoIntel | None = None,
        chat: ChatModel | None = None,
        regression: RegressionCheck | None = None,
        settings: DriverSettings | None = None,
    ) -> None:
        self.sm = sessionmaker
        self.config = config
        self.planner = planner
        self.replanner = replanner
        self.git = git
        self.repo_intel = repo_intel
        self.chat = chat
        self.regression = regression
        self.settings = settings or DriverSettings()

    # ------------------------------------------------------------------ helpers
    async def _advance(self, job_id: uuid.UUID, target: JobStatus, line: str) -> None:
        """Move forward through the preparation states only (re-entrant after a crash)."""
        async with self.sm() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
            cur = JobStatus(job.status)
            if cur == target or (cur in _PREPARE_ORDER and _PREPARE_ORDER.index(cur) > _PREPARE_ORDER.index(target)):
                return
            if can_transition_job(cur, target):
                await transition_job(s, job, target, reason="preparation", actor="driver", status_line=line)
                await s.commit()

    async def _job(self, job_id: uuid.UUID) -> Job:
        async with self.sm() as s:
            return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()

    async def _set_meta(self, job_id: uuid.UUID, **values: Any) -> None:
        async with self.sm() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
            job.metadata_ = {**(job.metadata_ or {}), **values}
            await s.commit()

    async def _workspace(self, job: Job) -> WorkspaceInfo | None:
        if self.git is None or job.repository_id is None:
            return None
        for ws in await self.git.list_workspaces(job.id):
            if ws.status not in ("archived", "cleaned", "removed", "failed"):
                return ws
        return None

    async def _ensure_workspace(self, job: Job) -> WorkspaceInfo | None:
        if self.git is None or job.repository_id is None:
            return None
        existing = await self._workspace(job)
        if existing is not None:
            return existing
        return await self.git.create_workspace(job.id, job.repository_id, base_branch=job.base_branch, slug=job.title)

    async def _triage(self, job: Job) -> dict[str, Any] | None:
        if not self.settings.triage_enabled or self.chat is None:
            return None
        if (job.metadata_ or {}).get("triage"):
            return dict(job.metadata_["triage"])
        prompt = (
            "Classify this software/infrastructure job. Answer only with the JSON object.\n"
            "intent: code_change|research|administration|database|container|media|documentation|mixed; "
            "needs_repository; needs_research (external docs needed); risk low|medium|high; summary (one sentence).\n\n"
            f"Title: {DEFAULT_REDACTOR.text(job.title)}\nTask:\n{DEFAULT_REDACTOR.text(job.prompt)[:6000]}"
        )
        try:
            res = await self.chat.structured(
                self.settings.triage_alias,
                [ChatMessage("system", "You are a fast task router. No explanations."), ChatMessage("user", prompt)],
                TriageResult,
                ctx=CallContext(purpose="triage", job_id=job.id),
                max_tokens=400,
                temperature=0.0,
                timeout_seconds=self.settings.triage_timeout_seconds,
            )
        except (ModelError, HermclawError) as exc:
            async with self.sm() as s:
                await append_event(
                    s,
                    EventType.STATUS,
                    source_type="driver",
                    job_id=job.id,
                    severity=Severity.warning,
                    payload={"text": f"Triage nicht verfügbar ({exc.code}) – Planung ohne Triage"},
                )
                await s.commit()
            return None
        triage = res.value.model_dump()
        await self._set_meta(job.id, triage=triage)
        return triage

    async def _planner_input(self, job: Job, ws: WorkspaceInfo | None, triage: dict[str, Any] | None) -> PlannerInput:
        inventory: dict[str, Any] = {}
        hits: list[ContextSnippet] = []
        if ws is not None and self.repo_intel is not None and self.git is not None:
            handle = await self.git.handle(ws)
            inventory = await self.repo_intel.inventory(handle, job_id=job.id)
            found = await self.repo_intel.context_for(handle, job.prompt, budget_chars=self.settings.context_budget_chars, job_id=job.id)
            hits = [ContextSnippet.from_hit(h) for h in found]
        cmd, framework, test_files = _test_hints(inventory)
        constraints: list[str] = []  # user constraints come from job_inputs inside the planner
        if triage and triage.get("needs_research"):
            constraints.append("Triage: external documentation research is likely required before implementation.")
        return PlannerInput(
            repository_inventory=inventory,
            retrieved_context=hits,
            constraints=constraints,
            existing_tests=test_files,
            known_paths=_paths_from_inventory(inventory, self.settings.max_known_paths),
            test_command=cmd,
            test_framework=framework,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------ JobDriver
    async def prepare(self, job_id: uuid.UUID, token: CancelToken) -> JobPhaseResult:
        job = await self._job(job_id)
        if job.current_plan_version is not None:
            return JobPhaseResult(ok=True, next_status=JobStatus.running.value)  # recovered after planning finished
        try:
            ws = None
            if job.repository_id is not None:
                await self._advance(job_id, JobStatus.inventory, "Repository wird erfasst (Workspace)")
                ws = await self._ensure_workspace(job)
            if token.cancelled:
                return JobPhaseResult(ok=False, error_code="CANCELLED")
            await self._advance(job_id, JobStatus.discovering, "Bestandsanalyse und Triage")
            triage = await self._triage(job)
            inputs = await self._planner_input(job, ws, triage)
            if token.cancelled:
                return JobPhaseResult(ok=False, error_code="CANCELLED")
            await self._advance(job_id, JobStatus.planning, "Gemma plant den Job")
            try:
                result = await self.planner.create_plan(job_id, inputs)
            except PlanConflict as exc:
                if exc.code != "PLAN_EXISTS":
                    raise
                return JobPhaseResult(ok=True, next_status=JobStatus.running.value, detail={"plan": "existing"})
        except PlannerError as exc:
            return JobPhaseResult(ok=False, error_code=exc.code, error_message=exc.message, detail=exc.details)
        except (GitError, ModelError, HermclawError) as exc:
            return JobPhaseResult(ok=False, error_code=exc.code, error_message=DEFAULT_REDACTOR.text(exc.message))
        async with self.sm() as s:
            await emit_status(s, job_id, f"Plan v{result.version} mit {len(result.step_ids)} Steps erstellt")
            await s.commit()
        return JobPhaseResult(ok=True, next_status=JobStatus.running.value, detail={"plan_version": result.version})

    async def finalize(self, job_id: uuid.UUID, token: CancelToken) -> JobPhaseResult:
        job = await self._job(job_id)
        notes: list[str] = []
        extra: dict[str, Any] = {"triage": (job.metadata_ or {}).get("triage"), "notes": notes}
        ws = await self._workspace(job)
        result = JobPhaseResult(ok=True)
        if ws is not None and self.git is not None:
            try:
                base = await self.git.check_base(ws)
                if base.stale:
                    upd = await self.git.update_to_base(ws, strategy=self.settings.update_strategy)
                    notes.append(f"Base aktualisiert ({upd.strategy}) {upd.old_base_sha[:10]} → {upd.new_base_sha[:10]}")
                    if self.regression is not None and upd.updated:
                        ok, info = await self.regression.rerun(job_id, await self.git.handle(ws))
                        if not ok:
                            result = JobPhaseResult(
                                ok=False,
                                error_code="REGRESSION_FAILED",
                                error_message="regression rerun after base update failed",
                                detail=info,
                            )
                if result.ok and not token.cancelled:
                    if await self.git.changed_files(ws) or (await self._has_commits(ws)):
                        push = await self.git.push_job_branch(
                            ws,
                            create_merge_request=self.settings.create_merge_request,
                            title=job.title,
                            description=f"Hermclaw job {job.id}",
                        )
                        if push.merge_request is not None:
                            extra["merge_request_url"] = push.merge_request.web_url
                        if push.merge_request_error:
                            extra["merge_request_error"] = push.merge_request_error
                    else:
                        notes.append("Keine Repository-Änderungen – nichts zu pushen.")
            except MergeConflictError as exc:
                result = JobPhaseResult(ok=False, error_code=exc.code, error_message=exc.message, detail=exc.details)
            except GitError as exc:
                result = JobPhaseResult(ok=False, error_code=exc.code, error_message=DEFAULT_REDACTOR.text(exc.message), detail=exc.details)
        extra["final_status"] = "succeeded" if result.ok else f"failed ({result.error_code})"
        async with self.sm() as s:
            data = await collect_report_data(s, job_id)
            text = render_report(data, extra=extra)
            art = await write_report_artifact(s, job_id, text, self.settings.artifacts_dir or get_settings().artifacts_dir)
            job_row = (await s.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one()
            done = sum(1 for st in data["steps"] if st.status == "completed" and not st.superseded)
            job_row.result_summary = f"{done} Steps abgeschlossen; Bericht: {art.name}" + (
                f"; MR: {extra['merge_request_url']}" if extra.get("merge_request_url") else ""
            )
            await emit_status(s, job_id, "Abschlussbericht erstellt")
            await s.commit()
        return result

    async def _has_commits(self, ws: WorkspaceInfo) -> bool:
        assert self.git is not None
        fresh = await self.git.get_workspace(ws.id)
        return bool(fresh.head_sha and fresh.head_sha != fresh.base_sha)

    async def replan(self, job_id: uuid.UUID, reason: str, evidence: dict[str, Any], token: CancelToken) -> JobPhaseResult:
        code = replan_reason_code(reason, evidence)
        failed_keys = [f.get("step_key") for f in evidence.get("failed_steps") or [] if f.get("status") == "failed"] or [
            f.get("step_key") for f in evidence.get("failed_steps") or []
        ]
        failed_step_id = None
        if failed_keys:
            async with self.sm() as s:
                failed_step_id = (
                    await s.execute(
                        select(Step.id).where(Step.job_id == job_id, Step.step_key == failed_keys[0], Step.superseded.is_(False))
                    )
                ).scalar_one_or_none()
        trigger = ReplanTrigger(
            reason_code=code,  # type: ignore[arg-type]
            failed_step_id=failed_step_id,
            evidence=DEFAULT_REDACTOR.obj(evidence),
            detail=DEFAULT_REDACTOR.text(reason)[:500],
        )
        job = await self._job(job_id)
        try:
            inputs = await self._planner_input(job, await self._workspace(job), (job.metadata_ or {}).get("triage"))
            res = await self.replanner.replan(job_id, trigger, inputs)
        except ReplanLimitReached as exc:
            return JobPhaseResult(ok=False, error_code="REPLAN_LIMIT", error_message=exc.message, detail=exc.details)
        except PlannerError as exc:
            return JobPhaseResult(ok=False, error_code=exc.code, error_message=exc.message, detail=exc.details)
        except (ModelError, HermclawError) as exc:
            return JobPhaseResult(ok=False, error_code=exc.code, error_message=DEFAULT_REDACTOR.text(exc.message))
        async with self.sm() as s:
            await emit_status(s, job_id, f"Neuer Plan v{res.version} ({code}): {len(res.created_step_keys)} neue Steps")
            await s.commit()
        return JobPhaseResult(ok=True, next_status=JobStatus.running.value, detail={"plan_version": res.version, "reason_code": code})
