"""Correction request (22.6) – the evidence package the correction pipeline (P23) hands to the next coder attempt.

``build_correction_request`` merges the *facts* of the deterministic verifier with the *opinions* of the heavy
review into one deterministic, size-bounded, redacted structure:

- ``verifier_failures`` – blocking failed/error checks (check type, name, message, evidence excerpt, path) in report
  order; they always come first because tools do not lie.
- ``review_findings`` – blocker → major → minor (stable within a severity), de-duplicated among themselves and
  *against the verifier*: a finding that only restates a verifier failure is dropped (its suggested fix is kept on the
  verifier failure), so the coder does not see the same problem twice.
- ``required_changes`` – the concrete to-do list (one entry per verifier failure and per major/blocker finding, plus
  the fail-closed reason when the step produced no changes); minor findings are optional and not listed here.
- ``constraints`` – reminder of the step constraints and the non-negotiable runtime rules (explicit scope, no test
  weakening, no special cases, runtime-controlled Git).

``to_dict()`` is JSON-serialisable and contains ``items`` in the format ``hermclaw.coder.handler`` /
``hermclaw.context_builder.CorrectionItem`` read (``source``/``label``/``message``/``path``/``suggested_fix``), so it
can be stored directly in ``step_attempts.correction_input``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from hermclaw.contracts.common import FindingSeverity
from hermclaw.contracts.review import ReviewContract, ReviewFinding
from hermclaw.contracts.scope import ScopeContract, normalise_path
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.verification import VerificationCheck, VerificationReport
from hermclaw.review.text import clip, compact_json, one_line, redact, strip_reasoning
from hermclaw.review.types import BLOCKING_SEVERITIES, EMPTY_DIFF, SEVERITY_RANK, ReviewOutcome

CORRECTION_REQUEST_KIND = "correction_request"
CORRECTION_REQUEST_VERSION = 1

CorrectionSource = Literal["verifier", "review", "verifier+review", "runtime", "none"]

#: Non-negotiable rules repeated in every correction request (generic, never task specific).
RUNTIME_RULES: tuple[str, ...] = (
    "Stay inside the explicit scope: modify only target paths, create only allowed new paths, never touch forbidden paths.",
    "Fix the root cause generically; do not add special cases that only satisfy a specific check, input or test.",
    "Do not weaken, skip, delete or rewrite tests or acceptance checks to make them pass.",
    "Git operations (stage, commit, push, reset) are performed by the runtime only.",
)

_EVIDENCE_TEXT_KEYS = (
    "stderr",
    "stdout",
    "output",
    "excerpt",
    "error",
    "errors",
    "detail",
    "details",
    "reason",
    "violations",
    "missing",
    "matches",
    "found",
    "expected",
    "actual",
)
_EVIDENCE_PATH_KEYS = ("path", "file", "file_path")
_EVIDENCE_PATHS_KEYS = ("paths", "files", "changed_files")
_TOKEN = re.compile(r"[a-z0-9_]{3,}")
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "not",
        "are",
        "was",
        "has",
        "have",
        "but",
        "from",
        "into",
        "its",
        "should",
        "must",
        "does",
        "line",
        "file",
        "check",
        "failed",
        "fails",
        "error",
        "verifier",
    }
)


@dataclass(frozen=True)
class CorrectionLimits:
    max_verifier_failures: int = 30
    max_review_findings: int = 30
    max_required_changes: int = 40
    max_constraints: int = 20
    message_chars: int = 800
    evidence_chars: int = 600
    summary_chars: int = 800
    fix_chars: int = 800
    change_chars: int = 600
    duplicate_overlap: float = 0.6  # token overlap (relative to the smaller side) that marks a duplicate
    min_contained_chars: int = 12  # minimum length for the substring duplicate test


@dataclass(frozen=True)
class VerifierFailure:
    check_type: str
    name: str
    status: str
    message: str
    evidence: str = ""  # redacted excerpt of the check evidence
    path: str = ""
    suggested_fix: str = ""  # taken over from a review finding that restated this failure

    @property
    def label(self) -> str:
        return f"{self.check_type}:{self.name}"

    def to_dict(self) -> dict[str, str]:
        return {
            "check_type": self.check_type,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "evidence": self.evidence,
            "path": self.path,
            "suggested_fix": self.suggested_fix,
        }


@dataclass(frozen=True)
class CorrectionFinding:
    severity: FindingSeverity
    summary: str
    path: str = ""
    evidence: str = ""
    suggested_fix: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity.value,
            "path": self.path,
            "summary": self.summary,
            "evidence": self.evidence,
            "suggested_fix": self.suggested_fix,
        }


@dataclass(frozen=True)
class CorrectionRequest:
    step_id: uuid.UUID
    attempt_id: uuid.UUID | None
    verifier_failures: tuple[VerifierFailure, ...] = ()
    review_findings: tuple[CorrectionFinding, ...] = ()
    required_changes: tuple[str, ...] = ()
    constraints: tuple[str, ...] = RUNTIME_RULES
    review_run_id: uuid.UUID | None = None
    verification_run_id: uuid.UUID | None = None
    review_error: str = ""  # fail-closed reason when the review could not be completed
    review_error_code: str = ""
    dropped_duplicates: int = 0  # review findings that only restated verifier facts
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def source(self) -> CorrectionSource:
        has_v, has_r = bool(self.verifier_failures), bool(self.review_findings)
        if has_v and has_r:
            return "verifier+review"
        if has_v:
            return "verifier"
        if has_r:
            return "review"
        return "runtime" if (self.review_error or self.required_changes) else "none"

    @property
    def is_empty(self) -> bool:
        """Nothing to correct (e.g. verifier passed and the review passed without findings)."""
        return not (self.verifier_failures or self.review_findings or self.required_changes or self.review_error)

    @property
    def blocking_findings(self) -> list[CorrectionFinding]:
        return [f for f in self.review_findings if f.blocking]

    def items(self) -> list[dict[str, str]]:
        """Flat evidence items for the coder context (``source``/``label``/``message``/``path``/``suggested_fix``)."""
        out: list[dict[str, str]] = []
        for v in self.verifier_failures:
            message = v.message or v.status
            if v.evidence:
                message += f" — evidence: {v.evidence}"
            out.append({"source": "verifier", "label": v.label, "message": message, "path": v.path, "suggested_fix": v.suggested_fix})
        for f in self.review_findings:
            message = f.summary + (f" — evidence: {f.evidence}" if f.evidence else "")
            out.append(
                {"source": "review", "label": f.severity.value, "message": message, "path": f.path, "suggested_fix": f.suggested_fix}
            )
        if self.review_error:
            out.append(
                {
                    "source": "runtime",
                    "label": self.review_error_code or "review_error",
                    "message": self.review_error,
                    "path": "",
                    "suggested_fix": "",
                }
            )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": CORRECTION_REQUEST_KIND,
            "version": CORRECTION_REQUEST_VERSION,
            "step_id": str(self.step_id),
            "attempt_id": str(self.attempt_id) if self.attempt_id else None,
            "source": self.source,
            "verifier_failures": [v.to_dict() for v in self.verifier_failures],
            "review_findings": [f.to_dict() for f in self.review_findings],
            "required_changes": list(self.required_changes),
            "constraints": list(self.constraints),
            "review_run_id": str(self.review_run_id) if self.review_run_id else None,
            "verification_run_id": str(self.verification_run_id) if self.verification_run_id else None,
            "review_error": self.review_error,
            "review_error_code": self.review_error_code,
            "dropped_duplicates": self.dropped_duplicates,
            "notes": list(self.notes),
            "items": self.items(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CorrectionRequest:
        """Inverse of ``to_dict`` (tolerant: unknown keys are ignored, malformed entries skipped)."""
        if data.get("kind", CORRECTION_REQUEST_KIND) != CORRECTION_REQUEST_KIND:
            raise ValueError(f"not a correction request: kind={data.get('kind')!r}")
        failures = tuple(
            VerifierFailure(
                check_type=str(d.get("check_type", "")),
                name=str(d.get("name", "")),
                status=str(d.get("status", "fail")),
                message=str(d.get("message", "")),
                evidence=str(d.get("evidence", "")),
                path=str(d.get("path", "")),
                suggested_fix=str(d.get("suggested_fix", "")),
            )
            for d in _dicts(data.get("verifier_failures"))
        )
        findings: list[CorrectionFinding] = []
        for d in _dicts(data.get("review_findings")):
            summary = str(d.get("summary", "")).strip()
            if not summary:
                continue
            try:
                severity = FindingSeverity(str(d.get("severity", "major")))
            except ValueError:
                severity = FindingSeverity.major  # fail-closed
            findings.append(
                CorrectionFinding(
                    severity=severity,
                    summary=summary,
                    path=str(d.get("path", "")),
                    evidence=str(d.get("evidence", "")),
                    suggested_fix=str(d.get("suggested_fix", "")),
                )
            )
        return cls(
            step_id=uuid.UUID(str(data["step_id"])),
            attempt_id=_uuid_or_none(data.get("attempt_id")),
            verifier_failures=failures,
            review_findings=tuple(findings),
            required_changes=tuple(str(c) for c in data.get("required_changes") or [] if str(c).strip()),
            constraints=tuple(str(c) for c in data.get("constraints") or [] if str(c).strip()) or RUNTIME_RULES,
            review_run_id=_uuid_or_none(data.get("review_run_id")),
            verification_run_id=_uuid_or_none(data.get("verification_run_id")),
            review_error=str(data.get("review_error") or ""),
            review_error_code=str(data.get("review_error_code") or ""),
            dropped_duplicates=int(data.get("dropped_duplicates") or 0),
            notes=tuple(str(n) for n in data.get("notes") or []),
        )

    def render(self, max_chars: int = 12_000) -> str:
        """Plain-text form for prompts, logs and the UI (sections in priority order, clipped at ``max_chars``)."""
        lines: list[str] = []
        if self.verifier_failures:
            lines.append("VERIFIER FAILURES (deterministic facts):")
            for v in self.verifier_failures:
                where = f" [{v.path}]" if v.path else ""
                lines.append(f"- {v.label} ({v.status}){where}: {v.message or v.status}")
                if v.evidence:
                    lines.append(f"  evidence: {v.evidence}")
                if v.suggested_fix:
                    lines.append(f"  suggested fix: {v.suggested_fix}")
        if self.review_findings:
            lines.append("REVIEW FINDINGS (blocker/major must be fixed):")
            for f in self.review_findings:
                where = f" [{f.path}]" if f.path else ""
                lines.append(f"- {f.severity.value}{where}: {f.summary}")
                if f.evidence:
                    lines.append(f"  evidence: {f.evidence}")
                if f.suggested_fix:
                    lines.append(f"  suggested fix: {f.suggested_fix}")
        if self.review_error:
            lines.append(f"REVIEW NOT COMPLETED: {self.review_error}")
        if self.required_changes:
            lines.append("REQUIRED CHANGES:")
            lines.extend(f"{i}. {c}" for i, c in enumerate(self.required_changes, start=1))
        if self.constraints:
            lines.append("CONSTRAINTS (still apply):")
            lines.extend(f"- {c}" for c in self.constraints)
        return clip("\n".join(lines), max_chars)


# ----------------------------------------------------------------------------------------------------- building
def build_correction_request(
    *,
    step_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    verification: VerificationReport | None = None,
    review: ReviewOutcome | ReviewContract | None = None,
    step: StepContract | None = None,
    verification_run_id: uuid.UUID | None = None,
    limits: CorrectionLimits | None = None,
) -> CorrectionRequest:
    """Build the deterministic, de-duplicated correction request for the next attempt of ``step_id``."""
    lim = limits or CorrectionLimits()
    notes: list[str] = []
    failures = _verifier_failures(verification, lim, notes)

    contract: ReviewContract | None
    review_run_id: uuid.UUID | None = None
    review_error = review_error_code = ""
    if isinstance(review, ReviewOutcome):
        contract = review.review
        review_run_id = review.review_run_id
        if review.status == "error" or review.fail_closed:
            review_error = clip(one_line(redact(strip_reasoning(review.reason))), lim.message_chars) or "review failed"
            review_error_code = review.error_code or ""
    else:
        contract = review

    findings = _review_findings(contract, lim)
    findings, failures, dropped = _dedupe_against_verifier(findings, failures, lim)
    if dropped:
        notes.append(f"{dropped} review finding(s) restated verifier failures and were merged into them")
    if len(findings) > lim.max_review_findings:
        notes.append(f"review findings capped at {lim.max_review_findings} of {len(findings)} (most severe kept)")
        findings = findings[: lim.max_review_findings]

    required = _required_changes(failures, findings, review_error_code, review_error, lim)
    constraints = _constraints(step, lim)
    return CorrectionRequest(
        step_id=step_id,
        attempt_id=attempt_id,
        verifier_failures=tuple(failures),
        review_findings=tuple(findings),
        required_changes=tuple(required),
        constraints=tuple(constraints),
        review_run_id=review_run_id,
        verification_run_id=verification_run_id,
        review_error=review_error,
        review_error_code=review_error_code,
        dropped_duplicates=dropped,
        notes=tuple(notes),
    )


def _verifier_failures(report: VerificationReport | None, lim: CorrectionLimits, notes: list[str]) -> list[VerifierFailure]:
    if report is None:
        return []
    out: list[VerifierFailure] = []
    seen: set[tuple[str, str, str, str]] = set()
    for check in report.failures:  # blocking fail/error checks only (advisory checks never block)
        failure = _failure(check, lim)
        key = (failure.check_type, failure.name, failure.path, _norm(failure.message))
        if key in seen:
            continue
        seen.add(key)
        out.append(failure)
    if len(out) > lim.max_verifier_failures:
        notes.append(f"verifier failures capped at {lim.max_verifier_failures} of {len(out)}")
        out = out[: lim.max_verifier_failures]
    if not report.passed and not out:
        # the report failed without a blocking failed check (inconsistent report): keep the fact anyway
        out.append(
            VerifierFailure(
                check_type="verifier",
                name="report",
                status="fail",
                message=clip(one_line(redact(report.summary)), lim.message_chars) or "verification did not pass",
            )
        )
    return out


def _failure(check: VerificationCheck, lim: CorrectionLimits) -> VerifierFailure:
    return VerifierFailure(
        check_type=one_line(check.check_type),
        name=clip(one_line(check.name), 200),
        status=check.status,
        message=clip(one_line(redact(check.message)), lim.message_chars),
        evidence=evidence_excerpt(check.evidence, lim.evidence_chars),
        path=_evidence_path(check.evidence),
    )


def evidence_excerpt(evidence: Mapping[str, Any], limit: int) -> str:
    """Most informative part of a check's evidence first (stderr/stdout/output/…), then the rest as compact JSON."""
    if not evidence or limit <= 0:
        return ""
    parts: list[str] = []
    for key in _EVIDENCE_TEXT_KEYS:
        value = evidence.get(key)
        if value in (None, "", [], {}):
            continue
        text = value if isinstance(value, str) else compact_json(value, limit)
        parts.append(f"{key}: {clip(redact(text.strip()), limit)}")
    rest = {k: v for k, v in evidence.items() if k not in _EVIDENCE_TEXT_KEYS and v not in (None, "", [], {})}
    if rest:
        parts.append(compact_json(rest, limit))
    return clip(redact(" | ".join(parts)), limit)


