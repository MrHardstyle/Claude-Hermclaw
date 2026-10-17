"""Deterministic review invariants (22.5) – applied by the runtime, never left to the model.

1. ``major``/``blocker`` finding → the verdict can never be ``pass`` (``enforce_review_invariant`` of the contract).
2. The deterministic verifier did not pass → the review can never pass either (facts beat opinions).
3. The model asks for fixes but names only ``minor`` findings → pass with the findings kept as notes: minor means
   "does not block acceptance" (review prompt), and a correction round for a duplicate test cost a whole attempt
   (rc17 battery J5, BUG-191). Applies to the model's own verdict only, never to a fail-closed or overridden review.

A failed or incomplete review (model error, timeout, empty diff) is handled by the reviewer as a fail-closed
outcome (verdict ``fix_required``), so no code path turns an error into a pass.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermclaw.contracts.review import ReviewContract, enforce_review_invariant
from hermclaw.review.types import BLOCKING_SEVERITIES, OVERRIDE_BLOCKING_FINDING, OVERRIDE_MINOR_ONLY, OVERRIDE_VERIFIER_FAILED


@dataclass(frozen=True)
class InvariantResult:
    review: ReviewContract  # effective review
    raw_verdict: str
    overridden: bool
    reasons: tuple[str, ...]


def apply_review_invariants(review: ReviewContract, *, verifier_passed: bool) -> InvariantResult:
    effective, overridden = enforce_review_invariant(review)
    reasons: list[str] = []
    minor_only = bool(review.findings) and not any(f.severity in BLOCKING_SEVERITIES for f in review.findings)
    if review.verdict == "fix_required" and minor_only and verifier_passed:
        effective = effective.model_copy(update={"verdict": "pass"})
        reasons.append(OVERRIDE_MINOR_ONLY)
    if review.verdict == "pass":
        if overridden or any(f.severity in BLOCKING_SEVERITIES for f in review.findings):
            reasons.append(OVERRIDE_BLOCKING_FINDING)
        if not verifier_passed:
            reasons.append(OVERRIDE_VERIFIER_FAILED)
    if (OVERRIDE_BLOCKING_FINDING in reasons or OVERRIDE_VERIFIER_FAILED in reasons) and effective.verdict == "pass":
        effective = effective.model_copy(update={"verdict": "fix_required"})
    return InvariantResult(review=effective, raw_verdict=review.verdict, overridden=bool(reasons), reasons=tuple(reasons))
