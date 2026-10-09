"""Event contract and canonical event types (Bauplan §13). No chain-of-thought is ever part of an event."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from hermclaw.contracts.common import Contract, Severity


class EventType:
    JOB_CREATED = "job.created"
    JOB_TRANSITION = "job.transition"
    JOB_SUCCEEDED = "job.succeeded"
    JOB_FAILED = "job.failed"
    JOB_CANCELLED = "job.cancelled"
    STEP_CREATED = "step.created"
    STEP_TRANSITION = "step.transition"
    ATTEMPT_STARTED = "attempt.started"
    ATTEMPT_FINISHED = "attempt.finished"
    REPO_INVENTORY_STARTED = "repo.inventory.started"
    REPO_INVENTORY_FINISHED = "repo.inventory.finished"
    REPO_SEARCH_EXECUTED = "repo.search.executed"
    REPO_INDEX_UPDATED = "repo.index.updated"
    RESEARCH_STARTED = "research.started"
    RESEARCH_QUERY_STARTED = "research.query.started"
    RESEARCH_SOURCE_READ = "research.source.read"
    RESEARCH_CLAIM_CREATED = "research.claim.created"
    RESEARCH_FINISHED = "research.finished"
    PLANNER_INVOKED = "planner.invoked"
    PLANNER_REPAIR = "planner.repair"
    PLANNER_FALLBACK_USED = "planner.fallback.used"
    PLANNER_PLAN_CREATED = "planner.plan.created"
    PLANNER_FAILED = "planner.failed"
    REPLAN_STARTED = "replan.started"
    REPLAN_CREATED = "replan.created"
    SCOPE_CREATED = "scope.created"
    SCOPE_EXPANSION_REQUESTED = "scope.expansion.requested"
    SCOPE_EXPANDED = "scope.expanded"
    SCOPE_UNAVAILABLE = "scope.unavailable"
    SCOPE_VIOLATION = "scope.violation"
    RESOURCE_REQUESTED = "resource.requested"
    RESOURCE_ACQUIRED = "resource.acquired"
    RESOURCE_RELEASED = "resource.released"
    RESOURCE_PREEMPT_REQUESTED = "resource.preempt.requested"
    RESOURCE_EXPIRED = "resource.expired"
    WORKER_REGISTERED = "worker.registered"
    WORKER_STATE = "worker.state"
    WORKER_OFFLINE = "worker.offline"
    WORKER_WAKE_SENT = "worker.wake.sent"
    WORKER_WAKE_STAGE = "worker.wake.stage"
    WORKER_READY = "worker.ready"
    WORKER_WAKE_FAILED = "worker.wake.failed"
    WORKER_ASSIGNED = "worker.assigned"
    MODEL_LOAD_STARTED = "model.load.started"
    MODEL_LOAD_FINISHED = "model.load.finished"
    MODEL_UNLOADED = "model.unloaded"
    MODEL_INVOCATION_STARTED = "model.invocation.started"
    MODEL_INVOCATION_FINISHED = "model.invocation.finished"
    CONTEXT_BUILT = "context.built"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_FINISHED = "tool.call.finished"
    COMMAND_RUN = "command.run"
    FILE_CHANGED = "file.changed"
    TEST_STARTED = "test.started"
    TEST_PASSED = "test.passed"
    TEST_FAILED = "test.failed"
    VERIFIER_STARTED = "verifier.started"
    VERIFIER_CHECK_FAILED = "verifier.check.failed"
    VERIFIER_FINISHED = "verifier.finished"
    REVIEW_STARTED = "review.started"
    REVIEW_FINDING_CREATED = "review.finding.created"
    REVIEW_FINISHED = "review.finished"
    CORRECTION_STARTED = "correction.started"
    STAGNATION_DETECTED = "stagnation.detected"
    STRATEGY_CHANGED = "strategy.changed"
    CHECKPOINT_CREATED = "checkpoint.created"
    GIT_OPERATION = "git.operation"
    GIT_COMMIT_CREATED = "git.commit.created"
    GIT_PUSHED = "git.pushed"
    MERGE_REQUEST_CREATED = "git.merge_request.created"
    DEPLOYMENT_STARTED = "deployment.started"
    DEPLOYMENT_FINISHED = "deployment.finished"
    MEDIA_STARTED = "media.started"
    MEDIA_FINISHED = "media.finished"
    SSH_COMMAND = "ssh.command"
    DB_TOOL = "db.tool"
    ERROR = "error"
    STATUS = "status"  # human-readable live status line for the UI


class EventEnvelope(Contract):
    event_id: UUID
    sequence: int
    timestamp: datetime
    job_id: UUID | None = None
    step_id: UUID | None = None
    attempt_id: UUID | None = None
    source_type: str
    source_id: str | None = None
    event_type: str
    severity: Severity = Severity.info
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    duration_ms: int | None = None
