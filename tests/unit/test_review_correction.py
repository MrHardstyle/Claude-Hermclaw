"""Correction request (22.6): ordering, de-duplication against verifier facts, required changes, serialisation."""

from __future__ import annotations

import json
import uuid

from hermclaw.contracts.common import FindingSeverity
from hermclaw.contracts.review import ReviewContract, ReviewFinding
from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.verification import VerificationCheck, VerificationReport
from hermclaw.review import (
    EMPTY_DIFF,
    REVIEW_TIMEOUT,
    CorrectionLimits,
    CorrectionRequest,
    ReviewOutcome,
    build_correction_request,
)
from hermclaw.review.correction import RUNTIME_RULES, evidence_excerpt

STEP_ID = uuid.uuid4()
ATTEMPT_ID = uuid.uuid4()


def _check(
    check_type: str, name: str, message: str, *, status: str = "fail", blocking: bool = True, **evidence: object
) -> VerificationCheck:
    return VerificationCheck(check_type=check_type, name=name, status=status, message=message, evidence=dict(evidence), blocking=blocking)  # type: ignore[arg-type]


def _finding(severity: str, summary: str, path: str = "app.py", evidence: str = "", fix: str = "") -> ReviewFinding:
    return ReviewFinding(severity=FindingSeverity(severity), path=path, summary=summary, evidence=evidence, suggested_fix=fix)


def _report(*checks: VerificationCheck) -> VerificationReport:
    return VerificationReport(passed=not any(c.status in ("fail", "error") and c.blocking for c in checks), checks=list(checks))


def _step() -> StepContract:
    return StepContract.model_validate(
        {
            "id": STEP_ID,
            "job_id": uuid.uuid4(),
            "step_key": "S001",
            "title": "t",
            "kind": "implement",
            "capability": "code.implement",
            "goal": "g",
            "status": "running",
            "constraints": ["Keep the public API backwards compatible", "  "],
            "scope": ScopeContract(target_paths=["app.py"], allowed_new_paths=["tests/test_app.py"], forbidden_paths=["db/**"]),
        }
    )


def test_verifier_first_then_findings_by_severity() -> None:
    report = _report(
        _check("unit", "pytest", "test_sub failed", stderr="AssertionError: assert 4 == 2", path="tests/test_app.py"),
        _check("lint", "ruff", "advisory only", blocking=False),
        _check("syntax", "python", "ok", status="pass"),
    )
    review = ReviewContract(
        verdict="fix_required",
        findings=[
            _finding("minor", "rename variable x"),
            _finding("major", "sub() ignores negative inputs", fix="handle negative numbers"),
            _finding("blocker", "SQL built by string concatenation", path="db.py", evidence='"SELECT " + name', fix="use bound parameters"),
        ],
    )
    req = build_correction_request(step_id=STEP_ID, attempt_id=ATTEMPT_ID, verification=report, review=review, step=_step())

    assert [v.label for v in req.verifier_failures] == ["unit:pytest"]  # advisory + passing checks excluded
    failure = req.verifier_failures[0]
    assert failure.path == "tests/test_app.py" and "AssertionError: assert 4 == 2" in failure.evidence
    assert [f.severity.value for f in req.review_findings] == ["blocker", "major", "minor"]
    assert req.source == "verifier+review" and not req.is_empty
    assert req.required_changes == (
        "Make the failing unit check 'pytest' in tests/test_app.py pass: test_sub failed",
        "[blocker] db.py: use bound parameters",
        "[major] app.py: handle negative numbers",
    )  # minor findings are optional
    assert req.constraints[0] == "Keep the public API backwards compatible"
    assert req.constraints[1].startswith("Scope: target_paths=app.py; allowed_new_paths=tests/test_app.py; forbidden_paths=db/**")
    assert req.constraints[-len(RUNTIME_RULES) :] == RUNTIME_RULES
    items = req.items()
    assert [i["source"] for i in items] == ["verifier", "review", "review", "review"]
    assert items[0]["label"] == "unit:pytest" and "evidence:" in items[0]["message"]
    assert items[1]["label"] == "blocker" and items[1]["suggested_fix"] == "use bound parameters"


