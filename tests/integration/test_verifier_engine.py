"""Verifier end-to-end against real git workspaces, real local tools and a real PostgreSQL database (P21)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.persistence.models import Artifact, CommandRun, Event, TestRun, VerificationCheckRow, VerificationRun
from hermclaw.verifier import ArtifactRecord, Verifier
from tests.integration.test_verifier_support import (
    GitCliReader,
    WorkspaceShellExecutor,
    by_name,
    commit_all,
    git,
    handle,
    init_repo,
    make_job_step,
    make_verifier,
    of_type,
    scope,
    step,
    write,
)

pytestmark = pytest.mark.integration

CALC = "def add(a, b):\n    return a + b\n"
CALC_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\ndef test_add_neg():\n    assert add(-1, 1) == 0\n"


def py_repo(tmp_path: Path) -> Path:
    return init_repo(
        tmp_path / "repo",
        {"README.md": "# demo\n", "calc.py": CALC, "tests/test_calc.py": CALC_TEST, ".gitignore": "__pycache__/\n.pytest_cache/\n"},
    )


async def run(sm: Any, repo: Path, st: Any, *, verifier: Verifier | None = None, kind: str = "implement", **kw: Any) -> Any:
    job_id, step_id = await make_job_step(sm, kind=kind)
    ws = handle(repo, job_id, kw.pop("base_sha", None))
    v = verifier or make_verifier(sm)
    return await v.run(st, ws, job_id=job_id, step_id=step_id, **kw), job_id, step_id


# ------------------------------------------------------------------------------------------- happy path + persistence
async def test_passing_step_is_persisted_with_checks_events_and_command_rows(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "\n\ndef sub(a, b):\n    return a - b\n")
    write(repo, "tests/test_sub.py", "from calc import sub\n\n\ndef test_sub():\n    assert sub(3, 2) == 1\n")
    st = step(
        acceptance=[
            {"type": "test", "command": "python3 -m pytest -q -p no:cacheprovider tests", "framework": "pytest", "min_passed": 3},
            {"type": "presence", "path_glob": "calc.py", "pattern": r"^def sub\(", "description": "sub exists"},
            {"type": "absence", "path_glob": "**/*.py", "pattern": "OLD_SPECIAL_CASE"},
            {"type": "diff", "must_change": ["calc.py"], "must_not_change": ["README.md"], "max_changed_files": 5},
            {"type": "command", "command": "python3 -c 'import calc; print(calc.sub(5, 3))'", "stdout_pattern": r"^2$"},
            {"type": "scope"},
            {"type": "security"},
        ],
        scope_contract=scope(target_paths=["calc.py"], allowed_new_paths=["tests/**"]),
    )
    outcome, job_id, step_id = await run(sessionmaker, repo, st)
    report = outcome.report
    assert report.passed, report.summary
    assert outcome.status == "passed"
    assert report.changed_files == ["calc.py", "tests/test_sub.py"]
    names = by_name(report)
    assert names["scope"].status == "pass"
    assert names["syntax:calc.py"].status == "pass"
    assert names["test_evidence"].status == "pass"
    unit = names["acceptance[0]:test"]
    assert unit.check_type == "unit" and unit.evidence["passed"] == 3 and unit.evidence["counts_verified"] is True
    assert names["acceptance[1]:presence – sub exists"].status == "pass"
    assert names["acceptance[4]:command"].evidence["stdout_matched"] is True
    assert "Verification passed" in report.summary
    async with sessionmaker() as s:
        row = await s.get(VerificationRun, outcome.run_id)
        assert row is not None and row.passed and row.status == "passed" and row.finished_at is not None
        assert row.changed_files == ["calc.py", "tests/test_sub.py"]
        rows = (
            (await s.execute(select(VerificationCheckRow).where(VerificationCheckRow.verification_run_id == outcome.run_id)))
            .scalars()
            .all()
        )
        assert len(rows) == len(report.checks)
        events = (await s.execute(select(Event).where(Event.step_id == step_id).order_by(Event.sequence))).scalars().all()
        kinds = [e.event_type for e in events]
        assert kinds[0] == EventType.VERIFIER_STARTED and kinds[-1] == EventType.VERIFIER_FINISHED
        assert EventType.VERIFIER_CHECK_FAILED not in kinds
        assert events[-1].payload["passed"] is True and events[-1].payload["verification_run_id"] == str(outcome.run_id)
        cmds = (await s.execute(select(CommandRun).where(CommandRun.step_id == step_id))).scalars().all()
        assert {c.classification for c in cmds} >= {"verifier:test", "verifier:verifier"}
        tests = (await s.execute(select(TestRun).where(TestRun.step_id == step_id))).scalars().all()
        assert len(tests) == 1 and tests[0].status == "passed" and tests[0].passed == 3 and tests[0].framework == "pytest"
    # verification leaves no trace in the workspace (pytest caches, __pycache__ …)
    assert sorted(git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()) == [" M calc.py", "?? tests/test_sub.py"]


# ------------------------------------------------------------------------------------------- 21.1 scope / forbidden / policy
async def test_scope_violation_and_forbidden_path_fail(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "# touched\n")
    write(repo, "README.md", "# changed outside scope\n")
    write(repo, "config/.env", "DEBUG=1\n")
    st = step("documentation", scope_contract=scope(target_paths=["calc.py"], allowed_new_paths=["config/**"]))
    outcome, _job, step_id = await run(sessionmaker, repo, st, kind="documentation")
    names = by_name(outcome.report)
    assert not outcome.report.passed and outcome.status == "failed"
    assert names["scope"].status == "fail"
    assert [v["path"] for v in names["scope"].evidence["violations"]] == ["README.md"]
    assert names["forbidden_paths"].status == "fail" and names["forbidden_paths"].evidence["paths"] == ["config/.env"]
    async with sessionmaker() as s:
        failed = (
            (await s.execute(select(Event).where(Event.step_id == step_id, Event.event_type == EventType.VERIFIER_CHECK_FAILED)))
            .scalars()
            .all()
        )
        assert {e.payload["name"] for e in failed} == {"scope", "forbidden_paths"}
        assert {e.payload.get("path") for e in failed} == {"README.md", "config/.env"}
        row = await s.get(VerificationRun, outcome.run_id)
        assert row is not None and row.status == "failed" and not row.passed and "scope" in (row.summary or "")


async def test_operations_are_judged_against_the_base_commit(sessionmaker: Any, tmp_path: Path) -> None:
    """A file created *and committed* after the base is still a creation (needs allowed_new_paths)."""
    repo = py_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD").strip()
    write(repo, "src/new.py", "X = 1\n")
    commit_all(repo, "earlier step")
    write(repo, "src/new.py", "X = 2\n")
    only_target = step("documentation", scope_contract=scope(target_paths=["src/*.py"], allowed_new_paths=[]))
    outcome, *_ = await run(sessionmaker, repo, only_target, base_sha=base, kind="documentation")
    scope_check = by_name(outcome.report)["scope"]
    assert scope_check.status == "fail" and scope_check.evidence["changes"] == [{"path": "src/new.py", "operation": "create"}]
    allowed = step("documentation", scope_contract=scope(target_paths=[], allowed_new_paths=["src/**"]))
    outcome2, *_ = await run(sessionmaker, repo, allowed, base_sha=base, kind="documentation")
    assert by_name(outcome2.report)["scope"].status == "pass"


async def test_changes_without_scope_contract_fail(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "# x\n")
    outcome, *_ = await run(sessionmaker, repo, step("documentation"), kind="documentation")
    assert by_name(outcome.report)["scope"].status == "fail"
    clean = init_repo(tmp_path / "clean")
    outcome2, *_ = await run(
        sessionmaker, clean, step("documentation", acceptance=[{"type": "diff", "allow_empty": True}]), kind="documentation"
    )
    assert outcome2.report.passed, outcome2.report.summary


async def test_deletion_policy(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    (repo / "README.md").unlink()
    no_delete = step("documentation", scope_contract=scope(target_paths=["README.md"]))
    outcome, *_ = await run(sessionmaker, repo, no_delete, kind="documentation")
    names = by_name(outcome.report)
    assert names["deleted_files"].status == "fail" and "not allowed by the scope" in names["deleted_files"].message
    with_delete = step("documentation", scope_contract=scope(target_paths=["README.md"], allowed_operations=["modify", "delete"]))
    outcome2, *_ = await run(sessionmaker, repo, with_delete, kind="documentation")
    assert by_name(outcome2.report)["deleted_files"].status == "pass" and outcome2.report.passed
    assert outcome2.report.changed_files == ["README.md"]
    job_policy = make_verifier(sessionmaker, allow_deletions=False)
    outcome3, *_ = await run(sessionmaker, repo, with_delete, verifier=job_policy, kind="documentation")
    assert (
        by_name(outcome3.report)["deleted_files"].status == "fail"
        and "policy forbids deletions" in by_name(outcome3.report)["deleted_files"].message
    )


async def test_generated_files_and_changed_file_limit(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"README.md": "x\n"})
    write(repo, "dist/bundle.js", "console.log(1);\n")
    for i in range(3):
        write(repo, f"docs/p{i}.md", f"page {i}\n")
    st = step("documentation", scope_contract=scope(allowed_new_paths=["dist/**", "docs/**"]))
    v = make_verifier(sessionmaker, max_changed_files=3)
    outcome, *_ = await run(sessionmaker, repo, st, verifier=v, kind="documentation")
    names = by_name(outcome.report)
    assert names["generated_files"].status == "fail" and names["generated_files"].evidence["paths"] == ["dist/bundle.js"]
    assert names["changed_file_count"].status == "fail" and names["changed_file_count"].evidence["changed_count"] == 4


# ------------------------------------------------------------------------------------------- 21.2 syntax
async def test_syntax_per_language_with_real_tools(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"README.md": "x\n"})
    files = {
        "ok.py": "x = 1\n",
        "bad.py": "def f(:\n",
        "ok.json": '{"a": 1}\n',
        "bad.json": '{"a": }\n',
        "ok.yaml": "a: 1\n",
        "bad.yaml": "a: [1\n",
        "ok.toml": 'a = "b"\n',
        "bad.toml": "a = \n",
        "ok.sh": "echo ok\n",
        "bad.sh": "if then fi\n",
        "ok.php": "<?php\necho 'x';\n",
        "bad.php": "<?php\necho 'x'\n",
        "ok.js": "const a = 1;\n",
        "bad.js": "const a = ;\n",
        "esm.mjs": "export const a = 1;\n",
        "comp.tsx": "export const A = () => <div/>;\n",
        "notes.txt": "plain\n",
        "logo.png": "\x89PNG\x00\x00binary",
    }
    for rel, content in files.items():
        write(repo, rel, content)
    executor = WorkspaceShellExecutor()
    v = make_verifier(sessionmaker, executor)
    outcome, *_ = await run(
        sessionmaker, repo, step("documentation", scope_contract=scope(allowed_new_paths=["**"])), verifier=v, kind="documentation"
    )
    syntax = {c.evidence.get("path"): c for c in of_type(outcome.report, "syntax")}
    for rel in ("ok.py", "ok.json", "ok.yaml", "ok.toml", "ok.sh", "ok.php", "ok.js", "esm.mjs"):
        assert syntax[rel].status == "pass", (rel, syntax[rel].message)
    for rel in ("bad.py", "bad.json", "bad.yaml", "bad.toml", "bad.sh", "bad.php", "bad.js"):
        assert syntax[rel].status == "fail", (rel, syntax[rel].message)
    assert syntax["comp.tsx"].status == "skip" and "tsc" in syntax["comp.tsx"].message
    assert "notes.txt" not in syntax and "logo.png" not in syntax
    assert "unexpected" in syntax["bad.php"].message.lower() and "SyntaxError" in syntax["bad.js"].message
    # php and node checks are batched: one sandbox command per language
    commands = [r.command for r in executor.requests]
    assert sum("php -l" in c for c in commands) == 1 and sum("node --check" in c for c in commands) == 1
    assert all(r.purpose == "verifier" and r.network is False for r in executor.requests)


async def test_missing_sandbox_tool_skips_with_reason(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"README.md": "x\n"})
    write(repo, "a.php", "<?php echo 1;\n")

    class NoPhp(WorkspaceShellExecutor):
        async def run(self, workspace: Any, req: Any) -> Any:
            return await super().run(
                workspace, type(req)(**{**req.__dict__, "command": req.command.replace("php -l", "php-not-installed -l")})
            )

    outcome, *_ = await run(
        sessionmaker,
        repo,
        step("documentation", scope_contract=scope(allowed_new_paths=["*.php"])),
        verifier=make_verifier(sessionmaker, NoPhp()),
        kind="documentation",
    )
    check = by_name(outcome.report)["syntax:a.php"]
    assert check.status == "skip" and "not available" in check.message
    assert outcome.report.passed


# ------------------------------------------------------------------------------------------- 21.3 compile / 21.4 lint
async def test_compile_go_pass_and_fail(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"go.mod": "module example.com/demo\n\ngo 1.21\n", "main.go": "package main\n\nfunc main() {}\n"})
    write(repo, "util.go", "package main\n\nfunc helper() int { return 1 }\n")
    st = step("documentation", scope_contract=scope(allowed_new_paths=["*.go"], target_paths=["main.go"]))
    outcome, *_ = await run(sessionmaker, repo, st, kind="documentation")
    assert by_name(outcome.report)["compile:go"].status == "pass", by_name(outcome.report)["compile:go"].message
    assert not (repo / "demo").exists()  # build output removed again
    write(repo, "util.go", 'package main\n\nfunc helper() int { return "x" }\n')
    outcome2, *_ = await run(sessionmaker, repo, st, kind="documentation")
    check = by_name(outcome2.report)["compile:go"]
    assert check.status == "fail" and "cannot use" in check.evidence["output"]


async def test_compile_cargo_offline(sessionmaker: Any, tmp_path: Path) -> None:
    cargo = '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n'
    repo = init_repo(tmp_path / "repo", {"Cargo.toml": cargo, "src/main.rs": "fn main() {}\n", ".gitignore": "target/\n"})
    write(repo, "src/main.rs", 'fn main() { let x: i32 = "no"; }\n')
    st = step("documentation", scope_contract=scope(target_paths=["src/main.rs"]))
    outcome, *_ = await run(sessionmaker, repo, st, kind="documentation")
    check = by_name(outcome.report)["compile:rust"]
    assert check.evidence["command"] == "cargo check --offline"
    assert check.status == "fail" and "mismatched types" in check.evidence["output"]


async def test_compile_typescript_skips_without_node_modules_and_irrelevant_changes(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"tsconfig.json": "{}\n", "src/a.ts": "export const a = 1;\n", "go.mod": "module x\n\ngo 1.21\n"})
    write(repo, "src/a.ts", "export const a: number = 2;\n")
    outcome, *_ = await run(
        sessionmaker, repo, step("documentation", scope_contract=scope(target_paths=["src/a.ts"])), kind="documentation"
    )
    names = by_name(outcome.report)
    assert names["compile:typescript"].status == "skip" and "node_modules" in names["compile:typescript"].message
    assert names["compile:go"].status == "skip" and "no changed go files" in names["compile:go"].message


async def test_lint_commands_run_only_for_changed_languages(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "print('debug')\n")
    executor = WorkspaceShellExecutor()
    v = make_verifier(sessionmaker, executor, lint_commands={"python": "! grep -n 'print(' {files}", "javascript": "exit 1"})
    st = step("documentation", scope_contract=scope(target_paths=["calc.py"]))
    outcome, *_ = await run(sessionmaker, repo, st, verifier=v, kind="documentation")
    lint = by_name(outcome.report)["lint:python"]
    assert lint.status == "fail" and "calc.py" in lint.evidence["command"] and "print('debug')" in lint.evidence["output"]
    assert "lint:javascript" not in by_name(outcome.report)
    assert [r.purpose for r in executor.requests if "grep" in r.command] == ["lint"]
    write(repo, "calc.py", CALC + "# clean\n")
    outcome2, *_ = await run(sessionmaker, repo, st, verifier=v, kind="documentation")
    assert by_name(outcome2.report)["lint:python"].status == "pass"


# ------------------------------------------------------------------------------------------- 21.5 / 21.6 / 21.12 tests
async def test_failing_and_insufficient_tests(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", "def add(a, b):\n    return a - b\n")
    st = step(
        acceptance=[
            {"type": "test", "command": "python3 -m pytest -q -p no:cacheprovider tests", "framework": "pytest"},
            {
                "type": "test",
                "command": "python3 -m pytest -q -p no:cacheprovider tests -k neg",
                "framework": "pytest",
                "min_passed": 2,
                "description": "integration subset",
            },
        ],
        scope_contract=scope(target_paths=["calc.py"]),
    )
    outcome, _job, step_id = await run(sessionmaker, repo, st)
    names = by_name(outcome.report)
    unit = names["acceptance[0]:test"]
    assert unit.status == "fail" and unit.evidence["failed"] == 2 and unit.evidence["passed"] == 0
    integ = names["acceptance[1]:test – integration subset"]
    assert integ.check_type == "integration" and integ.status == "fail" and "at least 2 required" in integ.message
    assert names["test_evidence"].status == "fail"
    async with sessionmaker() as s:
        statuses = sorted(t.status for t in (await s.execute(select(TestRun).where(TestRun.step_id == step_id))).scalars())
        assert statuses == ["failed", "failed"]


async def test_implement_step_in_repo_with_tests_needs_test_evidence(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "# change\n")
    outcome, *_ = await run(sessionmaker, repo, step(scope_contract=scope(target_paths=["calc.py"])))
    check = by_name(outcome.report)["test_evidence"]
    assert check.status == "fail" and "no test evidence" in check.message and check.evidence["test_files_sample"]
    zero = step(
        acceptance=[
            {
                "type": "test",
                "command": "python3 -m pytest -q -p no:cacheprovider tests -k nothing_matches",
                "framework": "pytest",
                "min_passed": 0,
            }
        ],
        scope_contract=scope(target_paths=["calc.py"]),
    )
    outcome2, *_ = await run(sessionmaker, repo, zero)
    assert by_name(outcome2.report)["test_evidence"].status == "fail"
    no_tests = init_repo(tmp_path / "plain", {"main.py": "x = 1\n"})
    write(no_tests, "main.py", "x = 2\n")
    outcome3, *_ = await run(sessionmaker, no_tests, step(scope_contract=scope(target_paths=["main.py"])))
    assert by_name(outcome3.report)["test_evidence"].status == "skip" and outcome3.report.passed
    doc = await run(sessionmaker, repo, step("documentation", scope_contract=scope(target_paths=["calc.py"])), kind="documentation")
    assert by_name(doc[0].report)["test_evidence"].status == "skip"


async def test_generic_runner_without_summary_passes_unverified(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "# change\n")
    st = step(
        acceptance=[{"type": "test", "command": "python3 -c 'import calc; assert calc.add(1, 1) == 2'", "framework": "generic"}],
        scope_contract=scope(target_paths=["calc.py"]),
    )
    outcome, *_ = await run(sessionmaker, repo, st)
    names = by_name(outcome.report)
    assert names["acceptance[0]:test"].status == "pass" and names["acceptance[0]:test"].evidence["counts_verified"] is False
    assert names["test_evidence"].status == "pass" and names["test_evidence"].evidence["counts_verified"] is False


# ------------------------------------------------------------------------------------------- 21.7 secrets / 21.8 conflicts
async def test_secret_added_in_diff_blocks_and_never_leaks(sessionmaker: Any, tmp_path: Path) -> None:
    token = "gh" + "p_" + "Q9w8E7r6T5y4U3i2O1p0A9s8D7f6G5h4J3k2"
    old_secret = "AK" + "IA" + "OLDOLDOLDOLDOLD1"
    repo = init_repo(tmp_path / "repo", {"settings.py": f'LEGACY = "{old_secret}"\nDEBUG = False\n'})
    write(repo, "settings.py", f'LEGACY = "{old_secret}"\nDEBUG = True\nGITHUB_TOKEN = "{token}"\n')
    write(repo, "keys/new.txt", "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nabc\n")
    st = step(
        "documentation",
        scope_contract=scope(target_paths=["settings.py"], allowed_new_paths=["keys/**"]),
        acceptance=[{"type": "security"}],
    )
    outcome, _job, step_id = await run(sessionmaker, repo, st, kind="documentation")
    names = by_name(outcome.report)
    settings = names["secrets:settings.py"]
    assert settings.status == "fail" and [f["line"] for f in settings.evidence["findings"]] == [3]  # pre-existing line 1 not reported
    assert names["secrets:keys/new.txt"].evidence["findings"][0]["rule"] == "private_key"
    assert names["acceptance[0]:security"].status == "fail"
    assert not outcome.report.passed
    async with sessionmaker() as s:
        rows = (
            (await s.execute(select(VerificationCheckRow).where(VerificationCheckRow.verification_run_id == outcome.run_id)))
            .scalars()
            .all()
        )
        events = (await s.execute(select(Event).where(Event.step_id == step_id))).scalars().all()
        dumped = repr([(r.message, r.evidence) for r in rows]) + repr([e.payload for e in events]) + outcome.report.model_dump_json()
        assert token not in dumped and token[:20] not in dumped


async def test_conflict_markers_block(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"notes.md": "Title\n=======\n", "app.cfg": "a=1\n"})
    write(repo, "app.cfg", "<<<<<<< HEAD\na=1\n=======\na=2\n>>>>>>> feature\n")
    write(repo, "notes.md", "Title\n=======\nNew\n=======\n")
    outcome, *_ = await run(
        sessionmaker, repo, step("documentation", scope_contract=scope(target_paths=["app.cfg", "notes.md"])), kind="documentation"
    )
    names = by_name(outcome.report)
    assert [m["line"] for m in names["conflicts:app.cfg"].evidence["markers"]] == [1, 3, 5]
    assert "conflicts:notes.md" not in names
    assert not outcome.report.passed


# ------------------------------------------------------------------------------------------- 21.9 / 21.10 presence + absence
async def test_presence_and_absence_evidence(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(
        tmp_path / "repo",
        {"src/a.py": "OLD_FLAG = 1\nkeep = 2\n", "src/b.py": "x = OLD_FLAG\n", "legacy/old.py": "pass\n", ".gitignore": "build/\n"},
    )
    write(repo, "build/out.txt", "ignored artefact\n")
    write(repo, "src/a.py", "keep = 2\n")
    st = step(
        "documentation",
        scope_contract=scope(target_paths=["src/a.py"]),
        acceptance=[
            {"type": "absence", "path_glob": "src/**/*.py", "pattern": "OLD_FLAG"},
            {"type": "absence", "path_glob": "legacy/"},
            {"type": "absence", "path_glob": "build/out.txt"},
            {"type": "absence", "path_glob": "removed/**"},
            {"type": "presence", "path_glob": "src/*.py", "pattern": "keep", "min_matches": 1},
            {"type": "presence", "path_glob": "src/*.py", "pattern": "keep", "min_matches": 2},
            {"type": "presence", "path_glob": "docs/**"},
            {"type": "absence", "path_glob": "src/*.py", "pattern": "(a+)+$"},
            {"type": "presence", "path_glob": "../outside"},
        ],
    )
    outcome, *_ = await run(sessionmaker, repo, st, kind="documentation")
    c = [by_name(outcome.report)[n] for n in sorted(by_name(outcome.report)) if n.startswith("acceptance[")]
    by_index = {int(x.name.split("]")[0].split("[")[1]): x for x in c}
    first = by_index[0]
    assert first.status == "fail" and first.evidence["matching_files"] == ["src/b.py"]
    assert first.evidence["samples"] == [{"path": "src/b.py", "line": 1, "snippet": "x = OLD_FLAG"}]
    assert by_index[1].status == "fail" and by_index[1].evidence["existing_paths"] == ["legacy/old.py"]
    assert by_index[2].status == "fail"  # explicitly named ignored file is still found
    assert by_index[3].status == "pass"
    assert by_index[4].status == "pass" and by_index[5].status == "fail"
    assert by_index[6].status == "fail"
    assert by_index[7].status == "error" and "catastrophic" in by_index[7].message
    assert by_index[8].status == "error"
    (repo / "src/b.py").write_text("x = 1\n", encoding="utf-8")
    import shutil

    shutil.rmtree(repo / "legacy")
    (repo / "build/out.txt").unlink()
    fixed = step(
        "documentation",
        scope_contract=scope(target_paths=["src/*.py", "legacy/**"], allowed_operations=["modify", "delete"]),
        acceptance=st.acceptance[:4],
    )  # type: ignore[arg-type]
    outcome2, *_ = await run(sessionmaker, repo, fixed, kind="documentation")
    assert outcome2.report.passed, outcome2.report.summary


# ------------------------------------------------------------------------------------------- 21.11 command / 21.13 diff
async def test_command_evidence_exit_code_stdout_and_timeout(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "# x\n")
    st = step(
        "documentation",
        scope_contract=scope(target_paths=["calc.py"]),
        acceptance=[
            {"type": "command", "command": "echo version 1.2.3", "stdout_pattern": r"version \d+\.\d+"},
            {"type": "command", "command": "exit 3", "expect_exit_code": 3},
            {"type": "command", "command": "echo nope", "stdout_pattern": "yes"},
            {"type": "command", "command": "exit 1"},
            {"type": "command", "command": "sleep 5", "timeout_seconds": 1},
            {"type": "command", "command": "true", "network": True},
        ],
    )
    executor = WorkspaceShellExecutor()
    outcome, *_ = await run(sessionmaker, repo, st, verifier=make_verifier(sessionmaker, executor), kind="documentation")
    status = [by_name(outcome.report)[n].status for n in sorted(n for n in by_name(outcome.report) if n.startswith("acceptance["))]
    assert status == ["pass", "pass", "fail", "fail", "fail", "pass"]
    timeout_check = by_name(outcome.report)["acceptance[4]:command"]
    assert "timed out" in timeout_check.message
    net = by_name(outcome.report)["acceptance[5]:command"]
    assert net.evidence["network"] is False and "not permitted" in net.evidence["note"]
    assert [r.network for r in executor.requests if r.command == "true"] == [False]


async def test_diff_evidence(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "# x\n")
    st = step(
        "documentation",
        scope_contract=scope(target_paths=["calc.py", "README.md"]),
        acceptance=[
            {"type": "diff", "must_change": ["calc.py"]},
            {"type": "diff", "must_change": ["docs/**"]},
            {"type": "diff", "must_not_change": ["*.py"]},
            {"type": "diff", "max_changed_files": 0},
        ],
    )
    outcome, *_ = await run(sessionmaker, repo, st, kind="documentation")
    names = by_name(outcome.report)
    assert names["acceptance[0]:diff"].status == "pass"
    assert names["acceptance[1]:diff"].status == "fail" and names["acceptance[1]:diff"].evidence["missing"] == ["docs/**"]
    assert names["acceptance[2]:diff"].status == "fail" and names["acceptance[2]:diff"].evidence["touched_forbidden"] == ["calc.py"]
    assert names["acceptance[3]:diff"].status == "fail"
    empty = init_repo(tmp_path / "empty")
    outcome2, *_ = await run(sessionmaker, empty, step("documentation", acceptance=[{"type": "diff"}]), kind="documentation")
    assert by_name(outcome2.report)["acceptance[0]:diff"].message == "the change is empty"


# ------------------------------------------------------------------------------------------- schema / artifact
async def test_schema_evidence(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"README.md": "x\n"})
    write(repo, "conf/app.json", '{"name": "api", "port": 8080}')
    write(repo, "conf/app.yaml", "name: api\nport: '8080'\n")
    write(repo, "conf/broken.toml", "name = \n")
    schema = {"type": "object", "required": ["name", "port"], "properties": {"port": {"type": "integer"}}}
    st = step(
        "documentation",
        scope_contract=scope(allowed_new_paths=["conf/**"]),
        acceptance=[
            {"type": "schema", "path": "conf/app.json", "json_schema": schema},
            {"type": "schema", "path": "conf/app.yaml", "format": "yaml", "json_schema": schema},
            {"type": "schema", "path": "conf/broken.toml", "format": "toml"},
            {"type": "schema", "path": "conf/missing.json"},
        ],
    )
    outcome, *_ = await run(sessionmaker, repo, st, kind="documentation")
    names = by_name(outcome.report)
    assert names["acceptance[0]:schema"].status == "pass"
    assert names["acceptance[1]:schema"].status == "fail" and "$.port" in names["acceptance[1]:schema"].evidence["errors"][0]
    assert names["acceptance[2]:schema"].status == "fail" and "invalid TOML" in names["acceptance[2]:schema"].message
    assert names["acceptance[3]:schema"].status == "fail"


async def test_artifact_evidence_from_db_and_custom_lookup(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    job_id, step_id = await make_job_step(sessionmaker, kind="image")
    async with sessionmaker() as s:
        s.add(Artifact(job_id=job_id, step_id=step_id, kind="image", name="shot-1.png", path="/a/1.png", size_bytes=2048))
        s.add(Artifact(job_id=job_id, step_id=step_id, kind="image", name="shot-2.png", path="/a/2.png", size_bytes=10))
        s.add(Artifact(job_id=job_id, step_id=step_id, kind="video", name="clip.mp4", path="/a/c.mp4", size_bytes=99999))
        await s.commit()
    st = step(
        "image",
        acceptance=[
            {"type": "artifact", "kind": "image", "name_glob": "shot-*.png", "min_count": 1, "min_size_bytes": 1000},
            {"type": "artifact", "kind": "image", "name_glob": "shot-*.png", "min_count": 2, "min_size_bytes": 1000},
            {"type": "artifact", "kind": "video", "name_glob": "*.mp4"},
        ],
    )
    v = make_verifier(sessionmaker)
    outcome = await v.run(st, handle(repo, job_id), job_id=job_id, step_id=step_id)
    names = by_name(outcome.report)
    assert [names[f"acceptance[{i}]:artifact"].status for i in range(3)] == ["pass", "fail", "pass"]
    assert names["acceptance[1]:artifact"].evidence["too_small"] == ["shot-2.png"]
    seen: list[tuple[Any, ...]] = []

    async def lookup(j: Any, s_: Any, kind: str) -> list[ArtifactRecord]:
        seen.append((j, s_, kind))
        return [ArtifactRecord(name="report.pdf", kind=kind, size_bytes=5)]

    custom = await v.run(
        step("image", acceptance=[{"type": "artifact", "kind": "document", "name_glob": "*.pdf"}]),
        handle(repo, job_id),
        job_id=job_id,
        step_id=step_id,
        artifacts=lookup,
    )
    assert custom.report.passed and seen == [(job_id, step_id, "document")]


# ------------------------------------------------------------------------------------------- side effects / workspace integrity
async def test_side_effects_of_verifier_commands_are_reverted(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "# verified content\n")
    st = step(
        "documentation",
        scope_contract=scope(target_paths=["calc.py"]),
        acceptance=[
            {
                "type": "command",
                "command": "echo '# rewritten by a formatter' >> calc.py && echo junk > junk.txt && rm README.md && git commit -qam sneaky || true",
            },
            {"type": "command", "command": "grep -q 'rewritten' calc.py"},  # commands see each other's effects
        ],
    )
    head = git(repo, "rev-parse", "HEAD").strip()
    outcome, *_ = await run(sessionmaker, repo, st, kind="documentation")
    names = by_name(outcome.report)
    assert names["acceptance[1]:command"].status == "pass"
    integrity = names["workspace_integrity"]
    assert integrity.status == "pass" and integrity.blocking is False and integrity.evidence["reverted"]
    assert (repo / "calc.py").read_text() == CALC + "# verified content\n"
    assert not (repo / "junk.txt").exists() and (repo / "README.md").exists()
    assert git(repo, "rev-parse", "HEAD").strip() == head
    assert outcome.report.changed_files == ["calc.py"]


async def test_concurrent_runs_on_one_workspace_are_serialised(sessionmaker: Any, tmp_path: Path) -> None:
    import asyncio

    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "# x\n")
    executor = WorkspaceShellExecutor(delay=0.05)
    v = make_verifier(sessionmaker, executor)
    job_id, step_id = await make_job_step(sessionmaker, kind="documentation")
    ws = handle(repo, job_id)
    st = step("documentation", scope_contract=scope(target_paths=["calc.py"]), acceptance=[{"type": "command", "command": "true"}])
    outcomes = await asyncio.gather(*(v.run(st, ws, job_id=job_id, step_id=step_id) for _ in range(3)))
    assert all(o.report.passed for o in outcomes)
    assert executor.max_active == 1
    assert len({o.run_id for o in outcomes}) == 3
    assert len(v._locks) == 0


async def test_verdict_depends_only_on_generic_checks_and_evidence(sessionmaker: Any, tmp_path: Path) -> None:
    """The same change passes or fails purely by its acceptance evidence – no built-in project rules."""
    repo = py_repo(tmp_path)
    write(repo, "calc.py", CALC + "SPECIAL = 1\n")
    base_scope = scope(target_paths=["calc.py"])
    lenient = step(
        "documentation", scope_contract=base_scope, acceptance=[{"type": "presence", "path_glob": "calc.py", "pattern": "SPECIAL"}]
    )
    strict = step(
        "documentation", scope_contract=base_scope, acceptance=[{"type": "absence", "path_glob": "calc.py", "pattern": "SPECIAL"}]
    )
    a, *_ = await run(sessionmaker, repo, lenient, kind="documentation")
    b, *_ = await run(sessionmaker, repo, strict, kind="documentation")
    assert a.report.passed and not b.report.passed
    generic = {c.name for c in a.report.checks if not c.name.startswith("acceptance[")}
    assert generic == {c.name for c in b.report.checks if not c.name.startswith("acceptance[")}
    source = "\n".join(p.read_text(encoding="utf-8") for p in Path("hermclaw/verifier").glob("*.py")).lower()
    for marker in ("hermclaw/", "calc.py", "special_case", "step.key ==", "step_key ==", "repository_key =="):
        assert marker not in source, marker


async def test_git_reader_fallback_without_local_repository(sessionmaker: Any, tmp_path: Path) -> None:
    """Added lines come from the GitReader diff when the orchestrator copy has no readable base tree."""
    repo = py_repo(tmp_path)
    token = "gl" + "pat-" + "AbCdEfGhIjKlMnOpQrSt"
    write(repo, "calc.py", CALC + f'TOKEN = "{token}"\n')
    write(repo, "extra.py", "<<<<<<< HEAD\n")
    reader = GitCliReader()
    job_id, step_id = await make_job_step(sessionmaker, kind="documentation")
    ws = handle(repo, job_id, base_sha="not-a-sha")
    real = ws.base_sha

    class Reader(GitCliReader):
        async def diff(self, workspace: Any, paths: Any = None, *, max_bytes: int = 200_000) -> str:
            return git(repo, "diff", "HEAD", "--", *(paths or []))[:max_bytes]

        async def changed_files(self, workspace: Any) -> list[str]:
            return ["calc.py", "extra.py"]

    del reader, real
    v = Verifier(sessionmaker, make_verifier(sessionmaker).policies, WorkspaceShellExecutor(), Reader())
    outcome = await v.run(
        step("documentation", scope_contract=scope(target_paths=["calc.py"], allowed_new_paths=["*.py"])),
        ws,
        job_id=job_id,
        step_id=step_id,
    )
    names = by_name(outcome.report)
    assert names["secrets:calc.py"].status == "fail"
    assert names["conflicts:extra.py"].status == "fail"
    assert names["scope"].evidence["operations_from"] == "git status"


# ------------------------------------------------------------------------------------------- framework-aware test parsing
async def test_framework_aware_test_results(sessionmaker: Any, tmp_path: Path) -> None:
    repo = init_repo(
        tmp_path / "repo",
        {"go.mod": "module example.com/demo\n\ngo 1.21\n", "calc.go": "package demo\n\nfunc Add(a, b int) int { return a + b }\n"},
    )
    write(
        repo,
        "calc_test.go",
        'package demo\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n\tif Add(1, 2) != 3 {\n\t\tt.Fatal("bad")\n\t}\n}\n',
    )
    jest_like = "printf 'PASS src/a.test.js\\nTests:       4 passed, 4 total\\n'"
    st = step(
        scope_contract=scope(allowed_new_paths=["*_test.go"]),
        acceptance=[
            {"type": "test", "command": "go test -v ./...", "framework": "go", "min_passed": 1},
            {"type": "test", "command": jest_like, "framework": "npm", "min_passed": 4},
            {"type": "test", "command": "printf 'Tests: 1 failed, 3 passed, 4 total\\n'; exit 1", "framework": "npm"},
        ],
    )
    outcome, *_ = await run(sessionmaker, repo, st)
    names = by_name(outcome.report)
    go = names["acceptance[0]:test"]
    assert go.status == "pass" and go.evidence["detected_framework"] == "go" and go.evidence["passed"] == 1
    jest = names["acceptance[1]:test"]
    assert jest.status == "pass" and jest.evidence["detected_framework"] == "jest" and jest.evidence["passed"] == 4
    failing = names["acceptance[2]:test"]
    assert failing.status == "fail" and failing.evidence["failed"] == 1
    assert names["test_evidence"].status == "pass"


async def test_staged_rename_is_judged_as_delete_and_create(sessionmaker: Any, tmp_path: Path) -> None:
    repo = py_repo(tmp_path)
    git(repo, "mv", "calc.py", "arith.py")
    rename_only_target = step("documentation", scope_contract=scope(target_paths=["calc.py"], allowed_new_paths=["arith.py"]))
    outcome, *_ = await run(sessionmaker, repo, rename_only_target, kind="documentation")
    names = by_name(outcome.report)
    assert names["scope"].evidence["changes"] == [{"path": "arith.py", "operation": "create"}, {"path": "calc.py", "operation": "delete"}]
    assert names["scope"].status == "fail"  # delete not in allowed_operations
    assert outcome.report.changed_files == ["arith.py", "calc.py"]
    allowed = step(
        "documentation",
        scope_contract=scope(target_paths=["calc.py"], allowed_new_paths=["arith.py"], allowed_operations=["create", "delete"]),
    )
    outcome2, *_ = await run(sessionmaker, repo, allowed, kind="documentation")
    assert by_name(outcome2.report)["scope"].status == "pass" and by_name(outcome2.report)["deleted_files"].status == "pass"


async def test_load_step_from_rows_uses_newest_active_scope(sessionmaker: Any, tmp_path: Path) -> None:
    from hermclaw.persistence.models import ScopeContractRow, Step
    from hermclaw.verifier import load_step

    job_id, step_id = await make_job_step(sessionmaker)
    async with sessionmaker() as s:
        row = await s.get(Step, step_id)
        assert row is not None
        row.acceptance = [{"type": "absence", "path_glob": "legacy/"}, {"type": "diff", "must_change": ["src/**"]}]
        row.network = True
        s.add(ScopeContractRow(job_id=job_id, step_id=step_id, version=1, status="superseded", contract={"target_paths": ["old.py"]}))
        s.add(
            ScopeContractRow(
                job_id=job_id, step_id=step_id, version=2, status="active", contract={"version": 2, "target_paths": ["src/a.py"]}
            )
        )
        await s.commit()
    async with sessionmaker() as s:
        st = await load_step(s, step_id)
    assert st.key == "S001" and st.kind == "implement" and st.network is True
    assert [a.type for a in st.acceptance] == ["absence", "diff"]
    assert st.scope is not None and st.scope.version == 2 and st.scope.target_paths == ["src/a.py"]
    async with sessionmaker() as s:
        with pytest.raises(LookupError):
            await load_step(s, job_id)


GLOBAL_TS = Path("/opt/node22/lib/node_modules/typescript")


@pytest.mark.skipif(not GLOBAL_TS.exists(), reason="no TypeScript installation available to link into node_modules")
async def test_compile_typescript_with_tsc(sessionmaker: Any, tmp_path: Path) -> None:
    import os

    tsconfig = '{"compilerOptions": {"strict": true, "noEmit": true}, "include": ["src"]}\n'
    repo = init_repo(
        tmp_path / "repo", {"tsconfig.json": tsconfig, "src/a.ts": "export const a: number = 1;\n", ".gitignore": "node_modules/\n"}
    )
    (repo / "node_modules/.bin").mkdir(parents=True)
    os.symlink(GLOBAL_TS.resolve(), repo / "node_modules/typescript")
    os.symlink("../typescript/bin/tsc", repo / "node_modules/.bin/tsc")
    write(repo, "src/a.ts", 'export const a: number = "not a number";\n')
    st = step("documentation", scope_contract=scope(target_paths=["src/a.ts"]))
    outcome, *_ = await run(sessionmaker, repo, st, kind="documentation")
    check = by_name(outcome.report)["compile:typescript"]
    assert check.status == "fail" and "TS2322" in check.evidence["output"], check.message
    write(repo, "src/a.ts", "export const a: number = 2;\n")
    outcome2, *_ = await run(sessionmaker, repo, st, kind="documentation")
    assert by_name(outcome2.report)["compile:typescript"].status == "pass"
    assert by_name(outcome2.report)["syntax:src/a.ts"].status == "skip"
