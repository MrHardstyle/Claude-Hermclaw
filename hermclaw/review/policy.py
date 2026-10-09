"""When is a heavy review required? (``policies.review.required_for_kinds``)."""

from __future__ import annotations

from hermclaw.contracts.common import MUTATING_STEP_KINDS
from hermclaw.core.config import ReviewPolicy


def _kind(step_kind: str) -> str:
    return str(step_kind).strip().lower()


def should_review(policy: ReviewPolicy, step_kind: str, verifier_passed: bool, *, review_failed_verification: bool = False) -> bool:
    """Review only step kinds listed in ``required_for_kinds``; by default only after the verifier passed.

    A failed verification already yields deterministic correction evidence and can never pass review (22.5), so
    the 27B reviewer is skipped unless ``review_failed_verification`` asks for richer correction input."""
    required = {_kind(k) for k in policy.required_for_kinds}
    if _kind(step_kind) not in required:
        return False
    return verifier_passed or review_failed_verification


def requires_change_evidence(step_kind: str) -> bool:
    """Mutating step kinds must show changes (a diff or an executed-command log) – otherwise review fails closed."""
    return _kind(step_kind) in {k.value for k in MUTATING_STEP_KINDS}