def test_review_findings_restating_verifier_facts_are_dropped() -> None:
    report = _report(
        _check("unit", "pytest", "tests/test_app.py::test_sub failed: assert 4 == 2", path="tests/test_app.py"),
        _check("lint", "ruff", "F401 'os' imported but unused", path="app.py"),
    )
    review = ReviewContract(
        verdict="fix_required",
        findings=[
            # restates the pytest failure (message contained in the evidence) – its fix is kept on the failure
            _finding(
                "major",
                "the sub test fails",
                path="tests/test_app.py",
                evidence="verifier: tests/test_app.py::test_sub failed: assert 4 == 2",
                fix="return a - b instead of a + b",
            ),
            # restates the lint failure via token overlap
            _finding("minor", "unused import os in app.py", evidence="F401 'os' imported but unused"),
            # names the check label explicitly
            _finding("major", "lint:ruff reports problems", path=""),
            # a genuine new problem on the same file
            _finding("blocker", "sub() silently truncates floats", path="app.py", evidence="+    return int(a - b)"),
            # same text as the lint message but a different file → not a duplicate
            _finding("minor", "F401 'os' imported but unused", path="other.py"),
        ],
    )
    req = build_correction_request(step_id=STEP_ID, attempt_id=ATTEMPT_ID, verification=report, review=review)
    assert req.dropped_duplicates == 3
    assert [(f.severity.value, f.path) for f in req.review_findings] == [("blocker", "app.py"), ("minor", "other.py")]
    pytest_failure = req.verifier_failures[0]
    assert pytest_failure.suggested_fix == "return a - b instead of a + b"
    assert req.required_changes[0] == ("Make the failing unit check 'pytest' in tests/test_app.py pass: return a - b instead of a + b")
    assert any("restated verifier failures" in n for n in req.notes)


def test_findings_deduplicated_among_themselves_keep_highest_severity() -> None:
    review = ReviewContract(
        verdict="fix_required",
        findings=[_finding("minor", "Missing   error handling"), _finding("major", "missing error handling", fix="raise ValueError")],
    )
    req = build_correction_request(step_id=STEP_ID, attempt_id=None, review=review)
    assert len(req.review_findings) == 1
    only = req.review_findings[0]
    assert only.severity == FindingSeverity.major and only.suggested_fix == "raise ValueError"
    assert req.source == "review"


def test_empty_diff_outcome_yields_required_change() -> None:
    outcome = ReviewOutcome(
        review_run_id=uuid.uuid4(),
        status="error",
        review=ReviewContract(verdict="fix_required", summary="fail-closed: no changes"),
        fail_closed=True,
        error_code=EMPTY_DIFF,
        reason="fail-closed: the mutating step kind 'implement' produced no changes",
    )
    req = build_correction_request(step_id=STEP_ID, attempt_id=ATTEMPT_ID, review=outcome)
    assert req.review_run_id == outcome.review_run_id and req.review_error_code == EMPTY_DIFF
    assert req.required_changes[0].startswith("The previous attempt produced no changes")
    assert req.source == "runtime"
    assert req.items()[-1] == {
        "source": "runtime",
        "label": EMPTY_DIFF,
        "message": outcome.reason,
        "path": "",
        "suggested_fix": "",
    }


def test_review_error_without_other_evidence_is_reported() -> None:
    outcome = ReviewOutcome(
        review_run_id=uuid.uuid4(),
        status="error",
        review=ReviewContract(verdict="fix_required"),
        fail_closed=True,
        error_code=REVIEW_TIMEOUT,
        reason="fail-closed: heavy review 'heavy-review' did not finish within 1500s",
    )
    req = build_correction_request(step_id=STEP_ID, attempt_id=None, review=outcome)
    assert req.review_error.startswith("fail-closed: heavy review")
    assert req.required_changes == (
        "Review could not be completed (REVIEW_TIMEOUT); re-check the change: fail-closed: heavy review 'heavy-review' did not finish within 1500s",
    )
    assert not req.is_empty


def test_passing_review_and_verifier_is_empty() -> None:
    req = build_correction_request(
        step_id=STEP_ID,
        attempt_id=None,
        verification=_report(_check("syntax", "python", "ok", status="pass")),
        review=ReviewContract(verdict="pass"),
    )
    assert req.is_empty and req.source == "none" and req.required_changes == ()


