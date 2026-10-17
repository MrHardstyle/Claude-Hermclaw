"""Job scope = union of the step scopes (job-level review, regression rerun after a base update)."""

from __future__ import annotations

from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import ScopePolicy
from hermclaw.runtime.adapters import merge_scopes
from hermclaw.scope.guard import ScopeGuard


def _scope(**kw: object) -> ScopeContract:
    return ScopeContract(source="manual", reason="test", **kw)  # type: ignore[arg-type]


def test_one_steps_leave_alone_does_not_forbid_another_steps_files() -> None:
    """BUG-196: S2 must not touch tests/, S7 adds tests/test_x.py – the job change is legitimate, but the merged
    scope kept ``tests/**`` forbidden and the job-level review failed deterministically at the end of the job."""
    s2 = _scope(target_paths=["app/core.py"], forbidden_paths=["tests/**", "migrations/**"])
    s7 = _scope(target_paths=["app/cli.py"], allowed_new_paths=["tests/test_x.py"], forbidden_paths=["migrations/**"])
    merged = merge_scopes([s2, s7])
    assert merged is not None and merged.forbidden_paths == ["migrations/**"]  # forbidden by every step: stays
    guard = ScopeGuard(merged, ScopePolicy())
    assert guard.decide("tests/test_x.py", "create")[0]
    assert guard.decide("app/core.py", "modify")[0] and guard.decide("app/cli.py", "modify")[0]
    assert not guard.decide("migrations/0002.py", "create")[0]
    assert not guard.decide("tests/test_other.py", "create")[0]  # allowed by no step: still rejected
