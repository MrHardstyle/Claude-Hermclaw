"""ORM models for every persistent entity (Bauplan §10). PostgreSQL is the single source of truth."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermclaw.persistence.base import Base, CreatedMixin, TimestampMixin

EMBEDDING_DIM = 768


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _fk(target: str, *, nullable: bool = True, ondelete: str = "CASCADE") -> Mapped[Any]:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True)


def _json(default: Any = None) -> Mapped[Any]:
    if default is None:
        default = dict
    return mapped_column(
        JSONB, nullable=False, default=default, server_default=text("'{}'::jsonb") if default is dict else text("'[]'::jsonb")
    )


# ============================================================================================= repositories
class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(String(200), nullable=False, default="main")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="gitlab")
    gitlab_project_id: Mapped[str | None] = mapped_column(String(200))
    protected_branches: Mapped[list[str]] = _json(list)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


# ============================================================================================= jobs
class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    repository_id: Mapped[uuid.UUID | None] = _fk("repositories.id", ondelete="SET NULL")
    base_branch: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(200), nullable=False, default="api")
    current_plan_version: Mapped[int | None] = mapped_column(Integer)
    replan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_owner: Mapped[str | None] = mapped_column(String(200))
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))

    steps: Mapped[list[Step]] = relationship(back_populates="job", cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (
        CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range"),
        Index("ix_jobs_status_priority", "status", "priority"),
    )


class JobInput(CreatedMixin, Base):
    __tablename__ = "job_inputs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = _fk("jobs.id", nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # prompt|constraint|file|url|option
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


# ============================================================================================= plans
class Plan(TimestampMixin, Base):
    __tablename__ = "plans"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = _fk("jobs.id", nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")  # active|superseded|failed
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PlanVersion(CreatedMixin, Base):
    __tablename__ = "plan_versions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = _fk("plans.id", nullable=False)
    job_id: Mapped[uuid.UUID] = _fk("jobs.id", nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # planner|replanner|fallback|manual
    model_alias: Mapped[str | None] = mapped_column(String(100))
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    validation_errors: Mapped[list[Any]] = _json(list)
    repair_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("plan_id", "version", name="plan_version"),)


# ============================================================================================= steps
class Step(TimestampMixin, Base):
    __tablename__ = "steps"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_version_id: Mapped[uuid.UUID | None] = _fk("plan_versions.id", ondelete="SET NULL")
    step_key: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    constraints: Mapped[list[Any]] = _json(list)
    acceptance: Mapped[list[Any]] = _json(list)
    repo_hints: Mapped[list[Any]] = _json(list)
    preferred_worker_capabilities: Mapped[list[Any]] = _json(list)
    allowed_new_paths: Mapped[list[Any]] = _json(list)
    forbidden_paths: Mapped[list[Any]] = _json(list)
    network: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    turn_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assigned_worker_id: Mapped[str | None] = mapped_column(String(100))
    current_scope_version: Mapped[int | None] = mapped_column(Integer)
    checkpoint: Mapped[dict[str, Any]] = _json(dict)
    result: Mapped[dict[str, Any]] = _json(dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    job: Mapped[Job] = relationship(back_populates="steps")
    __table_args__ = (
        Index("ix_steps_job_key_active", "job_id", "step_key", unique=True, postgresql_where=text("superseded = false")),
        Index("ix_steps_status_priority", "status", "priority"),
    )


class StepDependency(Base):
    __tablename__ = "step_dependencies"
    step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("steps.id", ondelete="CASCADE"), primary_key=True)
    depends_on_step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("steps.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    __table_args__ = (CheckConstraint("step_id <> depends_on_step_id", name="no_self_dependency"),)


class StepAttempt(Base):
    __tablename__ = "step_attempts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    step_id: Mapped[uuid.UUID] = _fk("steps.id", nullable=False)
    job_id: Mapped[uuid.UUID] = _fk("jobs.id", nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="initial")  # initial|correction|retry|resume
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    worker_id: Mapped[str | None] = mapped_column(String(100))
    model_alias: Mapped[str | None] = mapped_column(String(100))
    turns_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    history: Mapped[list[Any]] = _json(list)  # compact per-turn records (tool, args digest, result digest)
    fingerprints: Mapped[dict[str, Any]] = _json(dict)
    correction_input: Mapped[dict[str, Any]] = _json(dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("step_id", "attempt_no", name="step_attempt_no"),)


# ============================================================================================= events
class Event(Base):
    __tablename__ = "events"
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    payload: Mapped[dict[str, Any]] = _json(dict)
    correlation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (Index("ix_events_job_sequence", "job_id", "sequence"),)


# ============================================================================================= workers
class Worker(TimestampMixin, Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="offline", index=True)
    api_url: Mapped[str | None] = mapped_column(Text)
    worker_version: Mapped[str | None] = mapped_column(String(64))
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_job_id: Mapped[str | None] = mapped_column(String(64))
    active_step_id: Mapped[str | None] = mapped_column(String(64))
    wol: Mapped[dict[str, Any]] = _json(dict)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class WorkerCapability(Base):
    __tablename__ = "worker_capabilities"
    worker_id: Mapped[str] = mapped_column(String(100), ForeignKey("workers.id", ondelete="CASCADE"), primary_key=True)
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = _json(dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class WorkerHealth(CreatedMixin, Base):
    __tablename__ = "worker_health"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(100), ForeignKey("workers.id", ondelete="CASCADE"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    cpu_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    ram_total_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ram_used_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disk_free_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gpus: Mapped[list[Any]] = _json(list)
    loaded_models: Mapped[list[Any]] = _json(list)
    active_job: Mapped[str | None] = mapped_column(String(64))
    active_step: Mapped[str | None] = mapped_column(String(64))
    uptime_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    service_versions: Mapped[dict[str, Any]] = _json(dict)


# ============================================================================================= models
class ModelProfile(TimestampMixin, Base):
    __tablename__ = "model_profiles"
    alias: Mapped[str] = mapped_column(String(100), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="chat")
    host_worker_id: Mapped[str | None] = mapped_column(String(100))
    context_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_group: Mapped[str] = mapped_column(String(100), nullable=False)
    exclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_gb: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    fallback_for: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class ModelInvocation(Base):
    __tablename__ = "model_invocations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    alias: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_chars: Mapped[int | None] = mapped_column(Integer)  # length only – reasoning content is never stored
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(String(32))
    response_valid: Mapped[bool | None] = mapped_column(Boolean)
    repair_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    response_excerpt: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ============================================================================================= resources
class ResourceLease(Base):
    __tablename__ = "resource_leases"
    id: Mapped[uuid.UUID] = _uuid_pk()
    resource: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    resource_group: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    owner_step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    owner_kind: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. planner|coder|heavy|video|image|exec
    holder: Mapped[str] = mapped_column(String(200), nullable=False)  # process/instance id
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")  # active|preempting|released|expired
    preemptible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    exclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    __table_args__ = (
        Index(
            "uq_resource_leases_exclusive_active",
            "resource",
            unique=True,
            postgresql_where=text("exclusive = true AND state IN ('active','preempting')"),
        ),
        Index("ix_resource_leases_state_expires", "state", "expires_at"),
    )


class ResourceRequest(CreatedMixin, Base):
    """Waiting acquisitions – lets the manager honour priorities and fairness across processes."""

    __tablename__ = "resource_requests"
    id: Mapped[uuid.UUID] = _uuid_pk()
    resource: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    owner_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    owner_step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    owner_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    holder: Mapped[str] = mapped_column(String(200), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting")  # waiting|granted|cancelled
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ============================================================================================= workspaces / artifacts
class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = _fk("jobs.id", nullable=False)
    repository_id: Mapped[uuid.UUID | None] = _fk("repositories.id", ondelete="SET NULL")
    path: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(String(300), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(200), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")  # active|committed|pushed|archived|cleaned


class Artifact(CreatedMixin, Base):
    __tablename__ = "artifacts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID | None] = _fk("jobs.id")
    step_id: Mapped[uuid.UUID | None] = _fk("steps.id", ondelete="SET NULL")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


# ============================================================================================= scope
class ScopeContractRow(CreatedMixin, Base):
    __tablename__ = "scope_contracts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = _fk("jobs.id", nullable=False)
    step_id: Mapped[uuid.UUID] = _fk("steps.id", nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")  # active|superseded|unavailable
    contract: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict[str, Any]] = _json(dict)
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("step_id", "version", name="scope_step_version"),)


# ============================================================================================= tool calls / commands / tests
class ToolCallRow(Base):
    __tablename__ = "tool_calls"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    turn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, Any]] = _json(dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class CommandRun(CreatedMixin, Base):
    __tablename__ = "command_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    worker_id: Mapped[str | None] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(32), nullable=False, default="sandbox")  # sandbox|ssh|local
    command: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    cwd: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    network: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stdout_excerpt: Mapped[str | None] = mapped_column(Text)
    stderr_excerpt: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class TestRun(CreatedMixin, Base):
    __tablename__ = "test_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    command: Mapped[str] = mapped_column(Text, nullable=False)
    framework: Mapped[str] = mapped_column(String(32), nullable=False, default="generic")
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # passed|failed|error
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_excerpt: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    __test__ = False  # not a pytest class


# ============================================================================================= verification / review
class VerificationRun(CreatedMixin, Base):
    __tablename__ = "verification_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = _fk("jobs.id", nullable=False)
    step_id: Mapped[uuid.UUID] = _fk("steps.id", nullable=False)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running|passed|failed|error
    summary: Mapped[str | None] = mapped_column(Text)
    changed_files: Mapped[list[Any]] = _json(list)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationCheckRow(Base):
    __tablename__ = "verification_checks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    verification_run_id: Mapped[uuid.UUID] = _fk("verification_runs.id", nullable=False)
    check_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    message: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = _json(dict)


class ReviewRun(CreatedMixin, Base):
    __tablename__ = "review_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = _fk("jobs.id", nullable=False)
    step_id: Mapped[uuid.UUID] = _fk("steps.id", nullable=False)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model_alias: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running|completed|error
    verdict: Mapped[str | None] = mapped_column(String(32))
    raw_verdict: Mapped[str | None] = mapped_column(String(32))
    invariant_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    summary: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewFindingRow(Base):
    __tablename__ = "review_findings"
    id: Mapped[uuid.UUID] = _uuid_pk()
    review_run_id: Mapped[uuid.UUID] = _fk("review_runs.id", nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)
    suggested_fix: Mapped[str | None] = mapped_column(Text)


# ============================================================================================= research
class ResearchRun(CreatedMixin, Base):
    __tablename__ = "research_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID | None] = _fk("jobs.id")
    step_id: Mapped[uuid.UUID | None] = _fk("steps.id", ondelete="SET NULL")
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    queries: Mapped[list[Any]] = _json(list)
    synthesis: Mapped[str | None] = mapped_column(Text)
    contradictions: Mapped[list[Any]] = _json(list)
    model_alias: Mapped[str | None] = mapped_column(String(100))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchSource(Base):
    __tablename__ = "research_sources"
    id: Mapped[uuid.UUID] = _uuid_pk()
    research_run_id: Mapped[uuid.UUID] = _fk("research_runs.id", nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(String(300), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    authority_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    excerpt: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="read")  # read|failed|skipped
    error: Mapped[str | None] = mapped_column(Text)


class ResearchClaim(CreatedMixin, Base):
    __tablename__ = "research_claims"
    id: Mapped[uuid.UUID] = _uuid_pk()
    research_run_id: Mapped[uuid.UUID] = _fk("research_runs.id", nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    used_for_decision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decision_ref: Mapped[str | None] = mapped_column(Text)
    contradiction_group: Mapped[int | None] = mapped_column(Integer)


class ResearchClaimSource(Base):
    __tablename__ = "research_claim_sources"
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("research_claims.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_sources.id", ondelete="CASCADE"), primary_key=True
    )


# ============================================================================================= git / deployments / wake / bugs
class GitOperation(CreatedMixin, Base):
    __tablename__ = "git_operations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok|failed|refused
    ref: Mapped[str | None] = mapped_column(String(300))
    sha_before: Mapped[str | None] = mapped_column(String(64))
    sha_after: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = _json(dict)
    error: Mapped[str | None] = mapped_column(Text)


class Deployment(CreatedMixin, Base):
    __tablename__ = "deployments"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="started")
    manifest: Mapped[dict[str, Any]] = _json(dict)
    rollback_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WakeEvent(CreatedMixin, Base):
    __tablename__ = "wake_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = _json(dict)


class BugRecord(TimestampMixin, Base):
    __tablename__ = "bug_records"
    id: Mapped[uuid.UUID] = _uuid_pk()
    bug_key: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    severity: Mapped[str] = mapped_column(String(4), nullable=False)
    component: Mapped[str] = mapped_column(String(100), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reproduction: Mapped[str | None] = mapped_column(Text)
    expected: Mapped[str | None] = mapped_column(Text)
    actual: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = _json(dict)
    workaround: Mapped[str | None] = mapped_column(Text)
    regression_test: Mapped[str | None] = mapped_column(Text)
    fix_commit: Mapped[str | None] = mapped_column(String(64))


class MemoryEntry(CreatedMixin, Base):
    """Reserved for later memory features – disabled by default (Bauplan §10)."""

    __tablename__ = "memory_entries"
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# ============================================================================================= repository intelligence
class RepoIndexRun(CreatedMixin, Base):
    __tablename__ = "repo_index_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID | None] = _fk("repositories.id")
    workspace_id: Mapped[uuid.UUID | None] = _fk("workspaces.id", ondelete="SET NULL")
    git_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_index_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    inventory: Mapped[dict[str, Any]] = _json(dict)
    stats: Mapped[dict[str, Any]] = _json(dict)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodeSymbol(Base):
    __tablename__ = "code_symbols"
    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    git_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # function|class|method|route|table|...
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    parent: Mapped[str | None] = mapped_column(String(300))
    references: Mapped[list[Any]] = _json(list)
    __table_args__ = (Index("ix_code_symbols_repo_path", "repository_key", "path"),)


class CodeChunk(CreatedMixin, Base):
    __tablename__ = "code_chunks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    git_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(300))
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        Index("ix_code_chunks_repo_path", "repository_key", "path"),
        Index(
            "ix_code_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# ============================================================================================= auth
class ApiToken(CreatedMixin, Base):
    __tablename__ = "api_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[Any]] = _json(list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


ALL_TABLES = sorted(Base.metadata.tables)
