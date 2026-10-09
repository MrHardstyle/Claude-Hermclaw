"""Severity / verdict normalisation (22.4), invariants (22.5) and the review policy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermclaw.contracts.common import FindingSeverity
from hermclaw.contracts.review import ReviewContract, ReviewFinding
from hermclaw.core.config import ReviewPolicy
from hermclaw.review import (
    ReviewDraft,
    apply_review_invariants,
    normalise_findings,
    normalise_severity,
    requires_change_evidence,
    should_review,
)
from hermclaw.review.severity import MAX_FINDINGS, normalise_verdict, redact_review
from hermclaw.review.text import clip, clip_lines, fence, strip_reasoning
from hermclaw.review.types import OVERRIDE_BLOCKING_FINDING, OVERRIDE_VERIFIER_FAILED


@pytest.mark.parametrize(
    ("raw", "expected", "changed"),
    [
        ("blocker", FindingSeverity.blocker, False),
        ("Critical", FindingSeverity.blocker, True),
        ("must fix", FindingSeverity.blocker, True),
        ("SECURITY", FindingSeverity.blocker, True),
        ("major", FindingSeverity.major, False),
        ("high", FindingSeverity.major, True),
        ("medium", FindingSeverity.major, True),
        ("warning", FindingSeverity.major, True),
        ("minor", FindingSeverity.minor, False),
        ("nit-pick", FindingSeverity.minor, True),
        ("Low", FindingSeverity.minor, True),
        ("info", FindingSeverity.minor, True),
        ("weird", FindingSeverity.major, True),  # unknown → major (fail-closed)
        (None, FindingSeverity.major, True),
        (3, FindingSeverity.major, True),
        (FindingSeverity.minor, FindingSeverity.minor, False),
    ],
)
def test_normalise_severity(raw: object, expected: FindingSeverity, changed: bool) -> None:
    assert normalise_severity(raw) == (expected, changed)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pass", "pass"),
        ("Approved", "pass"),
        ("LGTM-not-a-verdict", "LGTM-not-a-verdict"),
        ("changes requested", "fix_required"),
        ("fail", "fix_required"),
    ],
)
def test_normalise_verdict(raw: str, expected: str) -> None:
    assert normalise_verdict(raw)[0] == expected


def test_draft_accepts_aliases_and_drops_unknown_fields() -> None:
    draft = ReviewDraft.model_validate(
        {
            "decision": "Request Changes",
            "issues": [
                {
                    "level": "HIGH",
                    "file": "src/a.py",
                    "description": "wrong sign",
                    "quote": "+ return b - a",
                    "fix": "swap operands",
                    "line": 7,
                },
                {"severity": "nit", "filename": "src/b.py", "title": "naming", "confidence": 0.3},
            ],
            "overall": "needs work",
            "reasoning": "hidden",
        }
    )
    assert draft.verdict == "fix_required" and draft.summary == "needs work"
    first, second = draft.findings
    assert (first.severity, first.path, first.summary, first.suggested_fix) == (
        FindingSeverity.major,
        "src/a.py",
        "wrong sign",
        "swap operands",
    )
    assert first.evidence.startswith("line 7")
    assert second.severity == FindingSeverity.minor and second.path == "src/b.py"
    notes = draft.normalisation_notes
    assert any("dropped unknown field 'reasoning'" in n for n in notes)
    assert any("dropped unknown field 'confidence'" in n for n in notes)
    contract = draft.to_contract()
    assert type(contract) is ReviewContract and contract.findings == draft.findings


def test_draft_schema_matches_contract_schema() -> None:
    draft_schema = ReviewDraft.model_json_schema()
    contract_schema = ReviewContract.model_json_schema()
    assert draft_schema["title"] == "ReviewContract"
    assert draft_schema["properties"] == contract_schema["properties"]
    assert draft_schema.get("required") == contract_schema.get("required")


@pytest.mark.parametrize(
    "raw",
    [
        {"verdict": "looks good", "findings": []},  # unknown verdict → repair
        {"findings": []},  # missing verdict → repair
        {"verdict": "pass", "findings": [{"severity": "major", "path": "a.py"}]},  # finding without summary → repair
        ["pass"],
    ],
)
def test_draft_rejects_unusable_answers(raw: object) -> None:
    with pytest.raises(ValidationError):
        ReviewDraft.model_validate(raw)


def test_draft_strips_reasoning_markup_and_caps_findings() -> None:
    raw = {
        "verdict": "fix_required",
        "summary": "<think>secret plan</think>Summary text",
        "findings": [{"severity": "minor", "summary": f"m{i:03d}"} for i in range(MAX_FINDINGS + 5)]
        + [{"severity": "blocker", "summary": "<thinking>x</thinking>the blocker"}],
    }
    draft = ReviewDraft.model_validate(raw)
    assert draft.summary == "Summary text"
    assert len(draft.findings) == MAX_FINDINGS
    assert any(f.severity == FindingSeverity.blocker and f.summary == "the blocker" for f in draft.findings)  # most severe kept


def test_draft_clips_overlong_text() -> None:
    draft = ReviewDraft.model_validate(
        {"verdict": "pass", "findings": [{"severity": "minor", "summary": "x" * 5000, "evidence": "y" * 9000}]}
    )
    f = draft.findings[0]
    assert len(f.summary) <= 2000 and len(f.evidence) <= 4000 and "omitted" in f.summary


def test_normalise_findings_paths_dedupe_and_order() -> None:
    review = ReviewContract(
        verdict="fix_required",
        findings=[
            ReviewFinding(severity=FindingSeverity.minor, path="b/src/app.py", summary="Style issue"),
            ReviewFinding(severity=FindingSeverity.major, path="./src/app.py:12", summary="wrong result", evidence="x"),
            ReviewFinding(severity=FindingSeverity.blocker, path="src/app.py", summary="style   issue"),  # duplicate of #0
            ReviewFinding(severity=FindingSeverity.minor, path="../etc/passwd", summary="odd path"),
            ReviewFinding(severity=FindingSeverity.minor, path="other.py", summary="outside"),
        ],
    )
    out, notes = normalise_findings(review, ["src/app.py"])
    assert [(f.severity.value, f.path) for f in out.findings] == [
        ("blocker", "src/app.py"),  # merged duplicate keeps the higher severity and the first position within it
        ("major", "src/app.py"),
        ("minor", ""),  # not a repository path → removed from the path field, kept as evidence text
        ("minor", "other.py"),
    ]
    assert out.findings[1].evidence == "line 12: x"
    assert out.findings[2].evidence.startswith("cited path '../etc/passwd' is not a repository path")
    assert any("non-repository path" in n for n in notes)
    assert any("duplicate finding merged" in n for n in notes)
    assert any("outside the changed files" in n for n in notes)


def test_redact_review_masks_secrets() -> None:
    token = "ghp_" + "Z" * 36
    review = ReviewContract(
        verdict="fix_required",
        findings=[
            ReviewFinding(severity=FindingSeverity.blocker, path="a.py", summary=f"token {token}", evidence=token, suggested_fix=token)
        ],
        summary=token,
    )
    out = redact_review(review)
    assert token not in repr(out)


# ----------------------------------------------------------------------------------------------- invariant (22.5)
def _review(verdict: str, *severities: FindingSeverity) -> ReviewContract:
    return ReviewContract(
        verdict=verdict,  # type: ignore[arg-type]
        findings=[ReviewFinding(severity=s, path="a.py", summary=f"finding {i}") for i, s in enumerate(severities)],
    )


@pytest.mark.parametrize("severity", [FindingSeverity.major, FindingSeverity.blocker])
def test_blocking_finding_never_passes(severity: FindingSeverity) -> None:
    res = apply_review_invariants(_review("pass", FindingSeverity.minor, severity), verifier_passed=True)
    assert res.review.verdict == "fix_required" and res.raw_verdict == "pass"
    assert res.overridden and res.reasons == (OVERRIDE_BLOCKING_FINDING,)


def test_failed_verifier_never_passes_and_reasons_combine() -> None:
    res = apply_review_invariants(_review("pass"), verifier_passed=False)
    assert res.review.verdict == "fix_required" and res.reasons == (OVERRIDE_VERIFIER_FAILED,)
    both = apply_review_invariants(_review("pass", FindingSeverity.major), verifier_passed=False)
    assert both.reasons == (OVERRIDE_BLOCKING_FINDING, OVERRIDE_VERIFIER_FAILED)


def test_invariant_keeps_consistent_verdicts() -> None:
    assert not apply_review_invariants(_review("pass", FindingSeverity.minor), verifier_passed=True).overridden
    res = apply_review_invariants(_review("fix_required", FindingSeverity.major), verifier_passed=False)
    assert res.review.verdict == "fix_required" and not res.overridden
    # fix_required without blocking findings is respected (a reviewer may be stricter than the invariant)
    assert apply_review_invariants(_review("fix_required"), verifier_passed=True).review.verdict == "fix_required"


# ----------------------------------------------------------------------------------------------- policy
def test_should_review_policy() -> None:
    policy = ReviewPolicy(required_for_kinds=["implement", " SSH "])
    assert should_review(policy, "implement", True)
    assert should_review(policy, "ssh", True)
    assert not should_review(policy, "implement", False)
    assert should_review(policy, "implement", False, review_failed_verification=True)
    assert not should_review(policy, "research", True)
    assert not should_review(ReviewPolicy(required_for_kinds=[]), "implement", True)


def test_requires_change_evidence() -> None:
    assert requires_change_evidence("implement") and requires_change_evidence("documentation") and requires_change_evidence("ssh")
    assert not requires_change_evidence("test") and not requires_change_evidence("verify")


# ----------------------------------------------------------------------------------------------- text helpers
def test_text_helpers() -> None:
    assert clip("abc", 10) == "abc"
    clipped = clip("x" * 100, 40)
    assert len(clipped) <= 40 and "omitted" in clipped
    body, cut = clip_lines("\n".join(f"line {i}" for i in range(100)), 120)
    assert cut and body.startswith("line 0") and "more lines omitted" in body and len(body) <= 160
    fenced = fence("a\n```\nb", "diff")
    assert fenced.startswith("````diff") and fenced.endswith("````")
    assert strip_reasoning("<think>a</think>b") == "b"
    assert strip_reasoning("<reasoning>never closed") == ""
    assert strip_reasoning("leak</think>answer") == "answer"
    assert strip_reasoning("a < b and c > d") == "a < b and c > d"


@pytest.mark.parametrize("raw", ["none", "N/A", "", "no findings", "[]"])
def test_textual_no_findings_means_empty(raw: str) -> None:
    assert ReviewDraft.model_validate({"verdict": "pass", "findings": raw}).findings == []


def test_free_text_findings_become_one_major_finding() -> None:
    draft = ReviewDraft.model_validate({"verdict": "pass", "findings": "the loop never terminates"})
    assert [(f.severity, f.summary) for f in draft.findings] == [(FindingSeverity.major, "the loop never terminates")]


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../secret.txt", "src/../../x.py"])
def test_absolute_and_traversing_paths_are_not_kept(bad: str) -> None:
    review = ReviewContract(verdict="fix_required", findings=[ReviewFinding(severity=FindingSeverity.major, path=bad, summary="look here")])
    out, _ = normalise_findings(review, ["app.py"])
    assert out.findings[0].path == "" and bad in out.findings[0].evidence
