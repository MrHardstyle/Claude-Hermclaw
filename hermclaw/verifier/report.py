"""Verification report (P21 21.14): verdict, run status and a human/correction-friendly summary."""

from __future__ import annotations

from collections.abc import Sequence

from hermclaw.contracts.verification import VerificationCheck, VerificationReport
from hermclaw.verifier.text import pg_safe

MAX_SUMMARY_FAILURES = 8


def build_report(checks: Sequence[VerificationCheck], changed_files: Sequence[str]) -> VerificationReport:
    """``passed`` iff no *blocking* check failed or errored; skips never fail a run."""
    ordered = list(checks)
    failures = [c for c in ordered if c.status in ("fail", "error") and c.blocking]
    counts = {s: sum(1 for c in ordered if c.status == s) for s in ("pass", "fail", "error", "skip")}
    tally = f"{len(ordered)} checks: {counts['pass']} passed, {counts['fail']} failed, {counts['error']} errors, {counts['skip']} skipped"
    if not failures:
        warn = [c for c in ordered if c.status in ("fail", "error")]
        extra = f"; {len(warn)} non-blocking problem(s)" if warn else ""
        summary = f"Verification passed ({tally}{extra}); {len(changed_files)} changed file(s)."
    else:
        lines = [f"Verification failed: {len(failures)} blocking problem(s) ({tally})."]
        for c in failures[:MAX_SUMMARY_FAILURES]:
            lines.append(f"- [{c.check_type}] {c.name}: {c.message or c.status}"[:600])
        if len(failures) > MAX_SUMMARY_FAILURES:
            lines.append(f"- … {len(failures) - MAX_SUMMARY_FAILURES} more")
        summary = "\n".join(lines)
    paths = sorted({pg_safe(p) for p in changed_files})
    return VerificationReport(passed=not failures, checks=ordered, changed_files=paths, summary=summary)


def run_status(report: VerificationReport) -> str:
    """``passed`` | ``failed`` | ``error`` (only errors, i.e. the verifier could not judge the change)."""
    if report.passed:
        return "passed"
    failures = report.failures
    if failures and all(c.status == "error" for c in failures):
        return "error"
    return "failed"
