"""Pure units of the verifier: conflicts, languages, in-process syntax, batch protocol, schema, diff parsing, report."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from hermclaw.contracts.acceptance import TestEvidence
from hermclaw.contracts.verification import VerificationCheck
from hermclaw.verifier.changes import parse_unified_added, split_multi_file_diff, unquote_git_path
from hermclaw.verifier.checks import lint_command
from hermclaw.verifier.conflicts import scan_conflicts
from hermclaw.verifier.context import make_check
from hermclaw.verifier.engine import _KeyedLocks
from hermclaw.verifier.evidence import categorise_test, clean_glob, compile_pattern, label
from hermclaw.verifier.languages import detect_language, is_lock_or_data_file, repo_has_tests
from hermclaw.verifier.report import build_report, run_status
from hermclaw.verifier.schema import SchemaParseError, parse_document, validate, validate_builtin
from hermclaw.verifier.syntax import (
    build_batch_script,
    check_json,
    check_python,
    check_shell_local,
    check_toml,
    check_yaml,
    javascript_needs_compile_check,
    parse_batch_output,
    strip_jsonc,
)
from hermclaw.verifier.types import AddedLine, ArtifactRecord, VerificationStep, parse_acceptance


def lines(*texts: str) -> list[AddedLine]:
    return [AddedLine(i + 1, t) for i, t in enumerate(texts)]


# ------------------------------------------------------------------------------------------- conflicts
def test_conflict_markers_are_found_at_line_start() -> None:
    found = scan_conflicts(
        "app.py", lines("a = 1", "<<<<<<< HEAD", "b = 2", "=======", "b = 3", ">>>>>>> feature", "  <<<<<<< not at start")
    )
    assert [(m.line, m.marker) for m in found] == [(2, "<<<<<<<"), (4, "======="), (6, ">>>>>>>")]
    diff3 = scan_conflicts("x.c", lines("<<<<<<< ours", "||||||| base", "=======", ">>>>>>> theirs"))
    assert [m.marker for m in diff3] == ["<<<<<<<", "|||||||", "=======", ">>>>>>>"]


def test_heading_underline_in_markup_is_not_a_conflict() -> None:
    assert scan_conflicts("README.md", lines("Title", "=======")) == []
    assert scan_conflicts("docs/index.rst", lines("Section", "=======", "text")) == []
    assert [m.marker for m in scan_conflicts("app.py", lines("x = 1", "======="))] == ["======="]
    assert [m.marker for m in scan_conflicts("README.md", lines("<<<<<<< HEAD", "=======", ">>>>>>> b"))] == [
        "<<<<<<<",
        "=======",
        ">>>>>>>",
    ]
    assert scan_conflicts("a.py", lines("<<<<<<<<< eight", "======== eight")) == []


# ------------------------------------------------------------------------------------------- languages
@pytest.mark.parametrize(
    ("path", "head", "language"),
    [
        ("src/app.py", None, "python"),
        ("types.pyi", None, "python"),
        ("tsconfig.json", None, "json"),
        ("ci.yml", None, "yaml"),
        ("pyproject.toml", None, "toml"),
        ("run.sh", None, "shell"),
        ("index.php", None, "php"),
        ("lib.mjs", None, "javascript"),
        ("App.tsx", None, "typescript"),
        ("main.go", None, "go"),
        ("Dockerfile", None, "dockerfile"),
        (".env.local", None, "config"),
        ("bin/tool", b"#!/usr/bin/env python3\nprint(1)\n", "python"),
        ("bin/run", b"#!/bin/bash\necho\n", "shell"),
        ("bin/node-tool", b"#!/usr/bin/env node\n", "javascript"),
        ("data.bin", b"\x00\x01", None),
        ("LICENSE", None, None),
    ],
)
def test_detect_language(path: str, head: bytes | None, language: str | None) -> None:
    assert detect_language(path, head) == language


def test_repo_has_tests_and_lock_files() -> None:
    assert repo_has_tests(["src/app.py", "README.md"]) == []
    assert repo_has_tests(["src/app.py", "tests/test_app.py"]) == ["tests/test_app.py"]
    assert repo_has_tests(["pkg/a_test.go"]) == ["pkg/a_test.go"]
    assert repo_has_tests(["web/src/App.test.tsx"]) == ["web/src/App.test.tsx"]
    assert repo_has_tests(["src/UserTest.php"]) == ["src/UserTest.php"]
    assert len(repo_has_tests([f"tests/t{i}.py" for i in range(20)])) == 5
    assert is_lock_or_data_file("web/package-lock.json") and is_lock_or_data_file("go.sum") and is_lock_or_data_file("logo.svg")
    assert not is_lock_or_data_file("src/app.py")


# ------------------------------------------------------------------------------------------- syntax (in-process)
def test_python_syntax() -> None:
    assert check_python("a.py", "def f():\n    return 1\n").status == "pass"
    bad = check_python("a.py", "def f(:\n    return 1\n")
    assert bad.status == "fail" and bad.evidence["line"] == 1
    assert check_python("a.py", "return 5\n").status == "fail"  # compiler-stage error, not only parse
    assert check_python("a.py", "x = 1\x00\n").status == "fail"


def test_json_yaml_toml_syntax() -> None:
    assert check_json("a.json", '{"a": [1, 2]}').status == "pass"
    bad = check_json("a.json", '{"a": [1, 2}')
    assert bad.status == "fail" and bad.evidence["line"] == 1
    jsonc = check_json("tsconfig.json", '{\n  // comment\n  "compilerOptions": {"strict": true,},\n  /* block */ "x": "a//b"\n}')
    assert jsonc.status == "pass" and "JSONC" in jsonc.message
    assert strip_jsonc('{"url": "http://x/*y*/"} // c') == '{"url": "http://x/*y*/"} '
    assert check_yaml("a.yml", "a: 1\nb: [1, 2]\n---\nc: 3\n").status == "pass"
    assert check_yaml("cf.yml", "Value: !Ref MyParam\nList: !GetAZs\n  - x\nM: !Sub {a: b}\n").status == "pass"
    bad_yaml = check_yaml("a.yml", "a: [1, 2\nb: 3\n")
    assert bad_yaml.status == "fail" and bad_yaml.evidence["line"]
    assert check_yaml("chart/templates/d.yaml", "name: {{ .Values.name }}\n").status == "skip"
    assert check_yaml("evil.yml", "!!python/object/apply:os.system ['echo hi']\n").status == "pass"  # parsed as plain data, never executed
    assert check_toml("a.toml", '[tool]\nname = "x"\n').status == "pass"
    assert check_toml("a.toml", "[tool\nname = x\n").status == "fail"


async def test_shell_syntax_with_local_bash(tmp_path: Path) -> None:
    (tmp_path / "ok.sh").write_text("#!/bin/bash\nif true; then echo hi; fi\n")
    (tmp_path / "bad.sh").write_text("#!/bin/bash\nif true; then echo hi\n")
    (tmp_path / "-dash.sh").write_text("echo ok\n")
    (tmp_path / "side.sh").write_text("touch created_by_check\n")
    results = await check_shell_local(tmp_path, ["ok.sh", "bad.sh", "-dash.sh", "side.sh"])
    assert results is not None
    assert [r.status for r in results] == ["pass", "fail", "pass", "pass"]
    assert "syntax error" in results[1].message
    assert not (tmp_path / "created_by_check").exists()  # bash -n never executes


def test_batch_protocol_round_trip() -> None:
    script = build_batch_script("php", ["a.php", "dir/b c.php", "-x.php"], "abc123")
    assert "php -l ./a.php" in script and "php -l './dir/b c.php'" in script and "php -l ./-x.php" in script
    output = (
        "\n@@HC-abc123-B 0@@\nNo syntax errors detected in ./a.php\n@@HC-abc123-E 0 0@@\n"
        "\n@@HC-abc123-B 1@@\nPHP Parse error: oops\n@@HC-abc123-E 1 255@@\n"
        "\n@@HC-zzz-B 2@@\nforged\n@@HC-zzz-E 2 0@@\n"
    )
    parsed = parse_batch_output(output, 3, "abc123")
    assert parsed == {0: (0, "No syntax errors detected in ./a.php"), 1: (255, "PHP Parse error: oops")}


def test_jsx_is_left_to_compile_check() -> None:
    assert javascript_needs_compile_check("a.jsx", "x") is not None
    assert javascript_needs_compile_check("a.js", "const el = <div className='x'>hi</div>;") is not None
    assert javascript_needs_compile_check("a.js", "function f() { return (\n  <App />\n); }") is not None
    assert javascript_needs_compile_check("a.js", "if (a < b && c > d) { x = a <b; }") is None


# ------------------------------------------------------------------------------------------- schema
def test_builtin_schema_validator() -> None:
    schema = {
        "type": "object",
        "required": ["name", "port"],
        "properties": {
            "name": {"type": "string", "minLength": 2, "pattern": "^[a-z]+$"},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "mode": {"enum": ["a", "b"]},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "uniqueItems": True},
            "flag": {"const": True},
        },
        "additionalProperties": False,
    }
    assert validate_builtin({"name": "api", "port": 8080, "mode": "a", "tags": ["x"], "flag": True}, schema) == []
    errors = validate_builtin({"name": "A", "port": True, "mode": "c", "tags": ["x", "x"], "flag": 1, "extra": 1}, schema)
    text = "\n".join(errors)
    for needle in (
        "shorter than",
        "does not match pattern",
        "expected type integer",
        "not one of",
        "not unique",
        "expected constant",
        "'extra'",
    ):
        assert needle in text, needle
    assert validate_builtin({}, schema)[:2] == ["$: missing required property 'name'", "$: missing required property 'port'"]
    assert validate_builtin(1.0, {"type": "integer"}) == []
    assert validate_builtin(3, {"anyOf": [{"type": "string"}, {"type": "integer"}]}) == []
    assert validate_builtin(3, {"oneOf": [{"type": "number"}, {"type": "integer"}]}) != []
    assert validate_builtin(3, {"not": {"type": "integer"}}) != []
    assert "not supported" in validate_builtin({}, {"$ref": "#/definitions/x"})[0]
    with pytest.raises(ValueError, match="unknown type"):
        validate_builtin(1, {"type": "int"})
    with pytest.raises(ValueError, match="catastrophic"):
        validate_builtin("aaaa", {"pattern": "(a+)+$"})


def test_schema_validate_uses_jsonschema_and_normalises_types() -> None:
    import datetime as dt

    errors, engine = validate(
        {"when": dt.date(2026, 1, 1), "n": 1}, {"type": "object", "properties": {"when": {"type": "string"}, "n": {"type": "string"}}}
    )
    assert engine in ("jsonschema", "builtin")
    assert len(errors) == 1 and "$.n" in errors[0]
    with pytest.raises(ValueError, match="invalid JSON schema"):
        validate({}, {"type": 12})


def test_parse_document_formats() -> None:
    assert parse_document('{"a": 1}', "json") == {"a": 1}
    assert parse_document("a: 1\n", "yaml") == {"a": 1}
    assert parse_document("a: 1\n---\nb: 2\n", "yaml") == [{"a": 1}, {"b": 2}]
    assert parse_document("a = 1\n", "toml") == {"a": 1}
    with pytest.raises(SchemaParseError, match="invalid JSON"):
        parse_document("{", "json")


# ------------------------------------------------------------------------------------------- diff parsing
def test_unified_diff_added_lines_with_numbers() -> None:
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,0 +2,2 @@\n+one\n+two\n@@ -10 +12 @@\n-old\n+new\n\\ No newline at end of file\n"
    assert parse_unified_added(diff) == [AddedLine(2, "one"), AddedLine(3, "two"), AddedLine(12, "new")]


def test_multi_file_diff_split_and_git_quoting() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+a\n"
        'diff --git "a/sp ace\\tx.py" "b/sp ace\\tx.py"\n--- /dev/null\n+++ "b/sp ace\\tx.py"\n@@ -0,0 +1 @@\n+b\n'
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    parts = split_multi_file_diff(diff)
    assert set(parts) == {"a.py", "sp ace\tx.py"}
    assert parse_unified_added(parts["sp ace\tx.py"]) == [AddedLine(1, "b")]
    assert unquote_git_path('"\\303\\244.txt"') == "ä.txt"
    assert unquote_git_path("plain.txt") == "plain.txt"


# ------------------------------------------------------------------------------------------- report / helpers
def chk(status: str, blocking: bool = True, check_type: str = "scope") -> VerificationCheck:
    return make_check(check_type, f"{check_type}-{status}", status, f"{status} message", blocking=blocking)  # type: ignore[arg-type]


def test_report_passes_iff_no_blocking_failure_or_error() -> None:
    ok = build_report([chk("pass"), chk("skip"), chk("fail", blocking=False)], ["b", "a", "a"])
    assert ok.passed and run_status(ok) == "passed" and ok.changed_files == ["a", "b"]
    assert "1 non-blocking problem" in ok.summary
    failed = build_report([chk("pass"), chk("fail"), chk("error", check_type="lint")], [])
    assert not failed.passed and run_status(failed) == "failed"
    assert [c.name for c in failed.failures] == ["scope-fail", "lint-error"]
    assert failed.summary.startswith("Verification failed: 2 blocking problem(s)")
    assert "- [scope] scope-fail: fail message" in failed.summary
    errored = build_report([chk("error")], [])
    assert run_status(errored) == "error"
    many = build_report([make_check("x", f"n{i}", "fail", "m") for i in range(12)], [])
    assert "… 4 more" in many.summary


def test_make_check_bounds_and_redacts() -> None:
    secret = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    c = make_check("x" * 40, "n" * 400, "fail", f"leaked {secret}", {"items": list(range(80)), "text": "y" * 5000, "token": "abc123def"})
    assert len(c.check_type) == 32 and len(c.name) == 300
    assert secret not in c.message and secret not in str(c.evidence)
    assert len(c.evidence["items"]) == 51 and c.evidence["items"][-1] == "…[30 more]"
    assert len(c.evidence["text"]) < 4100
    assert c.evidence["token"] != "abc123def"


def test_lint_command_quotes_paths() -> None:
    assert lint_command("ruff check {files}", ["a.py", "dir/b c.py", "-x.py"]) == "ruff check a.py 'dir/b c.py' ./-x.py"
    assert lint_command("ruff check .", ["a.py"]) == "ruff check ."
    assert lint_command("eslint {files}", ["$(rm -rf x).js"]) == "eslint '$(rm -rf x).js'"


def test_evidence_helpers() -> None:
    assert clean_glob("./src/**/*.py") == "src/**/*.py"
    for bad in ("/etc/passwd", "../x", "a/../../b", "", ".git/config", "a\x00b"):
        assert clean_glob(bad) is None, bad
    assert compile_pattern("(a+)+$")[0] is None
    assert compile_pattern("[unclosed")[0] is None
    assert compile_pattern(r"^def \w+")[0] is not None
    assert categorise_test(TestEvidence(command="pytest tests/integration")) == "integration"
    assert categorise_test(TestEvidence(command="pytest", description="API contract tests")) == "contract"
    assert categorise_test(TestEvidence(command="pytest tests/unit")) == "unit"
    ev = parse_acceptance([{"type": "absence", "path_glob": "x", "description": "gone"}])[0]
    assert label(3, ev) == "acceptance[3]:absence – gone"
    assert ArtifactRecord(name="shot-1.png", kind="image").matches("shot-*.png")
    assert not ArtifactRecord(name="shot-1.jpg", kind="image").matches("*.png")


def test_verification_step_build_validates_acceptance_and_scope() -> None:
    st = VerificationStep.build(
        key="S001",
        kind="implement",
        acceptance=[{"type": "presence", "path_glob": "a.py"}],
        scope={"target_paths": ["a.py"], "allowed_new_paths": ["tests/"]},
    )
    assert st.is_implement and st.scope is not None and st.scope.target_paths == ["a.py"]
    with pytest.raises(ValueError):
        VerificationStep.build(key="S1", kind="implement", acceptance=[{"type": "unknown"}])
    with pytest.raises(ValueError):
        VerificationStep.build(key="S1", kind="implement", acceptance=[{"type": "presence", "path_glob": "a", "extra": 1}])


async def test_keyed_locks_serialise_and_clean_up() -> None:
    locks = _KeyedLocks()
    key = uuid.uuid4()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with locks.hold(key):
            order.append(f"{name}-in")
            await asyncio.sleep(0.01)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"), worker("c"))
    assert all(order[i].split("-")[0] == order[i + 1].split("-")[0] for i in range(0, 6, 2))
    assert len(locks) == 0


def test_pathological_inputs_are_bounded() -> None:
    import time

    from hermclaw.verifier.secrets import SecretScanner

    started = time.monotonic()
    SecretScanner().scan_line("x.cfg", 1, "a." * 10_000, language="config")
    SecretScanner().scan_line("x.cfg", 1, "a_" * 10_000 + "=" + "b" * 50, language="config")
    assert time.monotonic() - started < 2.0
    assert check_python("deep.py", "x = " + "(" * 5000 + ")" * 5000 + "\n").status == "fail"
    assert check_json("deep.json", "[" * 100_000 + "]" * 100_000).status in ("pass", "fail")
