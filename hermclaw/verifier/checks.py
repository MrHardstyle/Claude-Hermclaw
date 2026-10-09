"""Generic (task-independent) verifier checks: scope, forbidden paths, generated files, changed-file count, deletion
policy, secrets, conflict markers, syntax, compile, lint and the implement-step test requirement (P21 21.1–21.8,
21.12)."""

from __future__ import annotations

import asyncio
import os
import shlex
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hermclaw.contracts.verification import VerificationCheck
from hermclaw.verifier.changes import matching, read_bytes, read_text
from hermclaw.verifier.conflicts import scan_conflicts
from hermclaw.verifier.context import VerifyContext
from hermclaw.verifier.languages import CODE_LANGUAGES, detect_language, repo_has_tests
from hermclaw.verifier.secrets import SecretScanner
from hermclaw.verifier.syntax import (
    IN_PROCESS,
    SyntaxResult,
    check_in_sandbox,
    check_shell_local,
    javascript_needs_compile_check,
)
from hermclaw.verifier.types import AddedContent

MAX_SYNTAX_FILE_BYTES = 2 * 1024 * 1024
COMPILE_TIMEOUT_FALLBACK = 600


# ------------------------------------------------------------------------------------------- 21.1 scope
def scope_checks(ctx: VerifyContext) -> list[VerificationCheck]:
    changes = ctx.changes
    guard = ctx.guard
    out: list[VerificationCheck] = []
    forbidden = sorted({c.path for c in changes.changes if guard.is_forbidden(c.path)})
    if forbidden:
        out.append(
            ctx.check(
                "forbidden",
                "forbidden_paths",
                "fail",
                f"{len(forbidden)} forbidden path(s) touched: {', '.join(forbidden[:10])}",
                {"paths": forbidden, "path": forbidden[0], "forbidden_globs": guard.forbidden},
            )
        )
    else:
        out.append(ctx.check("forbidden", "forbidden_paths", "pass", "no forbidden path was touched", {"forbidden_globs": guard.forbidden}))

    evidence: dict[str, Any] = {
        "changes": [{"path": c.path, "operation": c.operation} for c in changes.changes],
        "operations_from": "base tree" if changes.base_known else "git status",
    }
    if changes.invalid_paths:
        evidence["invalid_paths"] = changes.invalid_paths
        out.append(ctx.check("scope", "scope", "fail", f"git reported invalid paths: {changes.invalid_paths[:5]}", evidence))
        return out
    if ctx.step.scope is None:
        if changes.changes:
            msg = f"the step has no scope contract but changed {len(changes.changes)} file(s)"
            evidence["path"] = changes.changes[0].path
            out.append(ctx.check("scope", "scope", "fail", msg, evidence))
        else:
            out.append(ctx.check("scope", "scope", "pass", "no changes and no scope contract", evidence))
        return out
    violations = [v for v in guard.audit(changes.operations()) if v["path"] not in forbidden]
    evidence["scope_version"] = ctx.step.scope.version
    if violations:
        evidence["violations"] = violations
        evidence["path"] = violations[0]["path"]
        reasons = "; ".join(v["reason"] for v in violations[:5])
        out.append(
            ctx.check("scope", "scope", "fail", f"{len(violations)} change(s) outside scope v{ctx.step.scope.version}: {reasons}", evidence)
        )
    else:
        out.append(
            ctx.check(
                "scope", "scope", "pass", f"all {len(changes.changes)} change(s) are inside scope v{ctx.step.scope.version}", evidence
            )
        )
    return out


# ------------------------------------------------------------------------------------------- policy checks
def generated_check(ctx: VerifyContext) -> VerificationCheck:
    globs = list(ctx.policies.verifier.generated_file_globs)
    hits = matching([c.path for c in ctx.changes.existing], globs)
    if hits:
        return ctx.check(
            "generated",
            "generated_files",
            "fail",
            f"{len(hits)} generated file(s) are part of the change: {', '.join(hits[:10])}",
            {"paths": hits, "path": hits[0], "generated_globs": globs},
        )
    return ctx.check("generated", "generated_files", "pass", "no generated files in the change", {"generated_globs": globs})


def changed_count_check(ctx: VerifyContext) -> VerificationCheck:
    limit = ctx.policies.verifier.max_changed_files
    count = len(ctx.changes.paths)
    evidence = {"changed_count": count, "max_changed_files": limit}
    if count > limit:
        return ctx.check("changed_files", "changed_file_count", "fail", f"{count} files changed, policy allows at most {limit}", evidence)
    return ctx.check("changed_files", "changed_file_count", "pass", f"{count} file(s) changed (limit {limit})", evidence)


