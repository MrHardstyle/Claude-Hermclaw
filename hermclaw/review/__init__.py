"""Heavy review stage (Bauplan §22, Phase 22): Qwen3.8 27B (model role ``heavy``) reviews one completed step.

- ``prompt``     (22.1) goal, plan step, scope, size-budgeted diff, verifier facts, relevant code/tests.
- ``reviewer``   (22.2/22.3) ``HeavyReviewer`` – structured ``ReviewContract`` via ``ChatModel.structured``;
  persisted ``review_runs`` + ``review_findings`` + events; fail-closed on every error.
- ``severity``   (22.4) severity/verdict normalisation, path canonicalisation, de-duplication, ordering.
- ``invariant``  (22.5) major/blocker or a failed verifier never PASS.
- ``correction`` (22.6) ``CorrectionRequest`` for the next coder attempt (P23).
- ``policy``     ``should_review(step_kind, verifier_passed)`` from ``policies.review.required_for_kinds``.
- ``workspace``  ``WorkspaceReviewer`` – the ``ReviewerPort`` adapter (GitReader / RepoContextProvider / PostgreSQL).
"""

from hermclaw.review.correction import (
    CorrectionFinding,
    CorrectionLimits,
    CorrectionRequest,
    VerifierFailure,
    build_correction_request,
)
from hermclaw.review.invariant import InvariantResult, apply_review_invariants
from hermclaw.review.policy import requires_change_evidence, should_review
from hermclaw.review.prompt import REVIEW_SYSTEM_PROMPT, ReviewPrompt, build_review_prompt
from hermclaw.review.reviewer import HEAVY_ROLE, HeavyReviewer
from hermclaw.review.severity import ReviewDraft, normalise_findings, normalise_severity
from hermclaw.review.types import (
    EMPTY_DIFF,
    REVIEW_CANCELLED,
    REVIEW_CONTEXT_ERROR,
    REVIEW_INTERNAL_ERROR,
    REVIEW_PROFILE_MISSING,
    REVIEW_TIMEOUT,
    CodeSnippet,
    ReviewInput,
    ReviewOutcome,
    ReviewSettings,
)
from hermclaw.review.workspace import WorkspaceReviewer, is_test_path

__all__ = [
    "EMPTY_DIFF",
    "HEAVY_ROLE",
    "REVIEW_CANCELLED",
    "REVIEW_CONTEXT_ERROR",
    "REVIEW_INTERNAL_ERROR",
    "REVIEW_PROFILE_MISSING",
    "REVIEW_SYSTEM_PROMPT",
    "REVIEW_TIMEOUT",
    "CodeSnippet",
    "CorrectionFinding",
    "CorrectionLimits",
    "CorrectionRequest",
    "HeavyReviewer",
    "InvariantResult",
    "ReviewDraft",
    "ReviewInput",
    "ReviewOutcome",
    "ReviewPrompt",
    "ReviewSettings",
    "VerifierFailure",
    "WorkspaceReviewer",
    "apply_review_invariants",
    "build_correction_request",
    "build_review_prompt",
    "is_test_path",
    "normalise_findings",
    "normalise_severity",
    "requires_change_evidence",
    "should_review",
]
