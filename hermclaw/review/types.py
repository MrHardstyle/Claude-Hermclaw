"""Value types of the heavy review stage (Bauplan §22, Phase 22).

``ReviewInput`` is everything the heavy reviewer (Qwen3.8 27B, model role ``heavy``) gets: job goal, plan step
(kind, acceptance, constraints, scope), the unified diff against the workspace base, the deterministic verifier
report and optional code/test snippets. ``ReviewOutcome`` is the fail-closed result: the *effective* review after
severity normalisation and the deterministic invariants, the persisted run id and – on failure – a clear reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from hermclaw.contracts.common import FindingSeverity
from hermclaw.contracts.review import ReviewContract, ReviewFinding
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.verification import VerificationReport

ReviewStatus = Literal["completed", "error"]
Verdict = Literal["pass", "fix_required"]

# stable error codes of fail-closed outcomes (status == "error")
EMPTY_DIFF = "REVIEW_EMPTY_DIFF"
REVIEW_TIMEOUT = "REVIEW_TIMEOUT"
REVIEW_CANCELLED = "REVIEW_CANCELLED"
REVIEW_INTERNAL_ERROR = "REVIEW_INTERNAL_ERROR"
REVIEW_CONTEXT_ERROR = "REVIEW_CONTEXT_ERROR"
REVIEW_PROFILE_MISSING = "REVIEW_PROFILE_MISSING"

# invariant override reasons (22.5)
OVERRIDE_BLOCKING_FINDING = "major_or_blocker_finding"
OVERRIDE_VERIFIER_FAILED = "verifier_not_passed"

SEVERITY_RANK: dict[FindingSeverity, int] = {FindingSeverity.blocker: 0, FindingSeverity.major: 1, FindingSeverity.minor: 2}
BLOCKING_SEVERITIES = frozenset({FindingSeverity.major, FindingSeverity.blocker})


@dataclass(frozen=True)
class ReviewSettings:
    """Tunables of the review stage that are not part of ``policies.review`` (kept in code, not config)."""

    max_repairs: int = 2  # structured-output repair calls after the first answer
    prompt_safety_ratio: float = 0.9  # fraction of the estimated free context used for the prompt
    reserve_tokens: int = 256  # extra head-room for chat template differences
    min_prompt_chars: int = 6_000
    goal_chars: int = 3_000
    step_goal_chars: int = 3_000
    max_constraints: int = 30
    max_acceptance: int = 30
    item_chars: int = 800  # one constraint / acceptance criterion / scope entry
    max_scope_entries: int = 50
    verifier_share: float = 0.2  # max share of the variable budget for the verifier report
    snippet_share: float = 0.25  # max share of the variable budget for code/test snippets
    command_share: float = 0.15  # max share for an executed-commands log (non-repository mutations)
    check_message_chars: int = 600
    evidence_excerpt_chars: int = 600
    min_file_diff_chars: int = 400  # below this a truncated file only shows its header + omission note
    max_diff_files: int = 80  # further files are listed by name only
    generated_file_cap_chars: int = 400  # diff budget of generated files (policies.verifier.generated_file_globs)
    max_snippets: int = 12
    max_snippet_chars: int = 3_000
    snippet_budget_chars: int = 16_000  # what the workspace adapter asks repo intelligence for
    git_diff_max_bytes: int = 400_000  # what the workspace adapter asks the git reader for
    context_timeout_seconds: float = 120.0  # bound for collecting diff / snippets / commands
    max_command_log: int = 60  # executed commands of the attempt shown as change evidence
    review_failed_verification: bool = False  # should_review(kind, verifier_passed=False) -> True?


@dataclass(frozen=True)
class CodeSnippet:
    """A relevant piece of code or tests shown to the reviewer next to the diff."""

    path: str
    content: str
    start_line: int = 1
    end_line: int | None = None
    kind: Literal["code", "test"] = "code"


@dataclass(frozen=True)
class ReviewInput:
    job_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID | None
    goal: str  # job-level goal (title + prompt)
    step: StepContract  # kind, goal, acceptance, constraints, scope
    diff: str  # unified diff against the workspace base (committed + uncommitted + untracked)
    verification: VerificationReport  # deterministic facts
    snippets: Sequence[CodeSnippet] = ()
    command_log: Sequence[str] = ()  # executed commands – change evidence for steps that mutate outside the repo
    changed_files: Sequence[str] | None = None  # defaults to the verifier's changed_files / the diff's files


@dataclass(frozen=True)
class ReviewOutcome:
    """Fail-closed result of one heavy review. ``review`` is the *effective* contract (normalised + invariants)."""

    review_run_id: uuid.UUID
    status: ReviewStatus
    review: ReviewContract
    raw_verdict: str | None = None  # what the model said (None when the model was not called / failed)
    invariant_override: bool = False
    override_reasons: tuple[str, ...] = ()
    fail_closed: bool = False  # verdict forced to fix_required because the review could not be completed
    error_code: str | None = None
    reason: str = ""  # clear, redacted explanation (summary of the review or of the failure)
    model_alias: str | None = None
    repair_attempts: int = 0
    duration_ms: int = 0
    finding_ids: tuple[uuid.UUID, ...] = ()
    normalisation_notes: tuple[str, ...] = ()
    prompt_stats: dict[str, int | str | bool] = field(default_factory=dict)

    @property
    def verdict(self) -> Verdict:
        return self.review.verdict

    @property
    def passed(self) -> bool:
        """True only for a completed review whose effective verdict is pass (never on error)."""
        return self.status == "completed" and not self.fail_closed and self.review.verdict == "pass"

    @property
    def blocking_findings(self) -> list[ReviewFinding]:
        return [f for f in self.review.findings if f.severity in BLOCKING_SEVERITIES]
