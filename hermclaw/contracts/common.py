"""Shared enums and base classes for all contracts (P13)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """Strict base: unknown fields are rejected so LLM output cannot smuggle data."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class JobStatus(StrEnum):
    queued = "queued"
    inventory = "inventory"
    discovering = "discovering"
    researching = "researching"
    planning = "planning"
    waiting_for_resources = "waiting_for_resources"
    waiting_for_worker = "waiting_for_worker"
    waking_worker = "waking_worker"
    running = "running"
    testing = "testing"
    verifying = "verifying"
    reviewing = "reviewing"
    correcting = "correcting"
    replanning = "replanning"
    waiting_for_user = "waiting_for_user"
    blocked = "blocked"
    committing = "committing"
    deploying = "deploying"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class StepStatus(StrEnum):
    pending = "pending"
    ready = "ready"
    leased = "leased"
    running = "running"
    checkpointed = "checkpointed"
    testing = "testing"
    verifying = "verifying"
    reviewing = "reviewing"
    completed = "completed"
    failed = "failed"
    blocked = "blocked"
    cancelled = "cancelled"


class StepKind(StrEnum):
    inventory = "inventory"
    discover = "discover"
    research = "research"
    plan = "plan"
    implement = "implement"
    test = "test"
    verify = "verify"
    review = "review"
    replan = "replan"
    ssh = "ssh"
    database = "database"
    docker = "docker"
    deploy = "deploy"
    documentation = "documentation"
    image = "image"
    video = "video"


MUTATING_STEP_KINDS = frozenset(
    {StepKind.implement, StepKind.database, StepKind.docker, StepKind.deploy, StepKind.documentation, StepKind.ssh}
)


class Risk(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class Severity(StrEnum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"


class FindingSeverity(StrEnum):
    minor = "minor"
    major = "major"
    blocker = "blocker"


class WorkerState(StrEnum):
    offline = "offline"
    starting = "starting"
    ready = "ready"
    busy = "busy"
    draining = "draining"
    sleeping = "sleeping"
    waking = "waking"
    error = "error"


class WorkerKind(StrEnum):
    execution = "execution"
    model = "model"
    media = "media"
