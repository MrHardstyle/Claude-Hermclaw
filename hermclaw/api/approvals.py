"""Operator approvals of mutating SSH commands (P26 26.5, ``hermclaw.sshtools.approvals``).

A mutating ``ssh_command``/``ssh_upload`` of a job step is refused with outcome ``approval_required`` and an approval
token (event ``ssh.command``) while ``policies.ssh.require_approval_for_mutations`` is on. The operator reviews the
host + command and approves the token here; the next attempt of the step (retry, or the coder trying again) runs the
command exactly once. Tokens identify host + command + step, so an approval never covers a different command.

``GET`` needs the read scope; approving and revoking need ``admin`` (it authorises a change on a real host).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.api.auth import Principal, require
from hermclaw.api.jobs import get_session
from hermclaw.contracts.events import EventType
from hermclaw.core.errors import ConflictError, NotFoundError
from hermclaw.events.store import append_event
from hermclaw.persistence.models import Event, Job
from hermclaw.sshtools.approvals import APPROVALS_KEY, CONSUMED_KEY, grant_approval, revoke_approval

router = APIRouter(prefix="/api", tags=["approvals"])


class SshApprovalIn(BaseModel):
    token: str = Field(min_length=8, max_length=200)
    note: str | None = Field(default=None, max_length=500)


_ENTRY_KEYS = ("token", "approved_by", "approved_at", "note", "consumed_at")


def _tokens(entries: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entries if isinstance(entries, list) else []:
        out.append({"token": e} if isinstance(e, str) else {k: v for k, v in dict(e).items() if k in _ENTRY_KEYS})
    return out


@router.get("/jobs/{job_id}/ssh-approvals")
async def list_ssh_approvals(
    job_id: uuid.UUID, _: Principal = Depends(require("read")), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    meta = (await session.execute(select(Job.metadata_).where(Job.id == job_id))).first()
    if meta is None:
        raise NotFoundError("job not found")
    approved, consumed = _tokens((meta[0] or {}).get(APPROVALS_KEY)), _tokens((meta[0] or {}).get(CONSUMED_KEY))
    known = {a["token"] for a in approved}
    rows = (
        await session.execute(
            select(Event.payload, Event.step_id, Event.ts)
            .where(Event.job_id == job_id, Event.event_type == EventType.SSH_COMMAND)
            .order_by(Event.sequence.desc())
            .limit(500)
        )
    ).all()
    pending: dict[str, dict[str, Any]] = {}
    for payload, step_id, ts in rows:
        token = (payload or {}).get("approval")
        if (payload or {}).get("outcome") != "approval_required" or not token or token in known or token in pending:
            continue
        pending[token] = {
            "token": token,
            "host": payload.get("host"),
            "tool": payload.get("tool"),
            "command": payload.get("command"),
            "classification": payload.get("classification"),
            "step_id": str(step_id) if step_id else None,
            "requested_at": ts.isoformat() if ts else None,
        }
    return {"pending": list(pending.values()), "approved": approved, "consumed": consumed}


@router.post("/jobs/{job_id}/ssh-approvals", status_code=201)
async def approve_ssh_command(
    job_id: uuid.UUID,
    body: SshApprovalIn,
    principal: Principal = Depends(require("admin")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not await grant_approval(session, job_id, body.token, approved_by=principal.name, note=body.note):
        raise ConflictError("this approval token is already approved and unused")
    await append_event(
        session,
        EventType.SSH_APPROVAL_GRANTED,
        source_type="api",
        source_id=principal.name,
        job_id=job_id,
        payload={"approval": body.token, "approved_by": principal.name},
    )
    await session.commit()
    return {"job_id": str(job_id), "approval": body.token, "state": "approved"}


@router.delete("/jobs/{job_id}/ssh-approvals/{token}")
async def revoke_ssh_approval(
    job_id: uuid.UUID, token: str, principal: Principal = Depends(require("admin")), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if not await revoke_approval(session, job_id, token):
        raise NotFoundError("no unused approval with this token")
    await append_event(
        session,
        EventType.SSH_APPROVAL_REVOKED,
        source_type="api",
        source_id=principal.name,
        job_id=job_id,
        payload={"approval": token, "revoked_by": principal.name},
    )
    await session.commit()
    return {"job_id": str(job_id), "approval": token, "state": "revoked"}


__all__ = ["router"]
