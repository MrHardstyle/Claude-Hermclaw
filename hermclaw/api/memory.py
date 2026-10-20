"""Knowledge API of the "Wissen" page (D-019): list, read, search, write and retire entries.

Entries written here come from the operator and are active at once; nothing is deleted – a wrong entry is retired
and stays visible with its history. The embedding vector never leaves the server.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from hermclaw.api.auth import Principal, require
from hermclaw.memory.store import KIND_LABELS, MemoryStore
from hermclaw.persistence.models import MemoryEntry

router = APIRouter(prefix="/api", tags=["memory"])

Kind = Literal["lesson", "research", "incident", "tool_bug", "convention", "note"]
Status = Literal["candidate", "active", "retired"]


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind = "lesson"
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20000)
    status: Status = "active"
    scope: str | None = Field(default=None, max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=30)


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    content: str | None = Field(default=None, min_length=1, max_length=20000)
    status: Status | None = None
    scope: str | None = Field(default=None, max_length=300)
    tags: list[str] | None = Field(default=None, max_length=30)
    expires_at: datetime | None = None


def _store(request: Request) -> MemoryStore:
    return request.app.state.hermclaw.memory  # type: ignore[no-any-return]


def _out(row: MemoryEntry) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "kind_label": KIND_LABELS.get(row.kind, row.kind),
        "title": row.title,
        "content": row.content,
        "status": row.status,
        "source": row.source,
        "scope": row.scope,
        "tags": list(row.tags or []),
        "signature": row.signature,
        "occurrences": row.occurrences,
        "recalls": row.recalls,
        "job_id": row.job_id,
        "evidence": dict(row.evidence or {}),
        "embedded": row.embedding is not None,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/memory")
async def list_memory(
    request: Request,
    kind: Kind | None = None,
    status: Status | None = None,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(200, ge=1, le=1000),
    _: Principal = Depends(require("read")),
) -> list[dict[str, Any]]:
    return [_out(r) for r in await _store(request).entries(kind=kind, status=status, q=q, limit=limit)]


@router.get("/memory/search")
async def search_memory(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500),
    k: int = Query(5, ge=1, le=20),
    _: Principal = Depends(require("read")),
) -> list[dict[str, Any]]:
    """What a model's recall would get for ``q`` (active entries only; not counted as a recall)."""
    hits = await _store(request).search(q, k=k, count=False)
    return [{"id": h.id, "kind": h.kind, "title": h.title, "score": h.score, "scope": h.scope} for h in hits]


@router.get("/memory/{entry_id}")
async def get_memory(request: Request, entry_id: uuid.UUID, _: Principal = Depends(require("read"))) -> dict[str, Any]:
    row = await _store(request).get(entry_id)
    if row is None:
        raise HTTPException(404, "memory entry not found")
    return _out(row)


@router.post("/memory", status_code=201)
async def create_memory(request: Request, body: MemoryIn, _: Principal = Depends(require("control"))) -> dict[str, Any]:
    store = _store(request)
    entry_id = await store.add(
        kind=body.kind,
        title=body.title,
        content=body.content,
        status=body.status,
        source="operator",
        scope=body.scope,
        tags=body.tags,
    )
    row = await store.get(entry_id)
    assert row is not None
    return _out(row)


@router.patch("/memory/{entry_id}")
async def update_memory(
    request: Request, entry_id: uuid.UUID, body: MemoryPatch, _: Principal = Depends(require("control"))
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "nothing to change")
    if {"title", "content"} & set(changes):
        changes["source"] = "operator"  # the text is the operator's now: learning after jobs does not rewrite it
    row = await _store(request).update(entry_id, **changes)
    if row is None:
        raise HTTPException(404, "memory entry not found")
    return _out(row)
