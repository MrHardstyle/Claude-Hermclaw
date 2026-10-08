import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hermclaw.contracts.common import JobStatus, StepStatus
from hermclaw.core.errors import InvalidTransition
from hermclaw.runtime.state_machines import (
    JOB_TERMINAL,
    JOB_TRANSITIONS,
    STEP_TRANSITIONS,
    check_job,
    check_step,
    recovery_job_state,
    recovery_step_state,
)


def test_tables_cover_every_status():
    assert set(JOB_TRANSITIONS) == set(JobStatus)
    assert set(STEP_TRANSITIONS) == set(StepStatus)
    for targets in list(JOB_TRANSITIONS.values()) + list(STEP_TRANSITIONS.values()):
        assert all(t in JobStatus.__members__.values() or t in StepStatus.__members__.values() for t in targets)


def test_terminal_states():
    assert JOB_TRANSITIONS[JobStatus.succeeded] == frozenset()
    assert JOB_TRANSITIONS[JobStatus.cancelled] == frozenset()
    assert JOB_TRANSITIONS[JobStatus.failed] == frozenset({JobStatus.queued})
    assert STEP_TRANSITIONS[StepStatus.completed] == frozenset()


def test_invalid_transition_raises():
    with pytest.raises(InvalidTransition):
        check_job(JobStatus.succeeded, JobStatus.running)
    with pytest.raises(InvalidTransition):
        check_step(StepStatus.pending, StepStatus.completed)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(list(JobStatus)), min_size=1, max_size=40))
def test_job_random_walk_respects_table(seq):
    cur = JobStatus.queued
    for nxt in seq:
        if nxt in JOB_TRANSITIONS[cur]:
            cur = check_job(cur, nxt)
        else:
            with pytest.raises(InvalidTransition):
                check_job(cur, nxt)
    if cur in (JobStatus.succeeded, JobStatus.cancelled):
        assert not JOB_TRANSITIONS[cur]


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(list(StepStatus)), min_size=1, max_size=40))
def test_step_random_walk_respects_table(seq):
    cur = StepStatus.pending
    for nxt in seq:
        if nxt in STEP_TRANSITIONS[cur]:
            cur = check_step(cur, nxt)
        else:
            with pytest.raises(InvalidTransition):
                check_step(cur, nxt)


def test_every_nonterminal_job_state_can_reach_a_terminal_state():
    for start in JobStatus:
        seen, frontier = {start}, [start]
        while frontier:
            cur = frontier.pop()
            for n in JOB_TRANSITIONS[cur]:
                if n not in seen:
                    seen.add(n)
                    frontier.append(n)
        assert seen & JOB_TERMINAL or start in JOB_TERMINAL


@pytest.mark.parametrize("status", [StepStatus.leased, StepStatus.running, StepStatus.testing, StepStatus.verifying, StepStatus.reviewing])
def test_recovery_mapping_for_in_flight_steps(status):
    assert recovery_step_state(status, has_checkpoint=False) == StepStatus.ready
    assert recovery_step_state(status, has_checkpoint=True) == StepStatus.checkpointed
    assert recovery_step_state(StepStatus.completed, has_checkpoint=False) == StepStatus.completed
    assert recovery_job_state(JobStatus.waking_worker) == JobStatus.running
    assert recovery_job_state(JobStatus.succeeded) == JobStatus.succeeded