def _evidence_path(evidence: Mapping[str, Any]) -> str:
    for key in _EVIDENCE_PATH_KEYS:
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            return _canonical(value)
    for key in _EVIDENCE_PATHS_KEYS:
        value = evidence.get(key)
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            return _canonical(value[0])
    return ""


def _review_findings(review: ReviewContract | None, lim: CorrectionLimits) -> list[CorrectionFinding]:
    if review is None:
        return []
    merged: dict[tuple[str, str], tuple[int, CorrectionFinding]] = {}
    for idx, f in enumerate(review.findings):
        finding = _finding(f, lim)
        key = (finding.path, _norm(finding.summary))
        if key in merged:
            first, existing = merged[key]
            if SEVERITY_RANK[finding.severity] < SEVERITY_RANK[existing.severity]:
                existing = CorrectionFinding(
                    severity=finding.severity,
                    summary=existing.summary,
                    path=existing.path,
                    evidence=existing.evidence or finding.evidence,
                    suggested_fix=existing.suggested_fix or finding.suggested_fix,
                )
            merged[key] = (first, existing)
            continue
        merged[key] = (idx, finding)
    ordered = sorted(merged.values(), key=lambda t: (SEVERITY_RANK[t[1].severity], t[0]))
    return [f for _, f in ordered]


