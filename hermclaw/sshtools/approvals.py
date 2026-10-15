"""Operator approvals for mutating SSH commands (P26 26.3/26.5).

Approvals live on the job row: ``jobs.metadata['ssh_approvals']`` is a list of approved tokens (plain strings
or ``{"token": …, "approved_by": …, "approved_at": …}``). An approval is *single use*: executing the approved
command moves it to ``jobs.metadata['ssh_approvals_consumed']`` (history) under a row lock, so two concurrent
calls cannot both run one approval, and a retry of the same mutation needs a fresh approval (the operator may
approve the same token again – tokens identify host + command + step, approvals authorise one execution).

Only the runtime/API (operator action) writes ``ssh_approvals``; the model only ever sees the token in a
refusal, and a client cannot plant either key at job creation (reserved in ``hermclaw.core.job_metadata``,
BUG-140). Writes merge just these two keys at SQL level (``metadata || patch``) while holding ``FOR UPDATE``,
so concurrent writers of other metadata keys are not overwritten.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.core import job_metadata
from hermclaw.core.errors import NotFoundError, ValidationFailed
from hermclaw.persistence.models import Job
from hermclaw.sshtools.errors import SSH_APPROVAL_INVALID
from hermclaw.sshtools.policy import APPROVAL_TOKEN_RE

APPROVALS_KEY = job_metadata.SSH_APPROVALS  # reserved: POST /api/jobs refuses it in client metadata (BUG-140)
CONSUMED_KEY = job_metadata.SSH_APPROVALS_CONSUMED
_MAX_ENTRIES = 500


def _token_of(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("token"), str):
        return str(entry["token"])
    return None


def _check_token(token: str) -> None:
    if not APPROVAL_TOKEN_RE.match(token):
        raise ValidationFailed("malformed SSH approval token", code=SSH_APPROVAL_INVALID)


async def _locked_metadata(session: AsyncSession, job_id: uuid.UUID) -> dict[str, Any]:
    row = await session.execute(select(Job.metadata_).where(Job.id == job_id).with_for_update())
    found = row.first()
    if found is None:
        raise NotFoundError(f"job {job_id} not found")
    return dict(found[0] or {})


async def _patch(session: AsyncSession, job_id: uuid.UUID, patch: dict[str, Any]) -> None:
    await session.execute(
        text("UPDATE jobs SET metadata = coalesce(metadata, '{}'::jsonb) || CAST(:patch AS jsonb), updated_at = now() WHERE id = :id"),
        {"patch": json.dumps(patch), "id": job_id},
    )


def _list(meta: dict[str, Any], key: str) -> list[Any]:
    value = meta.get(key)
    return list(value) if isinstance(value, list) else []


async def grant_approval(session: AsyncSession, job_id: uuid.UUID, token: str, *, approved_by: str, note: str | None = None) -> bool:
    """Record an operator approval (caller commits). Returns False if the token was already approved."""
    _check_token(token)
    meta = await _locked_metadata(session, job_id)
    approvals = _list(meta, APPROVALS_KEY)
    if any(_token_of(e) == token for e in approvals):
        return False
    entry: dict[str, Any] = {"token": token, "approved_by": approved_by[:200], "approved_at": datetime.now(UTC).isoformat()}
    if note:
        entry["note"] = note[:500]
    approvals = [*approvals, entry][-_MAX_ENTRIES:]
    await _patch(session, job_id, {APPROVALS_KEY: approvals})
    return True


async def revoke_approval(session: AsyncSession, job_id: uuid.UUID, token: str) -> bool:
    """Remove a not yet used approval (caller commits)."""
    _check_token(token)
    meta = await _locked_metadata(session, job_id)
    approvals = _list(meta, APPROVALS_KEY)
    kept = [e for e in approvals if _token_of(e) != token]
    if len(kept) == len(approvals):
        return False
    await _patch(session, job_id, {APPROVALS_KEY: kept})
    return True


async def approval_state(session: AsyncSession, job_id: uuid.UUID, token: str) -> str:
    """``approved`` (usable) | ``consumed`` (used, not re-approved) | ``missing`` (no lock; informational)."""
    meta = dict((await session.scalar(select(Job.metadata_).where(Job.id == job_id))) or {})
    if any(_token_of(e) == token for e in _list(meta, APPROVALS_KEY)):
        return "approved"
    if any(_token_of(e) == token for e in _list(meta, CONSUMED_KEY)):
        return "consumed"
    return "missing"


async def consume_approval(
    session: AsyncSession,
    job_id: uuid.UUID,
    token: str,
    *,
    host_id: str,
    step_id: uuid.UUID | None,
) -> bool:
    """Atomically use an approval (caller commits). False if it is missing or already used."""
    _check_token(token)
    meta = await _locked_metadata(session, job_id)
    approvals = _list(meta, APPROVALS_KEY)
    consumed = _list(meta, CONSUMED_KEY)
    match = next((e for e in approvals if _token_of(e) == token), None)
    if match is None:
        return False
    used: dict[str, Any] = {
        "token": token,
        "host": host_id,
        "step_id": str(step_id) if step_id else None,
        "consumed_at": datetime.now(UTC).isoformat(),
    }
    if isinstance(match, dict) and match.get("approved_by"):
        used["approved_by"] = match["approved_by"]
    await _patch(
        session,
        job_id,
        {APPROVALS_KEY: [e for e in approvals if _token_of(e) != token], CONSUMED_KEY: [*consumed, used][-_MAX_ENTRIES:]},
    )
    return True


__all__ = ["APPROVALS_KEY", "CONSUMED_KEY", "approval_state", "consume_approval", "grant_approval", "revoke_approval"]
