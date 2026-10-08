"""JobContract – user request and job views (Bauplan §1, §34)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from hermclaw.contracts.common import Contract, JobStatus


class RepositoryRef(Contract):
    name: str | None = Field(default=None, description="registered repository name")
    url: str | None = Field(default=None, description="git remote URL (registered on the fly if allowed)")
    base_branch: str | None = None


class JobCreate(Contract):
    """Free user prompt plus optional repository and constraints."""

    prompt: str = Field(min_length=3, max_length=50_000)
    title: str | None = Field(default=None, max_length=200)
    repository: RepositoryRef | None = None
    constraints: list[str] = Field(default_factory=list, max_length=50)
    priority: int = Field(default=50, ge=0, le=100)
    allow_network_research: bool = True
    auto_commit: bool = True
    create_merge_request: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobContract(Contract):
    id: UUID
    title: str
    prompt: str
    status: JobStatus
    priority: int
    repository_id: UUID | None = None
    base_branch: str | None = None
    current_plan_version: int | None = None
    result_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
