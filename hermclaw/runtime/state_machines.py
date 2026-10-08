"""Deterministic job/step state machines (Bauplan §11, §12, P05).

Every transition is either explicitly allowed or rejected with ``InvalidTransition``. The tables are the
single authority used by the scheduler, API controls and recovery.
"""

from __future__ import annotations

from collections.abc import Mapping

from hermclaw.contracts.common import JobStatus as J
from hermclaw.contracts.common import StepStatus as S
from hermclaw.core.errors import InvalidTransition

_ACTIVE_JOB = {J.running, J.testing, J.verifying, J.reviewing, J.correcting}

JOB_TRANSITIONS: Mapping[J, frozenset[J]] = {
    J.queued: frozenset(
        {
            J.inventory,
            J.discovering,
            J.researching,
            J.planning,
            J.waiting_for_resources,
            J.waiting_for_worker,
            J.waking_worker,
            J.blocked,
            J.failed,
            J.cancelled,
        }
    ),
    J.inventory: frozenset({J.discovering, J.researching, J.planning, J.blocked, J.failed, J.cancelled}),
    J.discovering: frozenset({J.researching, J.planning, J.blocked, J.failed, J.cancelled}),
    J.researching: frozenset({J.planning, J.replanning, J.running, J.blocked, J.failed, J.cancelled}),
    J.planning: frozenset(
        {
            J.running,
            J.researching,
            J.waiting_for_resources,
            J.waiting_for_worker,
            J.waking_worker,
            J.waiting_for_user,
            J.blocked,
            J.failed,
            J.cancelled,
        }
    ),
    J.waiting_for_resources: frozenset(
        {J.running, J.planning, J.replanning, J.waiting_for_worker, J.waking_worker, J.blocked, J.failed, J.cancelled}
    ),
    J.waiting_for_worker: frozenset(
        {J.waking_worker, J.running, J.planning, J.replanning, J.waiting_for_resources, J.blocked, J.failed, J.cancelled}
    ),
    J.waking_worker: frozenset(
        {J.waiting_for_worker, J.waiting_for_resources, J.running, J.planning, J.replanning, J.blocked, J.failed, J.cancelled}
    ),
    J.running: frozenset(
        {
            J.testing,
            J.verifying,
            J.reviewing,
            J.correcting,
            J.replanning,
            J.researching,
            J.waiting_for_resources,
            J.waiting_for_worker,
            J.waking_worker,
            J.waiting_for_user,
            J.committing,
            J.deploying,
            J.blocked,
            J.failed,
            J.cancelled,
        }
    ),
    J.testing: frozenset({J.running, J.verifying, J.correcting, J.blocked, J.failed, J.cancelled}),
    J.verifying: frozenset({J.running, J.reviewing, J.correcting, J.committing, J.blocked, J.failed, J.cancelled}),
    J.reviewing: frozenset({J.running, J.correcting, J.committing, J.blocked, J.failed, J.cancelled}),
    J.correcting: frozenset({J.running, J.replanning, J.blocked, J.failed, J.cancelled}),
    J.replanning: frozenset({J.running, J.researching, J.waiting_for_user, J.blocked, J.failed, J.cancelled}),
    J.waiting_for_user: frozenset({J.running, J.planning, J.replanning, J.failed, J.cancelled}),
    J.blocked: frozenset({J.replanning, J.running, J.waiting_for_user, J.queued, J.failed, J.cancelled}),
    J.committing: frozenset({J.deploying, J.running, J.succeeded, J.blocked, J.failed, J.cancelled}),
    J.deploying: frozenset({J.running, J.succeeded, J.blocked, J.failed, J.cancelled}),
    J.succeeded: frozenset(),
    J.failed: frozenset({J.queued}),  # explicit retry via API only
    J.cancelled: frozenset(),
}

STEP_TRANSITIONS: Mapping[S, frozenset[S]] = {
    S.pending: frozenset({S.ready, S.blocked, S.failed, S.cancelled}),
    S.ready: frozenset({S.leased, S.pending, S.blocked, S.failed, S.cancelled}),
    S.leased: frozenset({S.running, S.ready, S.failed, S.cancelled}),
    S.running: frozenset({S.checkpointed, S.testing, S.verifying, S.reviewing, S.completed, S.ready, S.failed, S.blocked, S.cancelled}),
    S.checkpointed: frozenset({S.ready, S.running, S.failed, S.blocked, S.cancelled}),
    S.testing: frozenset({S.running, S.verifying, S.checkpointed, S.failed, S.blocked, S.cancelled}),
    S.verifying: frozenset({S.reviewing, S.completed, S.running, S.ready, S.checkpointed, S.failed, S.blocked, S.cancelled}),
    S.reviewing: frozenset({S.completed, S.running, S.ready, S.checkpointed, S.failed, S.blocked, S.cancelled}),
    S.completed: frozenset(),
    S.failed: frozenset({S.ready}),  # bounded retry decided by the scheduler
    S.blocked: frozenset({S.ready, S.failed, S.cancelled}),
    S.cancelled: frozenset(),
}

JOB_TERMINAL = frozenset({J.succeeded, J.failed, J.cancelled})
STEP_TERMINAL = frozenset({S.completed, S.cancelled})
STEP_DONE_OK = frozenset({S.completed})
STEP_IN_FLIGHT = frozenset({S.leased, S.running, S.testing, S.verifying, S.reviewing})


def can_transition_job(src: J | str, dst: J | str) -> bool:
    return J(dst) in JOB_TRANSITIONS[J(src)]


def can_transition_step(src: S | str, dst: S | str) -> bool:
    return S(dst) in STEP_TRANSITIONS[S(src)]


def check_job(src: J | str, dst: J | str) -> J:
    if not can_transition_job(src, dst):
        raise InvalidTransition(f"job transition {src} -> {dst} not allowed", details={"from": str(src), "to": str(dst)})
    return J(dst)


def check_step(src: S | str, dst: S | str) -> S:
    if not can_transition_step(src, dst):
        raise InvalidTransition(f"step transition {src} -> {dst} not allowed", details={"from": str(src), "to": str(dst)})
    return S(dst)


def recovery_step_state(status: S | str, *, has_checkpoint: bool) -> S:
    """Recovery mapping (P05 5.6): what an in-flight step becomes after a runtime crash."""
    st = S(status)
    if st in STEP_IN_FLIGHT:
        return S.checkpointed if has_checkpoint else S.ready
    return st


def recovery_job_state(status: J | str) -> J:
    """Jobs keep their macro state; transient worker/resource waits resume as running for re-evaluation."""
    st = J(status)
    if st in {J.waiting_for_worker, J.waking_worker, J.waiting_for_resources} | _ACTIVE_JOB:
        return J.running
    return st
