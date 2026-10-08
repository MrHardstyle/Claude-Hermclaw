"""Job API (Bauplan §34, P30 30.1/30.2/30.6/30.8)."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.api.auth import Principal, require
from hermclaw.api.schemas import ArtifactOut, ControlOut, JobDetailOut, JobListOut, JobOut, PlanVersionOut, StepOut
from hermclaw.contracts.common import JobStatus
from hermclaw.contracts.events import EventType
from hermclaw.contracts.job import JobCreate
from hermclaw.core.errors import ConflictError, NotFoundError, ValidationFailed
from hermclaw.core.settings import get_settings
from hermclaw.events.store import append_event
from hermclaw.persistence.db import get_sessionmaker
from hermclaw.persistence.models import (
    Artifact,
    CommandRun,
    Job,
    JobInput,
    PlanVersion,
    Repository,
    ResearchClaim,
    ResearchClaimSource,
    ResearchRun,
    ResearchSource,
    ReviewFindingRow,
    ReviewRun,
    ScopeContractRow,
    Step,
    StepAttempt,
    StepDependency,
    TestRun,
    ToolCallRow,
    VerificationCheckRow,
    VerificationRun,
)
from hermclaw.runtime.state_machines import JOB_TERMINAL, can_transition_job
from hermclaw.runtime.transitions import emit_status, transition_job

router = APIRouter(prefix="/api", tags=["jobs"])


async def get_session() -> Any:
    async with get_sessionmaker()() as session:
        yield session


def _job_out(job: Job) -> JobOut:
    data = {c.key: getattr(job, c.key) for c in job.__mapper__.column_attrs if c.key != "metadata_"}
    data["metadata"] = dict(job.metadata_ or {})
    return JobOut.model_validate(data)


async def _load_job(session: AsyncSession, job_id: uuid.UUID, *, lock: bool = False) -> Job:
    stmt = select(Job).where(Job.id == job_id)
    if lock:
        stmt = stmt.with_for_update()
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise NotFoundError(f"job {job_id} not found")
    return job


def _slug_name(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", tail)[:150] or f"repo-{uuid.uuid4().hex[:6]}"


async def _resolve_repository(session: AsyncSession, body: JobCreate) -> Repository | None:
    if body.repository is None:
        return None
    ref = body.repository
    if ref.name:
        repo = (await session.execute(select(Repository).where(Repository.name == ref.name))).scalar_one_or_none()
        if repo is None and not ref.url:
            raise ValidationFailed(f"repository '{ref.name}' is not registered; provide url to register it")
        if repo is not None:
            return repo
    if ref.url:
        repo = (await session.execute(select(Repository).where(Repository.url == ref.url))).scalar_one_or_none()
        if repo is not None:
            return repo
        if not re.match(r"^(ssh://|git@|https?://|file://)", ref.url):
            raise ValidationFailed("repository url must be ssh://, git@, http(s):// or file://")
        repo = Repository(name=ref.name or _slug_name(ref.url), url=ref.url, default_branch=ref.base_branch or "main")
        session.add(repo)
        await session.flush()
        await append_event(session, "repository.registered", source_type="api", payload={"repository": repo.name, "url": ref.url})
        return repo
    return None


@router.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(
    body: JobCreate, principal: Principal = Depends(require("control")), session: AsyncSession = Depends(get_session)
) -> JobOut:
    repo = await _resolve_repository(session, body)
    title = body.title or (body.prompt.strip().splitlines()[0][:120] if body.prompt.strip() else "Job")
    job = Job(
        title=title,
        prompt=body.prompt,
        status=JobStatus.queued.value,
        priority=body.priority,
        repository_id=repo.id if repo else None,
        base_branch=(
            body.repository.base_branch if body.repository and body.repository.base_branch else (repo.default_branch if repo else None)
        ),
        created_by=principal.name,
        metadata_={
            **body.metadata,
            "allow_network_research": body.allow_network_research,
            "auto_commit": body.auto_commit,
            "create_merge_request": body.create_merge_request,
        },
    )
    session.add(job)
    await session.flush()
    session.add(JobInput(job_id=job.id, kind="prompt", content=body.prompt))
    for c in body.constraints:
        session.add(JobInput(job_id=job.id, kind="constraint", content=c))
    await append_event(
        session,
        EventType.JOB_CREATED,
        source_type="api",
        source_id=principal.name,
        job_id=job.id,
        payload={"title": title, "repository": repo.name if repo else None, "priority": body.priority},
    )
    await emit_status(session, job.id, "Job angelegt und in die Warteschlange gestellt")
    await session.commit()
    return _job_out(job)


@router.get("/jobs", response_model=JobListOut)
async def list_jobs(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: Principal = Depends(require("read")),
    session: AsyncSession = Depends(get_session),
) -> JobListOut:
    stmt = select(Job)
    count = select(func.count()).select_from(Job)
    if status:
        stmt = stmt.where(Job.status == status)
        count = count.where(Job.status == status)
    rows = (await session.execute(stmt.order_by(Job.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    total = (await session.execute(count)).scalar_one()
    return JobListOut(items=[_job_out(j) for j in rows], total=total)


async def _steps_out(session: AsyncSession, job_id: uuid.UUID) -> list[StepOut]:
    steps = (await session.execute(select(Step).where(Step.job_id == job_id).order_by(Step.created_at, Step.step_key))).scalars().all()
    ids = [s.id for s in steps]
    key_by_id = {s.id: s.step_key for s in steps}
    deps: dict[uuid.UUID, list[str]] = {i: [] for i in ids}
    if ids:
        for d in (await session.execute(select(StepDependency).where(StepDependency.step_id.in_(ids)))).scalars():
            deps[d.step_id].append(key_by_id.get(d.depends_on_step_id, str(d.depends_on_step_id)))
    out = []
    for s in steps:
        so = StepOut.model_validate(s)
        so.depends_on = sorted(deps.get(s.id, []))
        out.append(so)
    return out


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
async def get_job(job_id: uuid.UUID, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)) -> JobDetailOut:
    job = await _load_job(session, job_id)
    steps = await _steps_out(session, job_id)
    counts: dict[str, int] = {}
    for s in steps:
        if not s.superseded:
            counts[s.status] = counts.get(s.status, 0) + 1
    detail = JobDetailOut(**_job_out(job).model_dump(), steps=steps, step_counts=counts)
    return detail


async def _control(session: AsyncSession, job_id: uuid.UUID, action: str, principal: Principal, *, reason: str = "") -> ControlOut:
    job = await _load_job(session, job_id, lock=True)
    status = JobStatus(job.status)
    detail = ""
    if action == "cancel":
        if status in JOB_TERMINAL:
            raise ConflictError(f"job already {status.value}")
        job.cancel_requested = True
        if status in (
            JobStatus.queued,
            JobStatus.blocked,
            JobStatus.waiting_for_user,
            JobStatus.waiting_for_resources,
            JobStatus.waiting_for_worker,
        ):
            await transition_job(session, job, JobStatus.cancelled, reason=reason or "cancelled via API", actor=principal.name)
        detail = "cancellation requested"
    elif action == "pause":
        if status in JOB_TERMINAL:
            raise ConflictError(f"job already {status.value}")
        job.pause_requested = True
        detail = "running steps finish at their next checkpoint; no new steps are dispatched"
    elif action == "resume":
        job.pause_requested = False
        if status == JobStatus.waiting_for_user:
            await transition_job(session, job, JobStatus.running, reason="resumed via API", actor=principal.name)
        detail = "resumed"
    elif action == "retry":
        if not can_transition_job(status, JobStatus.queued):
            raise ConflictError(f"retry not possible from status {status.value}")
        job.cancel_requested = False
        job.pause_requested = False
        job.metadata_ = {**(job.metadata_ or {}), "retry_requested": True}
        await transition_job(session, job, JobStatus.queued, reason="retry via API", actor=principal.name)
        detail = "job re-queued; failed steps will be retried"
    elif action == "replan":
        if status in JOB_TERMINAL:
            raise ConflictError(f"job already {status.value}")
        job.metadata_ = {**(job.metadata_ or {}), "replan_requested": reason or "manual replan via API"}
        detail = "replan requested"
    else:  # pragma: no cover
        raise ValidationFailed(f"unknown action {action}")
    await append_event(
        session, f"job.control.{action}", source_type="api", source_id=principal.name, job_id=job.id, payload={"reason": reason}
    )
    await session.commit()
    return ControlOut(job_id=job.id, action=action, status=job.status, detail=detail)


def _control_route(action: str):  # type: ignore[no-untyped-def]
    async def handler(
        job_id: uuid.UUID,
        reason: str = "",
        principal: Principal = Depends(require("control")),
        session: AsyncSession = Depends(get_session),
    ) -> ControlOut:
        return await _control(session, job_id, action, principal, reason=reason)

    handler.__name__ = f"{action}_job"
    return handler


for _action in ("cancel", "pause", "resume", "retry", "replan"):
    router.add_api_route(f"/jobs/{{job_id}}/{_action}", _control_route(_action), methods=["POST"], response_model=ControlOut)


@router.get("/jobs/{job_id}/plan", response_model=list[PlanVersionOut])
async def get_plan_versions(
    job_id: uuid.UUID, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)
) -> list[PlanVersionOut]:
    await _load_job(session, job_id)
    rows = (await session.execute(select(PlanVersion).where(PlanVersion.job_id == job_id).order_by(PlanVersion.version))).scalars().all()
    return [PlanVersionOut.model_validate(r) for r in rows]


@router.get("/jobs/{job_id}/steps", response_model=list[StepOut])
async def get_steps(
    job_id: uuid.UUID, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)
) -> list[StepOut]:
    await _load_job(session, job_id)
    return await _steps_out(session, job_id)


def _rows(rows: Any, *exclude: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = {c.key: getattr(r, c.key) for c in r.__mapper__.column_attrs if c.key not in exclude}
        if "metadata_" in d:
            d["metadata"] = d.pop("metadata_")
        out.append(d)
    return out


@router.get("/steps/{step_id}")
async def get_step_detail(
    step_id: uuid.UUID, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    step = (await session.execute(select(Step).where(Step.id == step_id))).scalar_one_or_none()
    if step is None:
        raise NotFoundError(f"step {step_id} not found")
    attempts = (
        (await session.execute(select(StepAttempt).where(StepAttempt.step_id == step_id).order_by(StepAttempt.attempt_no))).scalars().all()
    )
    scopes = (
        (await session.execute(select(ScopeContractRow).where(ScopeContractRow.step_id == step_id).order_by(ScopeContractRow.version)))
        .scalars()
        .all()
    )
    tools = (
        (await session.execute(select(ToolCallRow).where(ToolCallRow.step_id == step_id).order_by(ToolCallRow.started_at))).scalars().all()
    )
    cmds = (await session.execute(select(CommandRun).where(CommandRun.step_id == step_id).order_by(CommandRun.created_at))).scalars().all()
    tests = (await session.execute(select(TestRun).where(TestRun.step_id == step_id).order_by(TestRun.created_at))).scalars().all()
    vruns = (
        (await session.execute(select(VerificationRun).where(VerificationRun.step_id == step_id).order_by(VerificationRun.created_at)))
        .scalars()
        .all()
    )
    vchecks: Sequence[VerificationCheckRow] = []
    if vruns:
        vchecks = (
            (await session.execute(select(VerificationCheckRow).where(VerificationCheckRow.verification_run_id.in_([v.id for v in vruns]))))
            .scalars()
            .all()
        )
    rruns = (await session.execute(select(ReviewRun).where(ReviewRun.step_id == step_id).order_by(ReviewRun.created_at))).scalars().all()
    findings: Sequence[ReviewFindingRow] = []
    if rruns:
        findings = (
            (await session.execute(select(ReviewFindingRow).where(ReviewFindingRow.review_run_id.in_([r.id for r in rruns]))))
            .scalars()
            .all()
        )
    return {
        "step": StepOut.model_validate(step).model_dump(mode="json"),
        "attempts": _rows(attempts),
        "scope_versions": _rows(scopes),
        "tool_calls": _rows(tools),
        "commands": _rows(cmds),
        "tests": _rows(tests),
        "verifications": [{**v, "checks": [c for c in _rows(vchecks) if c["verification_run_id"] == v["id"]]} for v in _rows(vruns)],
        "reviews": [{**r, "findings": [f for f in _rows(findings) if f["review_run_id"] == r["id"]]} for r in _rows(rruns)],
    }


@router.get("/jobs/{job_id}/artifacts", response_model=list[ArtifactOut])
async def list_artifacts(
    job_id: uuid.UUID, kind: str | None = None, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)
) -> list[ArtifactOut]:
    await _load_job(session, job_id)
    stmt = select(Artifact).where(Artifact.job_id == job_id)
    if kind:
        stmt = stmt.where(Artifact.kind == kind)
    rows = (await session.execute(stmt.order_by(Artifact.created_at))).scalars().all()
    return [ArtifactOut.model_validate(r) for r in rows]


def _resolve_artifact(p: Path) -> tuple[Path, Path, bool]:
    root = get_settings().artifacts_dir.resolve()
    path = p.resolve()
    return root, path, path.is_file()


def _read_text_limited(p: Path, limit: int) -> str:
    return p.read_text(encoding="utf-8", errors="replace")[:limit] if p.is_file() else ""


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: uuid.UUID, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)
) -> FileResponse:
    art = (await session.execute(select(Artifact).where(Artifact.id == artifact_id))).scalar_one_or_none()
    if art is None:
        raise NotFoundError("artifact not found")
    root, path, exists = await asyncio.to_thread(_resolve_artifact, Path(art.path))
    if root not in path.parents:
        raise NotFoundError("artifact path outside artifact store")
    if not exists:
        raise NotFoundError("artifact file missing")
    return FileResponse(path, media_type=art.media_type, filename=art.name)


@router.get("/jobs/{job_id}/diff")
async def get_diff(
    job_id: uuid.UUID, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    await _load_job(session, job_id)
    art = (
        await session.execute(
            select(Artifact).where(Artifact.job_id == job_id, Artifact.kind == "diff").order_by(Artifact.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if art is None:
        return {"diff": "", "artifact_id": None}
    text = await asyncio.to_thread(_read_text_limited, Path(art.path), 2_000_000)
    return {
        "diff": text,
        "artifact_id": str(art.id),
        "step_id": str(art.step_id) if art.step_id else None,
        "created_at": art.created_at.isoformat(),
    }


@router.get("/jobs/{job_id}/research")
async def get_research(
    job_id: uuid.UUID, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    await _load_job(session, job_id)
    runs = (await session.execute(select(ResearchRun).where(ResearchRun.job_id == job_id).order_by(ResearchRun.created_at))).scalars().all()
    out = []
    for r in runs:
        sources = (
            (
                await session.execute(
                    select(ResearchSource).where(ResearchSource.research_run_id == r.id).order_by(ResearchSource.authority_score.desc())
                )
            )
            .scalars()
            .all()
        )
        claims = (await session.execute(select(ResearchClaim).where(ResearchClaim.research_run_id == r.id))).scalars().all()
        links: Sequence[ResearchClaimSource] = []
        if claims:
            links = (
                (await session.execute(select(ResearchClaimSource).where(ResearchClaimSource.claim_id.in_([c.id for c in claims]))))
                .scalars()
                .all()
            )
        by_claim: dict[uuid.UUID, list[str]] = {}
        for link in links:
            by_claim.setdefault(link.claim_id, []).append(str(link.source_id))
        out.append(
            {
                **_rows([r])[0],
                "sources": _rows(sources),
                "claims": [{**c, "source_ids": by_claim.get(c["id"], [])} for c in _rows(claims)],
            }
        )
    return out
