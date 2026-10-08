"""Audit trail for every git write: one ``git_operations`` row plus one event (Bauplan §13, §27)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.errors import HermclawError, PolicyViolation, ProtectedBranchError, StaleBaseError
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.gitops.errors import CommitNotVerifiedError, NothingToCommitError, NothingToPushError, PushRejectedError
from hermclaw.gitops.urls import redact_url
from hermclaw.persistence.models import GitOperation

SOURCE_TYPE = "gitops"
SOURCE_ID = "git-engine"
REFUSAL_ERRORS: tuple[type[HermclawError], ...] = (
    PolicyViolation,
    ProtectedBranchError,
    CommitNotVerifiedError,
    NothingToCommitError,
    NothingToPushError,
    PushRejectedError,
    StaleBaseError,
)


@dataclass
class OpRecord:
    """Mutable description of one git write while it is being executed."""

    operation: str
    job_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    ref: str | None = None
    sha_before: str | None = None
    sha_after: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    event_type: str = EventType.GIT_OPERATION
    recorded: bool = False
    row_id: uuid.UUID | None = None


def classify_error(exc: BaseException) -> str:
    return "refused" if isinstance(exc, REFUSAL_ERRORS) else "failed"


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact_url(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def write_audit(
    session: AsyncSession,
    rec: OpRecord,
    *,
    status: str,
    error: BaseException | None = None,
) -> GitOperation:
    """Insert the ``git_operations`` row and the matching event in the caller's transaction."""
    details: dict[str, Any] = DEFAULT_REDACTOR.obj(_clean(dict(rec.details)))
    error_text: str | None = None
    error_code: str | None = None
    if error is not None:
        error_code = getattr(error, "code", type(error).__name__)
        error_text = DEFAULT_REDACTOR.text(redact_url(str(error)))[:4000]
        if isinstance(error, HermclawError) and error.details:
            details.setdefault("error_details", DEFAULT_REDACTOR.obj(_clean(error.details)))
        details["error_code"] = error_code
    row = GitOperation(
        id=uuid.uuid4(),
        job_id=rec.job_id,
        step_id=rec.step_id,
        workspace_id=rec.workspace_id,
        operation=rec.operation,
        status=status,
        ref=rec.ref,
        sha_before=rec.sha_before,
        sha_after=rec.sha_after,
        details=details,
        error=error_text,
    )
    session.add(row)
    await session.flush()
    severity = {"ok": Severity.info, "refused": Severity.warning}.get(status, Severity.error)
    event_type = rec.event_type if status == "ok" else EventType.GIT_OPERATION
    payload: dict[str, Any] = {
        "operation": rec.operation,
        "status": status,
        "git_operation_id": str(row.id),
        "workspace_id": str(rec.workspace_id) if rec.workspace_id else None,
        "ref": rec.ref,
        "sha_before": rec.sha_before,
        "sha_after": rec.sha_after,
        "details": details,
    }
    if error is not None:
        payload["error_code"] = error_code
        payload["error"] = error_text
    await append_event(
        session,
        event_type,
        source_type=SOURCE_TYPE,
        source_id=SOURCE_ID,
        job_id=rec.job_id,
        step_id=rec.step_id,
        severity=severity,
        payload=payload,
    )
    rec.recorded = True
    rec.row_id = row.id
    return row