def deletions_check(ctx: VerifyContext) -> VerificationCheck:
    deleted = [c.path for c in ctx.changes.deleted]
    allow = ctx.policies.verifier.allow_deletions
    evidence: dict[str, Any] = {"deleted": deleted, "allow_deletions": allow}
    if not deleted:
        return ctx.check("deletions", "deleted_files", "pass", "no files were deleted", evidence)
    evidence["path"] = deleted[0]
    if not allow:
        return ctx.check("deletions", "deleted_files", "fail", f"policy forbids deletions; deleted: {', '.join(deleted[:10])}", evidence)
    if ctx.step.scope is None:
        return ctx.check("deletions", "deleted_files", "fail", "deletions need a scope contract that allows 'delete'", evidence)
    guard = ctx.guard
    refused = [{"path": p, "reason": reason} for p in deleted for ok, reason in [guard.decide(p, "delete")] if not ok]
    if refused:
        evidence["refused"] = refused
        return ctx.check(
            "deletions", "deleted_files", "fail", f"{len(refused)} deletion(s) not allowed by the scope: {refused[0]['reason']}", evidence
        )
    return ctx.check("deletions", "deleted_files", "pass", f"{len(deleted)} deletion(s) allowed by policy and scope", evidence)


# ------------------------------------------------------------------------------------------- 21.7 / 21.8 content
def secret_checks(ctx: VerifyContext, added: AddedContent, scanner: SecretScanner) -> list[VerificationCheck]:
    out: list[VerificationCheck] = []
    total_lines = 0
    for path in sorted(added.lines):
        lines = added.lines[path]
        total_lines += len(lines)
        findings = scanner.scan_file(path, lines)
        if findings:
            rules = sorted({f.rule for f in findings})
            where = ", ".join(f"line {f.line} ({f.rule})" for f in findings[:5])
            out.append(
                ctx.check(
                    "secrets",
                    f"secrets:{path}",
                    "fail",
                    f"possible secret(s) added to {path}: {where}. Remove the value and load it from the environment or a secret store.",
                    {"path": path, "findings": [f.to_dict() for f in findings], "rules": rules},
                )
            )
    if not out:
        out.append(
            ctx.check(
                "secrets",
                "secrets",
                "pass",
                f"no secrets in {total_lines} added line(s) of {len(added.lines)} file(s)",
                {
                    "files_scanned": len(added.lines),
                    "lines_scanned": total_lines,
                    "skipped": added.skipped,
                    "truncated": added.truncated,
                    "source": added.source,
                },
            )
        )
    return out


def conflict_checks(ctx: VerifyContext, added: AddedContent) -> list[VerificationCheck]:
    out: list[VerificationCheck] = []
    for path in sorted(added.lines):
        markers = scan_conflicts(path, added.lines[path])
        if markers:
            where = ", ".join(f"line {m.line} {m.marker}" for m in markers[:5])
            out.append(
                ctx.check(
                    "conflicts",
                    f"conflicts:{path}",
                    "fail",
                    f"merge conflict markers in {path}: {where}",
                    {"path": path, "markers": [m.to_dict() for m in markers]},
                )
            )
    if not out:
        out.append(ctx.check("conflicts", "conflict_markers", "pass", f"no conflict markers in {len(added.lines)} changed file(s)"))
    return out


# ------------------------------------------------------------------------------------------- 21.2 syntax
def _to_check(ctx: VerifyContext, r: SyntaxResult) -> VerificationCheck:
    evidence = {"path": r.path, "language": r.language, **r.evidence}
    return ctx.check("syntax", f"syntax:{r.path}", r.status, r.message, evidence)  # type: ignore[arg-type]


