"""ReviewContract – heavy review output with deterministic invariant (Bauplan §22)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from hermclaw.contracts.common import Contract, FindingSeverity


class ReviewFinding(Contract):
    severity: FindingSeverity
    path: str = Field(default="", max_length=500)
    summary: str = Field(min_length=3, max_length=2000)
    evidence: str = Field(default="", max_length=4000)
    suggested_fix: str = Field(default="", max_length=4000)


class ReviewContract(Contract):
    verdict: Literal["pass", "fix_required"]
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=50)
    summary: str = Field(default="", max_length=4000)


def enforce_review_invariant(review: ReviewContract) -> tuple[ReviewContract, bool]:
    """major/blocker -> verdict cannot be pass. Returns (effective review, overridden?)."""
    blocking = [f for f in review.findings if f.severity in (FindingSeverity.major, FindingSeverity.blocker)]
    if blocking and review.verdict == "pass":
        return review.model_copy(update={"verdict": "fix_required"}), True
    return review, False
