"""Severity normalisation of heavy-review output (22.4).

Two stages, both deterministic:

1. ``normalise_raw_review`` runs *before* schema validation (as a wrap validator of ``ReviewDraft``, the schema
   class handed to ``ChatModel.structured``). It maps severity/verdict synonyms and common field aliases onto the
   ``ReviewContract`` vocabulary, clips over-long texts and drops unknown keys (e.g. a ``reasoning`` field – model
   reasoning is never stored). Unknown severities become ``major`` (fail-closed: an unclassifiable finding blocks a
   pass). Unknown verdicts are left alone so validation fails and the gateway repairs the answer.
2. ``normalise_findings`` runs on the validated contract: canonical repository paths, ``path:line`` split, de-
   duplication (same path + summary keeps the highest severity) and stable ordering blocker → major → minor.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import ConfigDict, PrivateAttr, ValidatorFunctionWrapHandler, model_validator

from hermclaw.contracts.common import FindingSeverity
from hermclaw.contracts.review import ReviewContract, ReviewFinding
from hermclaw.contracts.scope import normalise_path
from hermclaw.review.text import clip, one_line
from hermclaw.review.types import SEVERITY_RANK

MAX_FINDINGS = 50
_LIMITS = {"path": 500, "summary": 2000, "evidence": 4000, "suggested_fix": 4000}
_TOP_SUMMARY_LIMIT = 4000

_SEVERITY_SYNONYMS: dict[str, FindingSeverity] = {
    # blocker
    "blocker": FindingSeverity.blocker,
    "blocking": FindingSeverity.blocker,
    "critical": FindingSeverity.blocker,
    "crit": FindingSeverity.blocker,
    "fatal": FindingSeverity.blocker,
    "severe": FindingSeverity.blocker,
    "showstopper": FindingSeverity.blocker,
    "show_stopper": FindingSeverity.blocker,
    "must_fix": FindingSeverity.blocker,
    "security": FindingSeverity.blocker,
    "p0": FindingSeverity.blocker,
    # major
    "major": FindingSeverity.major,
    "high": FindingSeverity.major,
    "medium": FindingSeverity.major,
    "moderate": FindingSeverity.major,
    "important": FindingSeverity.major,
    "significant": FindingSeverity.major,
    "error": FindingSeverity.major,
    "warning": FindingSeverity.major,
    "warn": FindingSeverity.major,
    "bug": FindingSeverity.major,
    "p1": FindingSeverity.major,
    "p2": FindingSeverity.major,
    # minor
    "minor": FindingSeverity.minor,
    "low": FindingSeverity.minor,
    "trivial": FindingSeverity.minor,
    "nit": FindingSeverity.minor,
    "nitpick": FindingSeverity.minor,
    "info": FindingSeverity.minor,
    "informational": FindingSeverity.minor,
    "note": FindingSeverity.minor,
    "suggestion": FindingSeverity.minor,
    "style": FindingSeverity.minor,
    "cosmetic": FindingSeverity.minor,
    "optional": FindingSeverity.minor,
    "p3": FindingSeverity.minor,
}
_PASS_VERDICTS = {"pass", "passed", "approve", "approved", "accept", "accepted"}
_FIX_VERDICTS = {
    "fix_required",
    "fixrequired",
    "fix",
    "fail",
    "failed",
    "reject",
    "rejected",
    "changes_requested",
    "request_changes",
    "needs_fix",
    "needs_fixes",
    "needs_changes",
    "needs_work",
    "block",
    "blocked",
}
_FIELD_ALIASES: dict[str, str] = {
    "severity": "severity",
    "level": "severity",
    "severity_level": "severity",
    "priority": "severity",
    "path": "path",
    "file": "path",
    "file_path": "path",
    "filepath": "path",
    "filename": "path",
    "location": "path",
    "summary": "summary",
    "message": "summary",
    "description": "summary",
    "title": "summary",
    "issue": "summary",
    "problem": "summary",
    "evidence": "evidence",
    "details": "evidence",
    "excerpt": "evidence",
    "quote": "evidence",
    "suggested_fix": "suggested_fix",
    "suggestedfix": "suggested_fix",
    "fix": "suggested_fix",
    "suggestion": "suggested_fix",
    "recommendation": "suggested_fix",
    "remediation": "suggested_fix",
}
_LINE_KEYS = ("line", "lines", "line_number", "start_line")
_VERDICT_KEYS = ("verdict", "decision")
_FINDINGS_KEYS = ("findings", "issues", "problems")
_SUMMARY_KEYS = ("summary", "overall", "comment")
_PATH_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?:[-:]\d+)?$")


def _token(value: object) -> str:
    return re.sub(r"[\s\-/]+", "_", str(value).strip().lower()).strip("_")


def normalise_severity(value: object) -> tuple[FindingSeverity, bool]:
    """Map a raw severity onto minor|major|blocker. Returns (severity, changed). Unknown → major (fail-closed)."""
    if isinstance(value, FindingSeverity):
        return value, False
    tok = _token(value) if value is not None else ""
    if tok in _SEVERITY_SYNONYMS:
        sev = _SEVERITY_SYNONYMS[tok]
        return sev, not (isinstance(value, str) and value == sev.value)
    return FindingSeverity.major, True


def normalise_verdict(value: object) -> tuple[object, bool]:
    """Map verdict synonyms onto pass|fix_required. Unknown values are returned unchanged (validation fails)."""
    if not isinstance(value, str):
        return value, False
    tok = _token(value)
    if tok in _PASS_VERDICTS:
        return "pass", value != "pass"
    if tok in _FIX_VERDICTS:
        return "fix_required", value != "fix_required"
    return value, False


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "; ".join(_text(v) for v in value if v is not None)
    return str(value)


def _first(data: dict[str, Any], keys: Iterable[str]) -> tuple[str | None, Any]:
    for key in keys:
        if key in data:
            return key, data[key]
    return None, None


def _normalise_raw_finding(raw: object, idx: int, notes: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        notes.append(f"finding[{idx}]: non-object finding converted (severity major)")
        return {"severity": FindingSeverity.major.value, "summary": clip(_text(raw), _LIMITS["summary"])}
    out: dict[str, Any] = {}
    line: str = ""
    for key, value in raw.items():
        canon = _FIELD_ALIASES.get(_token(key))
        if canon is None:
            if _token(key) in _LINE_KEYS and value not in (None, ""):
                line = _text(value)
            else:
                notes.append(f"finding[{idx}]: dropped unknown field '{clip(str(key), 40)}'")
            continue
        if canon in out:  # first occurrence wins (e.g. 'summary' before 'description')
            continue
        out[canon] = value
    sev, changed = normalise_severity(out.get("severity"))
    if changed:
        notes.append(f"finding[{idx}]: severity {clip(_text(out.get('severity')), 30)!r} -> {sev.value}")
    out["severity"] = sev.value
    for name, limit in _LIMITS.items():
        if name == "summary" and name not in out:
            continue  # a finding without summary stays invalid → repair
        text = _text(out.get(name))
        if len(text) > limit:
            notes.append(f"finding[{idx}]: {name} clipped")
        out[name] = clip(text, limit)
    if line and out.get("evidence", "").find(line) == -1:
        out["evidence"] = clip(f"line {line}: {out.get('evidence', '')}".rstrip(": "), _LIMITS["evidence"])
    return out


def normalise_raw_review(data: Any) -> tuple[Any, list[str]]:
    """Pre-validation normalisation of a parsed model answer. Non-objects are returned unchanged."""
    if not isinstance(data, dict):
        return data, []
    notes: list[str] = []
    out: dict[str, Any] = {}
    vkey, verdict = _first(data, _VERDICT_KEYS)
    if vkey is not None:
        out["verdict"], changed = normalise_verdict(verdict)
        if changed or vkey != "verdict":
            notes.append(f"verdict {clip(_text(verdict), 30)!r} -> {out['verdict']!r}")
    fkey, findings = _first(data, _FINDINGS_KEYS)
    if findings is None:
        findings = []
    elif isinstance(findings, dict) or not isinstance(findings, list):
        findings = [findings]
    cleaned = [_normalise_raw_finding(f, i, notes) for i, f in enumerate(findings)]
    if len(cleaned) > MAX_FINDINGS:
        cleaned = sorted(cleaned, key=lambda f: SEVERITY_RANK[FindingSeverity(f["severity"])])[:MAX_FINDINGS]
        notes.append(f"findings capped at {MAX_FINDINGS} (most severe kept) of {len(findings)}")
    out["findings"] = cleaned
    skey, summary = _first(data, _SUMMARY_KEYS)
    out["summary"] = clip(_text(summary), _TOP_SUMMARY_LIMIT)
    used = {vkey, fkey, skey}
    for key in data:
        if key not in used:
            notes.append(f"dropped unknown field '{clip(str(key), 40)}'")
    return out, notes


# Schema handed to ``ChatModel.structured``: the JSON schema is identical to ``ReviewContract`` (same title, same
# fields, no description – hence no docstring) but parsing is tolerant via ``normalise_raw_review``.
# Convert with ``to_contract()``.
class ReviewDraft(ReviewContract):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True, title="ReviewContract")

    _notes: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="wrap")
    @classmethod
    def _normalise(cls, data: Any, handler: ValidatorFunctionWrapHandler) -> ReviewDraft:
        if isinstance(data, ReviewDraft):
            return data
        cleaned, notes = normalise_raw_review(data)
        model = handler(cleaned)
        assert isinstance(model, ReviewDraft)
        model._notes = notes
        return model

    @property
    def normalisation_notes(self) -> list[str]:
        return list(self._notes)

    def to_contract(self) -> ReviewContract:
        return ReviewContract.model_validate(self.model_dump(mode="json"))


def _strip_dot(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path


def _canonical_path(raw: str, changed: set[str]) -> tuple[str, str]:
    """(path, line) – strips diff prefixes and ``./``, splits ``path:line``. Non-canonical paths stay as given."""
    p = raw.strip().replace("\\", "/")
    line = ""
    m = _PATH_LINE.match(p)
    if m:
        p, line = m.group("path"), m.group("line")
    for prefix in ("a/", "b/"):
        if p.startswith(prefix) and _strip_dot(p[len(prefix) :]) in changed:
            p = p[len(prefix) :]
    try:
        p = normalise_path(p)
    except ValueError:
        return raw.strip(), line
    return p, line


def normalise_findings(review: ReviewContract, changed_files: Sequence[str] = ()) -> tuple[ReviewContract, list[str]]:
    """Post-validation normalisation: canonical paths, de-duplication, ordering blocker → major → minor."""
    notes: list[str] = []
    changed = {c.strip() for c in changed_files}
    merged: dict[tuple[str, str], tuple[int, ReviewFinding]] = {}
    outside = 0
    for idx, f in enumerate(review.findings):
        path, line = _canonical_path(f.path, changed) if f.path else ("", "")
        evidence = f.evidence
        if line and line not in evidence:
            evidence = clip(f"line {line}: {evidence}".rstrip(": "), _LIMITS["evidence"])
        finding = f.model_copy(update={"path": path, "evidence": evidence}) if (path != f.path or evidence != f.evidence) else f
        if path and changed and path not in changed:
            outside += 1
        key = (path, one_line(finding.summary).casefold())
        if key in merged:
            first_idx, existing = merged[key]
            notes.append(f"duplicate finding merged: {clip(finding.summary, 60)!r}")
            if SEVERITY_RANK[finding.severity] < SEVERITY_RANK[existing.severity]:
                merged[key] = (first_idx, existing.model_copy(update={"severity": finding.severity}))
            continue
        merged[key] = (idx, finding)
    if outside:
        notes.append(f"{outside} finding(s) cite paths outside the changed files")
    ordered = sorted(merged.values(), key=lambda t: (SEVERITY_RANK[t[1].severity], t[0]))
    findings = [f for _, f in ordered]
    if findings == list(review.findings):
        return review, notes
    return review.model_copy(update={"findings": findings}), notes