async def syntax_checks(ctx: VerifyContext) -> list[VerificationCheck]:
    results: list[SyntaxResult] = []
    sandbox: dict[str, list[str]] = defaultdict(list)
    shell: list[str] = []
    root = ctx.root
    for change in ctx.changes.existing:
        rel = change.path
        data, truncated, reason = await asyncio.to_thread(read_bytes, root, rel, limit=MAX_SYNTAX_FILE_BYTES)
        if data is None:
            results.append(SyntaxResult(rel, "unknown", "skip", f"not checked: {reason}"))
            continue
        language = detect_language(rel, data[:256])
        if language is None:
            continue  # unknown file type: nothing to check (not reported)
        if truncated:
            results.append(SyntaxResult(rel, language, "skip", f"not checked: larger than {MAX_SYNTAX_FILE_BYTES} bytes"))
            continue
        text, why = await asyncio.to_thread(read_text, root, rel, limit=MAX_SYNTAX_FILE_BYTES)
        if text is None:
            results.append(SyntaxResult(rel, language, "skip", f"not checked: {why}"))
            continue
        if language in IN_PROCESS:
            results.append(await asyncio.to_thread(IN_PROCESS[language], rel, text))
        elif language == "shell":
            shell.append(rel)
        elif language == "php":
            sandbox["php"].append(rel)
        elif language == "javascript":
            skip = javascript_needs_compile_check(rel, text)
            if skip:
                results.append(SyntaxResult(rel, language, "skip", skip))
            else:
                sandbox["javascript"].append(rel)
        elif language == "typescript":
            results.append(SyntaxResult(rel, language, "skip", "TypeScript is checked by the compile check (tsc)"))
        elif language in CODE_LANGUAGES:
            reason = f"no syntax checker for {language}; covered by compile/lint/tests if configured"
            results.append(SyntaxResult(rel, language, "skip", reason))
        # data/markup formats without a parser (markdown, text, css, html, …) are not reported
    if shell:
        local = await check_shell_local(root, shell)
        if local is None:
            sandbox["shell"].extend(shell)
        else:
            results.extend(local)
    for language, paths in sandbox.items():
        results.extend(await check_in_sandbox(ctx.runner, language, paths))
    if not results:
        return [ctx.check("syntax", "syntax", "skip", "no changed file with a checkable syntax")]
    return [_to_check(ctx, r) for r in sorted(results, key=lambda r: r.path)]


# ------------------------------------------------------------------------------------------- 21.3 compile
_TS_RELEVANT = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".d.ts")


def _relevant(paths: Sequence[str], suffixes: Sequence[str], names: Sequence[str]) -> list[str]:
    return [p for p in paths if p.endswith(tuple(suffixes)) or os.path.basename(p) in names]


def _toolchains(root: Path, changed: Sequence[str], *, network: bool) -> list[tuple[str, str, list[str], str | None]]:
    """``(language, command, relevant changed files, skip reason)`` for each compile toolchain the repository has."""
    toolchains: list[tuple[str, str, list[str], str | None]] = []
    if os.path.isfile(root / "tsconfig.json"):
        relevant = _relevant(changed, _TS_RELEVANT, ("tsconfig.json", "package.json"))
        reason = None
        if not os.path.isdir(root / "node_modules"):
            reason = "node_modules is not installed in the workspace"
        elif not os.path.isdir(root / "node_modules" / "typescript"):
            reason = "typescript is not installed in node_modules"
        toolchains.append(("typescript", "npx tsc --noEmit", relevant, reason))
    if os.path.isfile(root / "go.mod"):
        toolchains.append(("go", "go build ./...", _relevant(changed, (".go",), ("go.mod", "go.sum")), None))
    if os.path.isfile(root / "Cargo.toml"):
        cmd = "cargo check" if network else "cargo check --offline"
        toolchains.append(("rust", cmd, _relevant(changed, (".rs",), ("Cargo.toml", "Cargo.lock")), None))
    return toolchains


async def compile_checks(ctx: VerifyContext) -> list[VerificationCheck]:
    root = ctx.root
    changed = ctx.changes.paths
    timeout = ctx.policies.sandbox.default_timeout_seconds or COMPILE_TIMEOUT_FALLBACK
    out: list[VerificationCheck] = []
    toolchains = await asyncio.to_thread(_toolchains, root, changed, network=ctx.step.network)
    for language, command, relevant, reason in toolchains:
        name = f"compile:{language}"
        evidence: dict[str, Any] = {"command": command, "relevant_files": relevant}
        if not relevant:
            out.append(ctx.check("compile", name, "skip", f"no changed {language} files", evidence))
            continue
        if reason:
            out.append(ctx.check("compile", name, "skip", f"{command} skipped: {reason}", evidence))
            continue
        res = await ctx.runner.run(command, purpose="verifier", timeout_seconds=timeout, network=ctx.step.network)
        evidence.update({"exit_code": res.exit_code, "duration_ms": res.duration_ms, "output": ctx.runner.clip(res.output, 4000)})
        if res.failed_to_run:
            out.append(ctx.check("compile", name, "error", f"{command} could not run: {res.problem or 'executor error'}", evidence))
        elif res.timed_out:
            out.append(ctx.check("compile", name, "fail", f"{command} timed out after {timeout}s", evidence))
        elif res.exit_code == 0:
            out.append(ctx.check("compile", name, "pass", f"{command} succeeded", evidence))
        elif res.exit_code in (126, 127):
            out.append(ctx.check("compile", name, "skip", f"{command.split()[0]} is not available in the sandbox image", evidence))
        else:
            out.append(ctx.check("compile", name, "fail", f"{command} failed with exit code {res.exit_code}", evidence))
    if not out:
        out.append(
            ctx.check("compile", "compile", "skip", "repository has no compile toolchain marker (tsconfig.json, go.mod, Cargo.toml)")
        )
    return out


