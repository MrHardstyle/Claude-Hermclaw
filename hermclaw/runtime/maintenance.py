"""Runtime maintenance of the orchestrator host: workspace retention (Bauplan §27 ``cleanup``).

:class:`WorkspaceJanitor` removes the job workspaces of *finished* jobs (``succeeded``/``failed``/``cancelled``)
once the job ended longer ago than ``policies.git.workspace_retention_hours`` (``0`` keeps them forever):

1. candidates: workspaces that are not ``cleaned`` yet, lie below the engine's workspace root and belong to a
   terminal job whose ``finished_at`` (``updated_at`` for rows without one) is older than the retention;
2. per workspace, under a ``FOR NO KEY UPDATE SKIP LOCKED`` lock of the job row, the job is re-checked (an API
   retry moves a failed job back to ``queued`` – such a workspace is never touched; a job row locked by someone
   else is skipped this round). The lock is compatible with the foreign-key checks of the audit/event inserts the
   cleanup performs, and it serialises concurrent janitors and the API control endpoints on the same job;
3. a workspace with verified commits that were never pushed (``committed``) is *kept* – verified work is never
   discarded silently;
4. the workspace-scoped code index (``code_symbols``/``code_chunks``) is dropped through
   ``repo.drop_workspace_index(handle)`` when the repository context provider offers it – *before* the directory,
   so a crash between the two steps never orphans index rows of a cleaned workspace;
5. :meth:`GitEngine.cleanup` removes the directory and marks the row ``cleaned`` (audited as ``workspace.cleanup``);
6. ``workspace.cleaned`` is emitted with job/workspace ids (in the job's event stream).

Errors are logged and never stop the loop; a workspace that failed (or was kept) is retried after
``retry_after_seconds``. Active jobs are never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.config import GitPolicy
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.gitops.engine import GitEngine
from hermclaw.persistence.models import Job, Workspace
from hermclaw.runtime.state_machines import JOB_TERMINAL

log = get_logger(__name__)

TERMINAL_JOB_STATES: tuple[str, ...] = tuple(sorted(s.value for s in JOB_TERMINAL))
SOURCE_TYPE = "runtime"
KEEP_UNPUSHED = "unpushed_commits"
KEEP_JOB_ACTIVE = "job_active"
KEEP_JOB_LOCKED = "job_locked"
KEEP_TOO_RECENT = "too_recent"
KEEP_ALREADY_CLEANED = "already_cleaned"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _error_text(exc: BaseException) -> str:
    return DEFAULT_REDACTOR.text(f"{type(exc).__name__}: {exc}")[:500]


@dataclass(frozen=True)
class CleanedWorkspace:
    workspace_id: uuid.UUID
    job_id: uuid.UUID
    status_before: str
    index: dict[str, int] | None = None
    index_error: str | None = None


@dataclass
class JanitorReport:
    """Outcome of one :meth:`WorkspaceJanitor.run_once` pass."""

    cleaned: list[CleanedWorkspace] = field(default_factory=list)
    kept: dict[uuid.UUID, str] = field(default_factory=dict)  # workspace id -> reason (KEEP_*)
    failed: dict[uuid.UUID, str] = field(default_factory=dict)  # workspace id -> error text

    @property
    def cleaned_ids(self) -> set[uuid.UUID]:
        return {c.workspace_id for c in self.cleaned}


class WorkspaceJanitor:
    """Retention cleanup of finished jobs' workspaces; :meth:`start`/:meth:`stop` run it periodically."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        git: GitEngine,
        repo: object | None = None,
        *,
        policy: GitPolicy | None = None,
        interval_seconds: float = 900.0,
        batch_size: int = 50,
        retry_after_seconds: float = 3600.0,
        source_id: str = "orchestrator",
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.sm = sessionmaker
        self.git = git
        self.repo = repo
        self.policy = policy or git.git_policy
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.retry_after_seconds = retry_after_seconds
        self.source_id = source_id
        self._now = now
        self._retry_at: dict[uuid.UUID, float] = {}  # workspace id -> monotonic time of the next attempt
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def retention_hours(self) -> int:
        return int(self.policy.workspace_retention_hours)

    @property
    def enabled(self) -> bool:
        return self.retention_hours > 0

    def _backing_off(self) -> list[uuid.UUID]:
        mono = time.monotonic()
        for ws_id in [w for w, at in self._retry_at.items() if at <= mono]:
            del self._retry_at[ws_id]
        return list(self._retry_at)

    def _defer(self, workspace_id: uuid.UUID) -> None:
        self._retry_at[workspace_id] = time.monotonic() + self.retry_after_seconds

    async def _candidates(self, cutoff: datetime) -> list[tuple[uuid.UUID, uuid.UUID]]:
        finished = func.coalesce(Job.finished_at, Job.updated_at)
        root = str(self.git.workspaces_root).rstrip(os.sep) + os.sep
        stmt = (
            select(Workspace.id, Workspace.job_id)
            .join(Job, Job.id == Workspace.job_id)
            .where(
                Job.status.in_(TERMINAL_JOB_STATES),
                finished < cutoff,
                Workspace.status != "cleaned",
                Workspace.path.startswith(root, autoescape=True),
            )
            .order_by(finished, Workspace.created_at)
            .limit(self.batch_size)
        )
        skip = self._backing_off()
        if skip:
            stmt = stmt.where(Workspace.id.not_in(skip))
        async with self.sm() as s:
            return [(row[0], row[1]) for row in (await s.execute(stmt)).all()]

    async def run_once(self) -> JanitorReport:
        """One retention pass (at most ``batch_size`` workspaces)."""
        report = JanitorReport()
        if not self.enabled:
            return report
        cutoff = _aware(self._now()) - timedelta(hours=self.retention_hours)
        for ws_id, job_id in await self._candidates(cutoff):
            try:
                await self._clean_one(ws_id, job_id, cutoff, report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one broken workspace must not stop the others
                error = _error_text(exc)
                report.failed[ws_id] = error
                self._defer(ws_id)
                log.warning("workspace retention cleanup failed", extra={"workspace_id": str(ws_id), "job_id": str(job_id), "error": error})
        if report.cleaned or report.failed:
            log.info(
                "workspace retention pass",
                extra={"cleaned": len(report.cleaned), "kept": len(report.kept), "failed": len(report.failed)},
            )
        return report

    async def _clean_one(self, ws_id: uuid.UUID, job_id: uuid.UUID, cutoff: datetime, report: JanitorReport) -> None:
        async with self.sm() as s, s.begin():
            # NO KEY UPDATE: excludes API control (FOR UPDATE), job transitions and other janitors, but not the
            # FOR KEY SHARE locks of the foreign keys the cleanup's audit/event rows take on this job
            stmt = select(Job).where(Job.id == job_id).with_for_update(key_share=True, skip_locked=True)
            job = (await s.execute(stmt)).scalar_one_or_none()
            if job is None:
                report.kept[ws_id] = KEEP_JOB_LOCKED
                return
            finished = _aware(job.finished_at or job.updated_at)
            if job.status not in TERMINAL_JOB_STATES:
                report.kept[ws_id] = KEEP_JOB_ACTIVE
                return
            if finished >= cutoff:
                report.kept[ws_id] = KEEP_TOO_RECENT
                return
            before = await self.git.get_workspace(ws_id)
            if before.status == "cleaned":
                report.kept[ws_id] = KEEP_ALREADY_CLEANED
                return
            if before.status == "committed" and await asyncio.to_thread(Path(before.path).is_dir):
                # verified commits that never reached the remote: keeping them is the only safe default (a
                # committed workspace whose directory is gone already has nothing left to keep and is cleaned)
                report.kept[ws_id] = KEEP_UNPUSHED
                self._defer(ws_id)
                log.info(
                    "workspace kept: verified commits were never pushed",
                    extra={"workspace_id": str(ws_id), "job_id": str(job_id), "head_sha": before.head_sha},
                )
                return
            # the index is derived data and goes first: a process dying between the two steps must not leave index
            # rows of a ``cleaned`` workspace behind (cleaned workspaces are never candidates again); the other way
            # round only costs a lazy re-index if the workspace were used once more before the next pass
            index, index_error = await self._drop_index(before.to_handle())
            cleaned = await self.git.cleanup(ws_id)
            payload: dict[str, Any] = {
                "text": f"Workspace des Jobs entfernt (Aufbewahrung von {self.retention_hours} h abgelaufen)",
                "workspace_id": str(ws_id),
                "job_id": str(job_id),
                "repository": cleaned.repository_name,
                "branch": cleaned.branch,
                "path": cleaned.path,
                "status_before": before.status,
                "job_status": job.status,
                "job_finished_at": finished.isoformat(),
                "retention_hours": self.retention_hours,
                "index": index,
            }
            if index_error:
                payload["index_error"] = index_error
            await append_event(
                s, EventType.WORKSPACE_CLEANED, source_type=SOURCE_TYPE, source_id=self.source_id, job_id=job_id, payload=payload
            )
        self._retry_at.pop(ws_id, None)
        report.cleaned.append(CleanedWorkspace(ws_id, job_id, before.status, index=index, index_error=index_error))

    async def _drop_index(self, handle: WorkspaceHandle) -> tuple[dict[str, int] | None, str | None]:
        drop: Callable[[WorkspaceHandle], Awaitable[Any]] | None = getattr(self.repo, "drop_workspace_index", None)
        if not callable(drop):
            return None, None
        try:
            counts = await drop(handle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the directory is gone already; a stale index is reported, not fatal
            error = _error_text(exc)
            log.warning("workspace index drop failed", extra={"workspace_id": str(handle.id), "error": error})
            return None, error
        return ({str(k): int(v) for k, v in dict(counts).items()} if counts else {}), None

    # ------------------------------------------------------------------------------------------ loop
    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the janitor must never take the scheduler down
                log.warning("workspace janitor failed", extra={"error": _error_text(exc)})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)

    def start(self) -> None:
        if not self.enabled:
            log.info("workspace retention disabled (policies.git.workspace_retention_hours = 0)")
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="workspace-janitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()


__all__ = [
    "KEEP_ALREADY_CLEANED",
    "KEEP_JOB_ACTIVE",
    "KEEP_JOB_LOCKED",
    "KEEP_TOO_RECENT",
    "KEEP_UNPUSHED",
    "TERMINAL_JOB_STATES",
    "CleanedWorkspace",
    "JanitorReport",
    "WorkspaceJanitor",
]
