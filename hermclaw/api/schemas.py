"""API response models (stable JSON for the UI)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StepOut(_Out):
    id: uuid.UUID
    step_key: str
    title: str
    kind: str
    capability: str
    goal: str
    status: str
    risk: str
    attempt_count: int
    correction_count: int
    superseded: bool
    assigned_worker_id: str | None = None
    current_scope_version: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    acceptance: list[Any] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class JobOut(_Out):
    id: uuid.UUID
    title: str
    prompt: str
    status: str
    priority: int
    repository_id: uuid.UUID | None = None
    base_branch: str | None = None
    current_plan_version: int | None = None
    replan_count: int = 0
    cancel_requested: bool = False
    pause_requested: bool = False
    result_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobDetailOut(JobOut):
    steps: list[StepOut] = Field(default_factory=list)
    step_counts: dict[str, int] = Field(default_factory=dict)


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int


class EventOut(_Out):
    sequence: int
    event_id: uuid.UUID
    ts: datetime
    job_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    source_type: str
    source_id: str | None = None
    event_type: str
    severity: str
    payload: dict[str, Any]
    correlation_id: str | None = None
    duration_ms: int | None = None


class ControlOut(BaseModel):
    job_id: uuid.UUID
    action: str
    status: str
    accepted: bool = True
    detail: str = ""


class ArtifactOut(_Out):
    id: uuid.UUID
    job_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    kind: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class PlanVersionOut(_Out):
    id: uuid.UUID
    version: int
    source: str
    model_alias: str | None = None
    plan_json: dict[str, Any]
    validation_errors: list[Any]
    repair_attempts: int
    reason: str | None = None
    created_at: datetime