def _finding(f: ReviewFinding, lim: CorrectionLimits) -> CorrectionFinding:
    return CorrectionFinding(
        severity=f.severity,
        summary=clip(one_line(redact(strip_reasoning(f.summary))), lim.summary_chars),
        path=_canonical(f.path) if f.path else "",
        evidence=clip(redact(strip_reasoning(f.evidence)).strip(), lim.evidence_chars),
        suggested_fix=clip(one_line(redact(strip_reasoning(f.suggested_fix))), lim.fix_chars),
    )


def _dedupe_against_verifier(
    findings: list[CorrectionFinding], failures: list[VerifierFailure], lim: CorrectionLimits
) -> tuple[list[CorrectionFinding], list[VerifierFailure], int]:
    """Drop review findings that restate a verifier failure; keep their suggested fix on that failure."""
    if not failures:
        return findings, failures, 0
    updated = list(failures)
    kept: list[CorrectionFinding] = []
    dropped = 0
    for finding in findings:
        match = next((i for i, v in enumerate(updated) if _restates(finding, v, lim)), None)
        if match is None:
            kept.append(finding)
            continue
        dropped += 1
        target = updated[match]
        if finding.suggested_fix and not target.suggested_fix:
            updated[match] = VerifierFailure(
                check_type=target.check_type,
                name=target.name,
                status=target.status,
                message=target.message,
                evidence=target.evidence,
                path=target.path or finding.path,
                suggested_fix=finding.suggested_fix,
            )
    return kept, updated, dropped