def test_inconsistent_failed_report_keeps_the_fact() -> None:
    report = VerificationReport(passed=False, checks=[], summary="verifier crashed before running checks")
    req = build_correction_request(step_id=STEP_ID, attempt_id=None, verification=report)
    assert [v.label for v in req.verifier_failures] == ["verifier:report"]
    assert req.verifier_failures[0].message == "verifier crashed before running checks"


def test_secrets_reasoning_and_size_limits() -> None:
    token = "ghp_" + "S" * 36
    report = _report(*[_check("unit", f"t{i}", f"failure {i} {token}", stdout="x" * 5000) for i in range(40)])
    review = ReviewContract(
        verdict="fix_required",
        findings=[_finding("major", f"<think>private</think>problem number {i} {token}", path=f"m{i}.py") for i in range(40)],
    )
    limits = CorrectionLimits(max_verifier_failures=5, max_review_findings=7, evidence_chars=200)
    req = build_correction_request(step_id=STEP_ID, attempt_id=None, verification=report, review=review, limits=limits)
    blob = json.dumps(req.to_dict())
    assert token not in blob and "private" not in blob
    assert len(req.verifier_failures) == 5 and len(req.review_findings) == 7
    assert all(len(v.evidence) <= 200 for v in req.verifier_failures)
    assert any("capped" in n for n in req.notes)


def test_to_dict_round_trip_and_item_format() -> None:
    report = _report(_check("compile", "tsc", "TS2322: type mismatch", path="web/a.ts"))
    review = ReviewContract(
        verdict="fix_required", findings=[_finding("blocker", "XSS via innerHTML", path="web/b.ts", fix="use textContent")]
    )
    req = build_correction_request(
        step_id=STEP_ID, attempt_id=ATTEMPT_ID, verification=report, review=review, verification_run_id=uuid.uuid4()
    )
    data = json.loads(json.dumps(req.to_dict()))  # JSON-serialisable (step_attempts.correction_input)
    assert data["kind"] == "correction_request" and data["version"] == 1 and data["source"] == "verifier+review"
    for item in data["items"]:
        assert set(item) == {"source", "label", "message", "path", "suggested_fix"} and item["message"]
    back = CorrectionRequest.from_dict(data)
    assert back == req


def test_from_dict_is_tolerant() -> None:
    data = {
        "step_id": str(STEP_ID),
        "attempt_id": "not-a-uuid",
        "verifier_failures": [{"check_type": "unit", "name": "x", "message": "m"}, "junk"],
        "review_findings": [{"severity": "catastrophic", "summary": "bad"}, {"severity": "minor", "summary": ""}],
        "unknown": 1,
    }
    req = CorrectionRequest.from_dict(data)
    assert req.attempt_id is None and len(req.verifier_failures) == 1
    assert [(f.severity, f.summary) for f in req.review_findings] == [(FindingSeverity.major, "bad")]
    assert req.constraints == RUNTIME_RULES


def test_render_orders_sections() -> None:
    req = build_correction_request(
        step_id=STEP_ID,
        attempt_id=None,
        verification=_report(_check("unit", "pytest", "1 failed")),
        review=ReviewContract(verdict="fix_required", findings=[_finding("major", "edge case", fix="handle empty input")]),
    )
    text = req.render()
    heads = ["VERIFIER FAILURES", "REVIEW FINDINGS", "REQUIRED CHANGES", "CONSTRAINTS"]
    assert [text.index(h) for h in heads] == sorted(text.index(h) for h in heads)
    assert len(req.render(max_chars=100)) <= 100


def test_evidence_excerpt_prioritises_output() -> None:
    text = evidence_excerpt({"zeta": 1, "stdout": "", "stderr": "Traceback: boom", "cmd": "pytest"}, 200)
    assert text.startswith("stderr: Traceback: boom") and '"cmd":"pytest"' in text
    assert evidence_excerpt({}, 100) == ""


def test_non_repository_paths_never_become_item_paths() -> None:
    review = ReviewContract(verdict="fix_required", findings=[_finding("major", "reads outside the repo", path="../../etc/shadow")])
    report = _report(_check("unit", "pytest", "boom", path="/abs/outside.py"))
    req = build_correction_request(step_id=STEP_ID, attempt_id=None, verification=report, review=review)
    assert all(not item["path"] for item in req.items())
    assert "'../../etc/shadow' is not a repository path" in req.review_findings[0].summary
