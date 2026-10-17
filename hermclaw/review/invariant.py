"""Deterministic review invariants (22.5) – applied by the runtime, never left to the model.

1. ``major``/``blocker`` finding → the verdict can never be ``pass`` (``enforce_review_invariant`` of the contract).
2. The deterministic verifier did not pass → the review can never pass either (facts beat opinions).
3. The model asks for fixes but names only ``minor`` findings → pass with the findings kept as notes: minor means
   "does not block acceptance" (review prompt), and a correction round for a duplicate test cost a whole attempt
   (rc17 battery J5, BUG-191). Applies to the model's own verdict only, never to a fail-closed or overridden review.

4. A major/blocker finding that claims a syntax error in a file whose syntax check the verifier passed contradicts a
   tool fact (the prompt forbids that) and is downgraded to ``minor`` with a note. rc23 battery X4: a misleading code
   comment ("unclosed string") made the model report a syntax error in a file that parsed, twice, and the step failed
   (BUG-217).

A failed or incomplete review (model error, timeout, empty diff) is handled by the reviewer as a fail-closed
outcome (verdict ``fix_required``), so no code path turns an error into a pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hermclaw.contracts.common import FindingSeverity
from hermclaw.contracts.review import ReviewContract, enforce_review_invariant
from hermclaw.contracts.verification import VerificationReport
from hermclaw.review.types import BLOCKING_SEVERITIES, OVERRIDE_BLOCKING_FINDING, OVERRIDE_MINOR_ONLY, OVERRIDE_VERIFIER_FAILED

_SYNTAX_CLAIM = re.compile(
    r"syntax\s*error|invalid syntax|unparseable|cannot be parsed|does not parse|fails? to parse|not (?:be )?parsed|"
    r"unclosed (?:string|quote|bracket|parenthes)|unterminated (?:string|triple)|unbalanced (?:bracket|parenthes|brace)",
    re.IGNORECASE,
)
CONTRADICTION_NOTE = "[contradicts the verifier: the syntax check of this file passed] "


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


def _passed_syntax_paths(verification: VerificationReport) -> set[str]:
    out: set[str] = set()
    for check in verification.checks:
        path = check.evidence.get("path")
        if check.check_type == "syntax" and check.status == "pass" and isinstance(path, str) and path:
            out.add(path)
    return out


def downgrade_contradicted_findings(review: ReviewContract, verification: VerificationReport) -> tuple[ReviewContract, list[str]]:
    """Rule 4: blocking syntax-error claims about files the verifier parsed successfully become ``minor``."""
    parsed = _passed_syntax_paths(verification)
    if not parsed:
        return review, []
    notes: list[str] = []
    findings = []
    for finding in review.findings:
        text = f"{finding.summary}\n{finding.evidence}"
        paths = {finding.path} if finding.path else {p for p in parsed if p in text}
        if finding.severity in BLOCKING_SEVERITIES and paths and paths <= parsed and _SYNTAX_CLAIM.search(text):
            summary = (CONTRADICTION_NOTE + finding.summary)[:2000]
            findings.append(finding.model_copy(update={"severity": FindingSeverity.minor, "summary": summary}))
            notes.append(f"downgraded a syntax-error claim about {', '.join(sorted(paths))}: the verifier parsed it")
        else:
            findings.append(finding)
    return (review.model_copy(update={"findings": findings}) if notes else review), notes