# ------------------------------------------------------------------------------------------- 21.4 lint
def lint_command(template: str, paths: Sequence[str]) -> str:
    quoted = " ".join(shlex.quote(p if not p.startswith("-") else "./" + p) for p in paths)
    return template.replace("{files}", quoted)


async def lint_checks(ctx: VerifyContext) -> list[VerificationCheck]:
    commands = ctx.policies.verifier.lint_commands
    if not commands:
        return [ctx.check("lint", "lint", "skip", "no lint commands configured (policies.verifier.lint_commands)")]
    by_language: dict[str, list[str]] = defaultdict(list)
    for change in ctx.changes.existing:
        data, _truncated, _reason = await asyncio.to_thread(read_bytes, ctx.root, change.path, limit=256)
        language = detect_language(change.path, data)
        if language is not None:
            by_language[language].append(change.path)
    timeout = ctx.policies.sandbox.default_timeout_seconds or COMPILE_TIMEOUT_FALLBACK
    out: list[VerificationCheck] = []
    for language in sorted(commands):
        files = by_language.get(language, [])
        name = f"lint:{language}"
        if not files:
            continue
        command = lint_command(commands[language], files)
        res = await ctx.runner.run(command, purpose="lint", timeout_seconds=timeout, network=False)
        evidence: dict[str, Any] = {
            "command": command,
            "files": files,
            "exit_code": res.exit_code,
            "duration_ms": res.duration_ms,
            "output": ctx.runner.clip(res.output, 4000),
        }
        if len(files) == 1:
            evidence["path"] = files[0]
        if res.failed_to_run:
            out.append(ctx.check("lint", name, "error", f"lint command could not run: {res.problem or 'executor error'}", evidence))
        elif res.timed_out:
            out.append(ctx.check("lint", name, "fail", f"lint timed out after {timeout}s", evidence))
        elif res.exit_code in (126, 127):
            out.append(ctx.check("lint", name, "error", f"configured lint command is not available in the sandbox: {command}", evidence))
        elif res.exit_code == 0:
            out.append(ctx.check("lint", name, "pass", f"lint passed for {len(files)} {language} file(s)", evidence))
        else:
            out.append(ctx.check("lint", name, "fail", f"lint reported problems (exit code {res.exit_code})", evidence))
    if not out:
        out.append(ctx.check("lint", "lint", "skip", "no changed files in a language with a configured lint command"))
    return out


# ------------------------------------------------------------------------------------------- 21.12 test requirement
async def require_tests_check(ctx: VerifyContext, outcomes: Sequence[tuple[VerificationCheck, bool | None, bool]]) -> VerificationCheck:
    """An implement step in a repository with tests must have executed at least one passing test command."""
    if not ctx.step.is_implement:
        return ctx.check("test_evidence", "test_evidence", "skip", f"not required for '{ctx.step.kind}' steps")
    test_files = repo_has_tests(await ctx.files())
    evidence: dict[str, Any] = {"test_files_sample": test_files, "test_evidence_count": len(outcomes)}
    if not test_files:
        return ctx.check("test_evidence", "test_evidence", "skip", "the repository has no automated tests", evidence)
    if not outcomes:
        return ctx.check(
            "test_evidence",
            "test_evidence",
            "fail",
            "implement step in a repository with tests has no test evidence: add a test criterion that runs the relevant tests",
            evidence,
        )
    executed_ok = [c for c, executed, ok in outcomes if ok and executed is not False]
    evidence["passing"] = [c.name for c in executed_ok]
    evidence["counts_verified"] = any(executed for _c, executed, ok in outcomes if ok)
    if executed_ok:
        return ctx.check("test_evidence", "test_evidence", "pass", f"{len(executed_ok)} test command(s) executed and passed", evidence)
    if all(ok for _c, _e, ok in outcomes):
        return ctx.check("test_evidence", "test_evidence", "fail", "test commands passed but did not execute any test", evidence)
    return ctx.check("test_evidence", "test_evidence", "fail", "no test command executed and passed", evidence)