def _restates(finding: CorrectionFinding, failure: VerifierFailure, lim: CorrectionLimits) -> bool:
    """True when ``finding`` describes the same problem as the verifier ``failure`` (paths must be compatible)."""
    if finding.path and failure.path and finding.path != failure.path:
        return False
    f_text = _norm(f"{finding.summary} {finding.evidence}")
    v_msg = _norm(failure.message)
    label = _norm(failure.label)
    if label and len(label) >= 5 and label in f_text:
        return True
    if len(v_msg) >= lim.min_contained_chars and v_msg in f_text:
        return True
    summary = _norm(finding.summary)
    if len(summary) >= lim.min_contained_chars and summary in _norm(f"{failure.message} {failure.evidence}"):
        return True
    f_tokens = _tokens(f"{finding.summary} {finding.evidence}")
    v_tokens = _tokens(f"{failure.check_type} {failure.name} {failure.message} {failure.evidence}")
    if len(f_tokens) < 3 or len(v_tokens) < 3:
        return False
    overlap = len(f_tokens & v_tokens) / min(len(f_tokens), len(v_tokens))
    return overlap >= lim.duplicate_overlap


def _required_changes(
    failures: Sequence[VerifierFailure],
    findings: Sequence[CorrectionFinding],
    review_error_code: str,
    review_error: str,
    lim: CorrectionLimits,
) -> list[str]:
    changes: list[str] = []
    if review_error_code == EMPTY_DIFF:
        changes.append("The previous attempt produced no changes. Implement the step goal so that the acceptance criteria are met.")
    for v in failures:
        where = f" in {v.path}" if v.path else ""
        base = f"Make the failing {v.check_type} check '{v.name}'{where} pass"
        detail = v.suggested_fix or v.message
        changes.append(clip(f"{base}: {detail}" if detail else base, lim.change_chars))
    for f in findings:
        if not f.blocking:
            continue
        where = f"{f.path}: " if f.path else ""
        text = f.suggested_fix or f"resolve '{f.summary}'"
        changes.append(clip(f"[{f.severity.value}] {where}{text}", lim.change_chars))
    if review_error and review_error_code != EMPTY_DIFF and not failures and not findings:
        changes.append(
            clip(f"Review could not be completed ({review_error_code or 'error'}); re-check the change: {review_error}", lim.change_chars)
        )
    out = list(_unique(changes))
    return out[: lim.max_required_changes]


