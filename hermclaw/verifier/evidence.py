"""Evaluation of the step's acceptance evidence (P21 21.9–21.13 + schema/artifact/scope/security).

Every criterion yields exactly one check named ``acceptance[<index>]:<type>`` so correction knows which criterion
failed. Nothing here is task-specific: criteria are interpreted only through their generic fields.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermclaw.contracts.acceptance import (
    AbsenceEvidence,
    AcceptanceCriterion,
    ArtifactEvidence,
    CommandEvidence,
    DiffEvidence,
    PresenceEvidence,
    SchemaEvidence,
    ScopeEvidence,
    SecurityEvidence,
    TestEvidence,
)
from hermclaw.contracts.scope import normalise_path
from hermclaw.contracts.verification import CheckStatus, VerificationCheck
from hermclaw.scope.guard import path_matches
from hermclaw.tools.testparse import Framework, parse_counts, summarise
from hermclaw.tools.workspace import has_git_segment, regex_is_risky
from hermclaw.verifier.changes import matching, read_text
from hermclaw.verifier.commands import TestRecord
from hermclaw.verifier.context import VerifyContext
from hermclaw.verifier.schema import SchemaParseError, parse_document, validate
from hermclaw.verifier.text import pg_safe

MAX_SEARCH_FILES = 5000
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_TOTAL_BYTES = 256 * 1024 * 1024
MAX_SAMPLES = 20
MAX_SCHEMA_FILE_BYTES = 5 * 1024 * 1024
_GLOB_CHARS = frozenset("*?[")

FRAMEWORK_MAP: dict[str, Framework | None] = {
    "pytest": "pytest",
    "unittest": "unittest",
    "npm": None,  # jest / vitest / mocha – detected from the output
    "phpunit": "phpunit",
    "go": "go",
    "cargo": "cargo",
    "generic": None,  # any known runner summary, otherwise exit code only
}


def label(index: int, criterion: AcceptanceCriterion) -> str:
    desc = f" – {criterion.description}" if criterion.description else ""
    return f"acceptance[{index}]:{criterion.type}{desc}"


def categorise_test(ev: TestEvidence) -> str:
    """unit | integration | contract – from the evidence wording (generic, used for reporting only)."""
    text = f"{ev.description} {ev.command}".lower()
    if "integration" in text or "e2e" in text or "end-to-end" in text:
        return "integration"
    if "contract" in text:
        return "contract"
    return "unit"


# ------------------------------------------------------------------------------------------- path globs / search
def clean_glob(raw: str) -> str | None:
    g = raw.strip().replace("\\", "/")
    while g.startswith("./"):
        g = g[2:]
    if not g or "\x00" in g or g.startswith("/") or ".." in g.split("/") or has_git_segment(g.rstrip("/")):
        return None
    return g


async def candidates(ctx: VerifyContext, glob: str) -> list[str]:
    """Existing workspace paths matching ``glob``. A literal path is also found when it is git-ignored."""
    files = await ctx.files()
    if any(ch in _GLOB_CHARS for ch in glob):
        return [f for f in files if path_matches(f, glob)]
    rel = glob.rstrip("/")
    try:
        rel = normalise_path(rel)
    except ValueError:
        return []
    return await asyncio.to_thread(_literal_candidates, ctx.root / rel, rel, files)


def _literal_candidates(target: Path, rel: str, files: Sequence[str]) -> list[str]:
    if os.path.lexists(target) and not os.path.isdir(target):
        return [rel]
    if os.path.isdir(target) and not os.path.islink(target):
        below = [f for f in files if f.startswith(rel + "/")]
        return below or [rel + "/"]
    return []


@dataclass
class SearchResult:
    total: int
    samples: list[dict[str, Any]]
    files_searched: int
    files_matched: list[str]
    skipped: dict[str, str]


def _search(root_files: Sequence[tuple[str, str | None, str]], regex: re.Pattern[str]) -> SearchResult:
    total, searched = 0, 0
    samples: list[dict[str, Any]] = []
    matched: list[str] = []
    skipped: dict[str, str] = {}
    for rel, text, reason in root_files:
        if text is None:
            skipped[rel] = reason
            continue
        searched += 1
        count = 0
        line, last = 1, 0
        for m in regex.finditer(text):
            if m.start() == m.end():
                continue  # empty matches do not count
            count += 1
            if len(samples) < MAX_SAMPLES:
                line += text.count("\n", last, m.start())
                last = m.start()
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.start())
                snippet = text[start : end if end != -1 else len(text)].strip()
                samples.append({"path": rel, "line": line, "snippet": snippet[:200]})
        if count:
            matched.append(rel)
            total += count
    return SearchResult(total, samples, searched, matched, skipped)


async def search(ctx: VerifyContext, paths: Sequence[str], pattern: re.Pattern[str]) -> SearchResult:
    root = ctx.root

    def load_and_search() -> SearchResult:
        loaded: list[tuple[str, str | None, str]] = []
        budget = MAX_SEARCH_TOTAL_BYTES
        for rel in paths[:MAX_SEARCH_FILES]:
            if rel.endswith("/"):
                continue
            if budget <= 0:
                loaded.append((rel, None, f"not searched: total search budget of {MAX_SEARCH_TOTAL_BYTES} bytes exhausted"))
                continue
            text, reason = read_text(root, rel, limit=MAX_SEARCH_FILE_BYTES)
            budget -= len(text) if text is not None else 0
            loaded.append((rel, text, reason))
        return _search(loaded, pattern)

    result = await asyncio.to_thread(load_and_search)
    if len(paths) > MAX_SEARCH_FILES:
        result.skipped["…"] = f"only the first {MAX_SEARCH_FILES} of {len(paths)} files were searched"
    return result


def compile_pattern(pattern: str) -> tuple[re.Pattern[str] | None, str]:
    if regex_is_risky(pattern):
        return None, f"pattern {pattern!r} was rejected: nested quantifiers risk catastrophic backtracking"
    try:
        return re.compile(pattern, re.MULTILINE), ""
    except re.error as exc:
        return None, f"invalid regular expression {pattern!r}: {exc}"


# ------------------------------------------------------------------------------------------- presence / absence
async def presence(ctx: VerifyContext, index: int, ev: PresenceEvidence) -> VerificationCheck:
    name = label(index, ev)
    glob = clean_glob(ev.path_glob)
    if glob is None:
        return ctx.check("presence", name, "error", f"invalid path glob {ev.path_glob!r}", {"path_glob": ev.path_glob})
    found = await candidates(ctx, glob)
    base: dict[str, Any] = {"path_glob": glob, "pattern": ev.pattern, "min_matches": ev.min_matches}
    if ev.pattern is None:
        status: CheckStatus = "pass" if len(found) >= ev.min_matches else "fail"
        msg = f"{len(found)} path(s) match {glob!r}" + ("" if status == "pass" else f"; at least {ev.min_matches} required")
        return ctx.check("presence", name, status, msg, {**base, "matching_paths": found[:MAX_SAMPLES], "count": len(found)})
    regex, problem = compile_pattern(ev.pattern)
    if regex is None:
        return ctx.check("presence", name, "error", problem, base)
    res = await search(ctx, found, regex)
    status = "pass" if res.total >= ev.min_matches else "fail"
    msg = f"{res.total} match(es) of {ev.pattern!r} in {len(res.files_matched)} of {res.files_searched} file(s) matching {glob!r}"
    if status == "fail":
        msg += f"; at least {ev.min_matches} required" + ("" if found else " (no file matches the glob)")
    evidence = {**base, "matches": res.total, "samples": res.samples, "files_searched": res.files_searched, "skipped": res.skipped}
    if res.samples:
        evidence["path"] = res.samples[0]["path"]
    return ctx.check("presence", name, status, msg, evidence)


async def absence(ctx: VerifyContext, index: int, ev: AbsenceEvidence) -> VerificationCheck:
    """First-class removal evidence: the path is gone, or the pattern has 0 matches over the glob."""
    name = label(index, ev)
    glob = clean_glob(ev.path_glob)
    if glob is None:
        return ctx.check("absence", name, "error", f"invalid path glob {ev.path_glob!r}", {"path_glob": ev.path_glob})
    found = await candidates(ctx, glob)
    base: dict[str, Any] = {"path_glob": glob, "pattern": ev.pattern, "expected_matches": 0}
    if ev.pattern is None:
        if found:
            return ctx.check(
                "absence",
                name,
                "fail",
                f"{len(found)} path(s) matching {glob!r} still exist: {', '.join(found[:5])}",
                {**base, "existing_paths": found, "count": len(found), "path": found[0]},
            )
        return ctx.check("absence", name, "pass", f"no path matches {glob!r}", {**base, "count": 0})
    regex, problem = compile_pattern(ev.pattern)
    if regex is None:
        return ctx.check("absence", name, "error", problem, base)
    res = await search(ctx, found, regex)
    evidence = {
        **base,
        "matches": res.total,
        "matching_files": res.files_matched,
        "samples": res.samples,
        "files_searched": res.files_searched,
        "skipped": res.skipped,
    }
    if res.total:
        evidence["path"] = res.files_matched[0]
        where = ", ".join(f"{s['path']}:{s['line']}" for s in res.samples[:5])
        return ctx.check("absence", name, "fail", f"{res.total} match(es) of {ev.pattern!r} remain ({where})", evidence)
    return ctx.check("absence", name, "pass", f"0 matches of {ev.pattern!r} in {res.files_searched} file(s) matching {glob!r}", evidence)


# ------------------------------------------------------------------------------------------- diff
def diff_evidence(ctx: VerifyContext, index: int, ev: DiffEvidence) -> VerificationCheck:
    name = label(index, ev)
    paths = ctx.changes.paths
    problems: list[str] = []
    missing = [g for g in ev.must_change if not matching(paths, [g])]
    if missing:
        problems.append(f"expected changes matching {missing} but none were found")
    touched = matching(paths, ev.must_not_change)
    if touched:
        problems.append(f"files that must not change were changed: {touched[:10]}")
    if ev.max_changed_files is not None and len(paths) > ev.max_changed_files:
        problems.append(f"{len(paths)} files changed, at most {ev.max_changed_files} allowed")
    if not paths and not ev.allow_empty:
        problems.append("the change is empty")
    evidence: dict[str, Any] = {
        "changed_files": paths,
        "changed_count": len(paths),
        "must_change": ev.must_change,
        "must_not_change": ev.must_not_change,
        "missing": missing,
        "touched_forbidden": touched,
        "max_changed_files": ev.max_changed_files,
        "allow_empty": ev.allow_empty,
    }
    if touched:
        evidence["path"] = touched[0]
    if problems:
        return ctx.check("diff", name, "fail", "; ".join(problems), evidence)
    return ctx.check("diff", name, "pass", f"diff matches the expectation ({len(paths)} changed file(s))", evidence)


# ------------------------------------------------------------------------------------------- command
async def command_evidence(ctx: VerifyContext, index: int, ev: CommandEvidence) -> VerificationCheck:
    name = label(index, ev)
    network = ev.network and ctx.step.network
    base: dict[str, Any] = {
        "command": ev.command,
        "expect_exit_code": ev.expect_exit_code,
        "stdout_pattern": ev.stdout_pattern,
        "timeout_seconds": ev.timeout_seconds,
        "network": network,
    }
    if ev.network and not ctx.step.network:
        base["note"] = "network requested by the evidence but not permitted for this step: ran without network"
    regex: re.Pattern[str] | None = None
    if ev.stdout_pattern:
        regex, problem = compile_pattern(ev.stdout_pattern)
        if regex is None:
            return ctx.check("command", name, "error", problem, base)
    out = await ctx.runner.run(ev.command, purpose="verifier", timeout_seconds=ev.timeout_seconds, network=network)
    evidence = {**base, "exit_code": out.exit_code, "duration_ms": out.duration_ms, "output": ctx.runner.clip(out.output, 3000)}
    if out.failed_to_run:
        return ctx.check("command", name, "error", f"command could not be executed: {out.problem or 'executor error'}", evidence)
    if out.timed_out:
        return ctx.check("command", name, "fail", f"command timed out after {ev.timeout_seconds}s", evidence)
    problems = []
    if out.exit_code != ev.expect_exit_code:
        problems.append(f"exit code {out.exit_code}, expected {ev.expect_exit_code}")
    if regex is not None:
        matched = regex.search(out.stdout) is not None
        evidence["stdout_matched"] = matched
        if not matched:
            problems.append(f"stdout does not match {ev.stdout_pattern!r}")
    if problems:
        return ctx.check("command", name, "fail", "; ".join(problems), evidence)
    return ctx.check("command", name, "pass", f"exit code {out.exit_code} as expected", evidence)


# ------------------------------------------------------------------------------------------- tests
@dataclass
class TestOutcome:
    check: VerificationCheck
    executed: bool | None  # tests demonstrably ran (None: runner output not parseable)
    ok: bool

    __test__ = False


async def run_test_evidence(ctx: VerifyContext, index: int, ev: TestEvidence) -> TestOutcome:
    name = label(index, ev)
    category = categorise_test(ev)
    fw = FRAMEWORK_MAP.get(ev.framework)
    out = await ctx.runner.run(ev.command, purpose="test", timeout_seconds=ev.timeout_seconds, network=ctx.step.network)
    output = out.output
    counts = parse_counts(output, fw) if not out.failed_to_run else None
    summary = summarise(output, out.exit_code, timed_out=out.timed_out, framework=fw)
    evidence: dict[str, Any] = {
        "command": ev.command,
        "framework": ev.framework,
        "detected_framework": counts.framework if counts else "generic",
        "category": category,
        "exit_code": out.exit_code,
        "passed": summary.passed,
        "failed": summary.failed,
        "errors": summary.errors,
        "skipped": summary.skipped,
        "min_passed": ev.min_passed,
        "counts_verified": counts is not None,
        "duration_ms": out.duration_ms,
        "output": ctx.runner.clip(output, 4000),
    }
    record_status = summary.status
    if out.failed_to_run:
        check = ctx.check(category, name, "error", f"test command could not be executed: {out.problem or 'executor error'}", evidence)
        result = TestOutcome(check, None, False)
        record_status = "error"
    elif out.timed_out:
        result = TestOutcome(ctx.check(category, name, "fail", f"tests timed out after {ev.timeout_seconds}s", evidence), None, False)
    elif counts is not None:
        problems = []
        if out.exit_code != 0:
            problems.append(f"exit code {out.exit_code}")
        if counts.failed or counts.errors:
            problems.append(f"{counts.failed} failed, {counts.errors} errors")
        if counts.passed < ev.min_passed:
            problems.append(f"{counts.passed} passed, at least {ev.min_passed} required")
        executed = counts.total > 0
        if problems:
            result = TestOutcome(ctx.check(category, name, "fail", f"{summary.line()} – " + "; ".join(problems), evidence), executed, False)
            record_status = "failed" if executed else "error"
        else:
            result = TestOutcome(ctx.check(category, name, "pass", summary.line(), evidence), executed, True)
            record_status = "passed"
    elif out.exit_code == 0:
        evidence["note"] = "runner output has no recognised summary: test count not verified (exit code only)"
        result = TestOutcome(ctx.check(category, name, "pass", "exit code 0 (test count not verified)", evidence), None, True)
        record_status = "passed"
    else:
        result = TestOutcome(
            ctx.check(category, name, "fail", f"test command failed with exit code {out.exit_code}", evidence), None, False
        )
        record_status = "failed"
    ctx.runner.tests.append(
        TestRecord(
            command=pg_safe(ctx.redactor.text(ev.command)),
            framework=evidence["detected_framework"] if counts else ev.framework,
            status=record_status,
            passed=summary.passed,
            failed=summary.failed,
            errors=summary.errors,
            skipped=summary.skipped,
            output_excerpt=ctx.runner.clip(output),
            duration_ms=out.duration_ms,
        )
    )
    return result


# ------------------------------------------------------------------------------------------- schema / artifact
async def schema_evidence(ctx: VerifyContext, index: int, ev: SchemaEvidence) -> VerificationCheck:
    name = label(index, ev)
    try:
        rel = normalise_path(ev.path)
    except ValueError:
        return ctx.check("schema", name, "error", f"invalid path {ev.path!r}", {"path": ev.path})
    base: dict[str, Any] = {"path": rel, "format": ev.format, "has_schema": ev.json_schema is not None}
    text, reason = await asyncio.to_thread(read_text, ctx.root, rel, limit=MAX_SCHEMA_FILE_BYTES)
    if text is None:
        return ctx.check("schema", name, "fail", f"{rel}: {reason}", base)
    try:
        document = parse_document(text, ev.format)
    except SchemaParseError as exc:
        return ctx.check("schema", name, "fail", f"{rel}: {exc}", base)
    if ev.json_schema is None:
        return ctx.check("schema", name, "pass", f"{rel} is valid {ev.format.upper()}", base)
    schema = ev.json_schema
    try:
        errors, engine = await asyncio.to_thread(validate, document, schema)
    except Exception as exc:
        return ctx.check("schema", name, "error", f"schema validation failed: {exc}", base)
    evidence = {**base, "engine": engine, "errors": errors}
    if errors:
        return ctx.check("schema", name, "fail", f"{rel} violates the schema: {errors[0]}", evidence)
    return ctx.check("schema", name, "pass", f"{rel} matches the schema", evidence)


async def artifact_evidence(ctx: VerifyContext, index: int, ev: ArtifactEvidence) -> VerificationCheck:
    name = label(index, ev)
    base: dict[str, Any] = {"kind": ev.kind, "name_glob": ev.name_glob, "min_count": ev.min_count, "min_size_bytes": ev.min_size_bytes}
    try:
        records = list(await ctx.artifacts(ctx.job_id, ctx.step_id, ev.kind))
    except Exception as exc:
        return ctx.check("artifact", name, "error", f"artifact lookup failed: {type(exc).__name__}: {exc}", base)
    named = [r for r in records if r.kind == ev.kind and r.matches(ev.name_glob)]
    big_enough = [r for r in named if r.size_bytes >= ev.min_size_bytes]
    evidence = {
        **base,
        "found": [{"name": r.name, "size_bytes": r.size_bytes} for r in named],
        "too_small": [r.name for r in named if r.size_bytes < ev.min_size_bytes],
    }
    if len(big_enough) >= ev.min_count:
        return ctx.check("artifact", name, "pass", f"{len(big_enough)} artifact(s) of kind {ev.kind!r} match {ev.name_glob!r}", evidence)
    msg = (
        f"{len(big_enough)} artifact(s) of kind {ev.kind!r} matching {ev.name_glob!r} with ≥{ev.min_size_bytes} bytes; "
        f"{ev.min_count} required"
    )
    return ctx.check("artifact", name, "fail", msg, evidence)


# ------------------------------------------------------------------------------------------- mirrored criteria
def mirrored(
    ctx: VerifyContext, index: int, ev: ScopeEvidence | SecurityEvidence, sources: Sequence[VerificationCheck]
) -> VerificationCheck:
    """Scope/security criteria are satisfied by the generic (always-on, non-disableable) checks of the same kind."""
    name = label(index, ev)
    failing = [c for c in sources if c.status in ("fail", "error") and c.blocking]
    evidence: dict[str, Any] = {"satisfied_by": sorted({f"{c.check_type}:{c.name}" for c in sources})}
    if isinstance(ev, SecurityEvidence):
        evidence.update({"secret_scan": ev.secret_scan, "conflict_markers": ev.conflict_markers})
    if failing:
        evidence["failing"] = [f"{c.check_type}:{c.name}" for c in failing]
        status: CheckStatus = "error" if all(c.status == "error" for c in failing) else "fail"
        return ctx.check(ev.type, name, status, "; ".join(c.message for c in failing[:3]), evidence)
    return ctx.check(ev.type, name, "pass", f"satisfied by {len(sources)} generic check(s)", evidence)