def _constraints(step: StepContract | None, lim: CorrectionLimits) -> list[str]:
    items: list[str] = []
    if step is not None:
        items.extend(clip(one_line(redact(c)), lim.message_chars) for c in step.constraints if c.strip())
        if step.scope is not None:
            items.append(_scope_line(step.scope))
    items.extend(RUNTIME_RULES)
    return list(_unique(items))[: max(lim.max_constraints, len(RUNTIME_RULES))]


def _scope_line(scope: ScopeContract) -> str:
    def fmt(values: Sequence[str]) -> str:
        shown = ", ".join(values[:15])
        return (shown + (f" (+{len(values) - 15} more)" if len(values) > 15 else "")) or "(none)"

    return clip(
        f"Scope: target_paths={fmt(scope.target_paths)}; allowed_new_paths={fmt(scope.allowed_new_paths)}; "
        f"forbidden_paths={fmt(scope.forbidden_paths)}",
        1_200,
    )


# ----------------------------------------------------------------------------------------------------- helpers
def _norm(text: str) -> str:
    return one_line(text).casefold()


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.casefold()) if t not in _STOPWORDS}


def _canonical(path: str) -> str:
    raw = path.strip().replace("\\", "/")
    try:
        return normalise_path(raw)
    except ValueError:
        return raw


def _unique(values: Iterable[str]) -> Iterable[str]:
    seen: set[str] = set()
    for v in values:
        key = _norm(v)
        if key and key not in seen:
            seen.add(key)
            yield v


def _dicts(value: object) -> list[Mapping[str, Any]]:
    return [d for d in value if isinstance(d, Mapping)] if isinstance(value, list) else []


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None
