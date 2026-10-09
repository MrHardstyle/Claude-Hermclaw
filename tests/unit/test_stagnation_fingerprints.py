"""P20 20.1/20.2 fingerprints: normalisation, error signatures, failing tests, action/diff/file fingerprints."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hermclaw.core.redaction import REDACTED
from hermclaw.stagnation.fingerprints import (
    EMPTY_DIFF_HASH,
    action_fingerprint,
    action_label,
    changed_files_fingerprint,
    decision_label,
    diff_hash,
    error_signature,
    extract_failing_tests,
    failing_tests_fingerprint,
    key_error_lines,
    normalise_args,
    normalise_text,
    safe_label,
    sequence_label,
    tool_sequence,
)

PYTEST_RUN = """\x1b[1m============================= test session starts ==============================\x1b[0m
platform linux -- Python 3.12.3, pytest-8.3.2, pluggy-1.5.0
rootdir: /tmp/pytest-of-runner/pytest-{n}/test_run0
Using --randomly-seed={seed}
collected 4 items

tests/test_app.py ..F.                                                   [100%]

=================================== FAILURES ===================================
__________________________________ test_add ___________________________________

    def test_add():
>       assert add(1, 2) == 4
E       assert 3 == 4
E        +  where 3 = add(1, 2)

tests/test_app.py:{line}: AssertionError
------------------------------ Captured log call -------------------------------
{ts} INFO worker pid={pid} object <Foo at {addr}> in /tmp/tmp{tmp}/x
=========================== short test summary info ============================
FAILED tests/test_app.py::test_add - assert 3 == 4
========================= 1 failed, 3 passed in {dur}s =========================
"""


def render(n: int, seed: int, line: int, ts: str, pid: int, addr: str, tmp: str, dur: str) -> str:
    return PYTEST_RUN.format(n=n, seed=seed, line=line, ts=ts, pid=pid, addr=addr, tmp=tmp, dur=dur)


# ------------------------------------------------------------------------------------------------ normalisation
def test_normalise_strips_volatile_noise() -> None:
    text = (
        "\x1b[31m2026-10-09T12:00:01.123Z ERROR at 0x7f3a2b4c5d60 took 0.53s (12 ms)\x1b[0m\r\n"
        '  File "/tmp/pytest-of-u/pytest-7/test0/app.py", line 42, in add\n'
        "app.c:10:5: error: bad\n"
        "Memory: 6.00 MB  seed=991 pid 4242 id 3f2a1b9c-1d2e-4f50-8a6b-0c1d2e3f4a5b sha deadbeefcafe0123\n"
        "progress [ 50%]\n\n\n"
    )
    out = normalise_text(text)
    for volatile in (
        "2026-10-09",
        "0x7f3a",
        "0.53",
        "12 ms",
        "pytest-7",
        "line 42",
        ":10:5",
        "6.00",
        "991",
        "4242",
        "3f2a1b9c",
        "deadbeef",
        "50%]",
    ):
        assert volatile not in out, volatile
    assert "\x1b" not in out and "\r" not in out and "\n\n" not in out
    assert "error: bad" in out and "File" in out


def test_line_numbers_are_kept_when_requested() -> None:
    assert "line 42" in normalise_text("File x.py, line 42", strip_line_numbers=False)
    assert "app.c:10:5" in normalise_text("app.c:10:5: error", strip_line_numbers=False)
    assert "line <n>" in normalise_text("File x.py, line 42")


def test_meaningful_numbers_survive_normalisation() -> None:
    # assertion values distinguish errors; only volatile numbers are removed
    assert "assert 3 == 4" in normalise_text("E   assert 3 == 4")
    assert (
        error_signature("run_test", "TESTS_FAILED", "E assert 3 == 4").digest
        != error_signature("run_test", "TESTS_FAILED", "E assert 5 == 4").digest
    )


@settings(max_examples=60, deadline=None)
@given(
    n=st.integers(0, 999),
    seed=st.integers(0, 2**31),
    line=st.integers(1, 5000),
    pid=st.integers(1, 99999),
    addr=st.text("0123456789abcdef", min_size=8, max_size=12).map(lambda s: "0x" + s),
    tmp=st.text("abcdefghijklmnopqrstuvwxyz0123456789_", min_size=6, max_size=10),
    dur=st.floats(0.01, 999, allow_nan=False).map(lambda f: f"{f:.2f}"),
    ts=st.datetimes().map(lambda d: d.isoformat(timespec="milliseconds")),
)
def test_two_runs_differing_only_in_noise_share_a_signature(
    n: int, seed: int, line: int, pid: int, addr: str, tmp: str, dur: str, ts: str
) -> None:
    base = render(1, 42, 7, "2026-01-01T00:00:00.000", 1, "0x7fffdeadbeef", "abcdef12", "0.12")
    other = render(n, seed, line, ts, pid, addr, tmp, dur)
    a = error_signature("run_test", "TESTS_FAILED", base)
    b = error_signature("run_test", "TESTS_FAILED", other)
    assert a.digest == b.digest and a.signature == b.signature
    assert extract_failing_tests(base) == extract_failing_tests(other) == ("tests/test_app.py::test_add",)


def test_signature_distinguishes_different_errors_tools_and_codes() -> None:
    base = render(1, 1, 7, "2026-01-01T00:00:00", 1, "0x7fffdeadbeef", "abcdef12", "0.1")
    other = base.replace("assert 3 == 4", "assert 3 == 5")
    assert error_signature("run_test", "TESTS_FAILED", base).digest != error_signature("run_test", "TESTS_FAILED", other).digest
    assert error_signature("run_test", "TESTS_FAILED", base).digest != error_signature("run_command", "TESTS_FAILED", base).digest
    assert error_signature("run_test", "TESTS_FAILED", base).digest != error_signature("run_test", "TEST_ERROR", base).digest


def test_signature_ignores_traceback_context_but_not_the_exception() -> None:
    tb1 = "Traceback (most recent call last):\n  File \"app.py\", line 3, in <module>\n    x = compute(1)\nNameError: name 'compute' is not defined"
    tb2 = "Traceback (most recent call last):\n  File \"app.py\", line 9, in <module>\n    y = 2 + compute(3)\nNameError: name 'compute' is not defined"
    tb3 = 'Traceback (most recent call last):\n  File "app.py", line 9, in <module>\n    y = 2 + compute(3)\nTypeError: bad operand'
    s1, s2, s3 = (error_signature("run_command", "COMMAND_FAILED", t) for t in (tb1, tb2, tb3))
    assert s1.digest == s2.digest != s3.digest
    assert s1.signature.startswith("NameError") and s1.error_code == "COMMAND_FAILED" and s1.tool == "run_command"


def test_signature_without_key_lines_uses_tail_and_default_code() -> None:
    sig = error_signature("run_command", None, "some\nplain\noutput")
    assert sig.error_code == "ERROR" and sig.signature == "some"
    assert error_signature("run_command", "", "").signature == "ERROR"
    assert key_error_lines(normalise_text("ok\nValueError: x\nValueError: x\nE   assert 1")) == ["ValueError: x", "E assert 1"]


def test_signatures_and_labels_are_redacted() -> None:
    secret = "ghp_" + "A" * 36
    sig = error_signature("run_command", "COMMAND_FAILED", f"RuntimeError: auth failed with token {secret}")
    assert secret not in sig.signature and REDACTED in sig.signature
    assert secret not in action_label("run_command", {"command": f"curl -H 'Authorization: token {secret}' x"})
    assert len(safe_label("x" * 1000)) <= 120


# ------------------------------------------------------------------------------------------------ failing tests
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "FAILED tests/a.py::test_x - assert 1\nERROR tests/b.py - ImportError: y\nFAILED (failures=1)",
            ("tests/a.py::test_x", "tests/b.py"),
        ),
        ("tests/a.py::test_y FAILED                     [ 50%]\ntests/a.py::test_z PASSED", ("tests/a.py::test_y",)),
        ("FAIL: test_one (pkg.mod.Case.test_one)\nERROR: test_two (pkg.mod.Case)\n", ("pkg.mod.Case.test_one", "pkg.mod.Case.test_two")),
        (
            "=== RUN   TestA\n--- FAIL: TestA (0.00s)\n    --- FAIL: TestA/sub (0.01s)\nFAIL\nFAIL\tgithub.com/x/y\t0.003s\n",
            ("TestA", "TestA/sub", "github.com/x/y"),
        ),
        ("test tests::it_works ... FAILED\ntest tests::other ... ok\n", ("tests::it_works",)),
        (" FAIL  src/a.test.js (5.2 s)\n  ● Math › adds\n    ✕ adds (3 ms)\n    ✓ subs (1 ms)\n", ("Math › adds", "adds", "src/a.test.js")),
        ("  2 passing\n  1 failing\n\n  1) Array #indexOf():\n     AssertionError: expected -1\n", ("Array #indexOf()",)),
        ("There was 1 failure:\n\n1) Tests\\FooTest::testBar\nFailed asserting that false is true.\n", ("Tests\\FooTest::testBar",)),
        ("all good\n3 passed in 0.1s", ()),
        ("1) not in a failure section\n", ()),
    ],
)
def test_extract_failing_tests_per_runner(output: str, expected: tuple[str, ...]) -> None:
    assert extract_failing_tests(output) == tuple(sorted(expected))


def test_extract_failing_tests_is_bounded_and_deduplicated() -> None:
    out = "\n".join(f"FAILED tests/t.py::test_{i} - x" for i in range(500)) + "\nFAILED tests/t.py::test_1 - again"
    ids = extract_failing_tests(out)
    assert len(ids) == 200 and len(set(ids)) == 200 and ids == tuple(sorted(ids))


def test_failing_set_fingerprint_is_order_insensitive() -> None:
    assert failing_tests_fingerprint(["b", "a", "a"]) == failing_tests_fingerprint(["a", "b"])
    assert failing_tests_fingerprint(["a"]) != failing_tests_fingerprint(["a", "b"])
    assert failing_tests_fingerprint([]) is None


# ------------------------------------------------------------------------------------------------------ actions
def test_action_fingerprint_normalises_paths_whitespace_and_order() -> None:
    a = action_fingerprint("read_file", {"path": "./src//app.py"})
    assert a == action_fingerprint("read_file", {"path": "src/app.py"}) and a.startswith("read_file:")
    assert action_fingerprint("list_files", {"path": "src/"}) == action_fingerprint("list_files", {"path": "src"})
    assert action_fingerprint("list_files", {"path": ""}) == action_fingerprint("list_files", {"path": "./"})
    assert action_fingerprint("run_test", {"command": "pytest  -q\ttests"}) == action_fingerprint(
        "run_test", {"command": "pytest -q tests"}
    )
    assert action_fingerprint("git_diff", {"paths": ["b.py", "a.py"]}) == action_fingerprint("git_diff", {"paths": ["a.py", "b.py"]})
    assert action_fingerprint("x", {"b": 1, "a": {"d": 2, "c": [1, 2]}}) == action_fingerprint("x", {"a": {"c": [1, 2], "d": 2}, "b": 1})
    # distinct actions stay distinct
    assert action_fingerprint("read_file", {"path": "a.py"}) != action_fingerprint("read_file", {"path": "b.py"})
    assert action_fingerprint("read_file", {"path": "a.py"}) != action_fingerprint("read_range", {"path": "a.py"})
    assert action_fingerprint("write_file", {"path": "a", "content": "x"}) != action_fingerprint(
        "write_file", {"path": "a", "content": "y"}
    )
    assert action_fingerprint("x", {"items": [1, 2]}) != action_fingerprint("x", {"items": [2, 1]})  # non-path lists keep order
    # traversal/absolute paths are kept verbatim instead of raising
    assert action_fingerprint("read_file", {"path": "../etc/passwd"}) != action_fingerprint("read_file", {"path": "etc/passwd"})
    assert normalise_args({"path": "/abs/x", "flag": True, "n": None, "obj": object})["path"] == "/abs/x"


def test_action_label_never_contains_content() -> None:
    assert action_label("write_file", {"path": "a.py", "content": "SECRET CONTENT"}) == "write_file a.py"
    assert action_label("git_diff", {"paths": ["b", "a"]}) == "git_diff a, b"
    assert action_label("checkpoint", {}) == "checkpoint"


def test_tool_sequence_and_decision_label() -> None:
    assert tool_sequence(["a:1", "b:2"], 3) is None
    assert tool_sequence(["a:1", "b:2", "c:3", "d:4"], 3) == "b:2>c:3>d:4"
    assert tool_sequence(["a:1"], 1) is None
    assert sequence_label(["read_file:1", "run_test:2", "replace_text:3"], 3) == "read_file>run_test>replace_text"
    assert decision_label("  Fix Import!! ") == "fix-import"
    assert decision_label("fix_import") == "fix-import"
    assert decision_label("   ") is None and decision_label("!!") is None
    assert "abcdefghij" not in (decision_label("use ghp_" + "Abcdefghij" * 4) or "")


# ---------------------------------------------------------------------------------------------- diffs and files
def test_diff_hash_ignores_blob_ids_offsets_and_trailing_whitespace() -> None:
    d1 = "diff --git a/x b/x\nindex 1234567..89abcde 100644\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@ def f():\n-a\n+b   \n"
    d2 = "diff --git a/x b/x\nindex fedcba9..7654321 100644\n--- a/x\n+++ b/x\n@@ -10,3 +12,3 @@ def f():\n-a\n+b\n"
    d3 = d2.replace("+b", "+c")
    assert diff_hash(d1) == diff_hash(d2) != diff_hash(d3)
    assert diff_hash("") == diff_hash("\n\n") == EMPTY_DIFF_HASH


def test_changed_files_fingerprint() -> None:
    assert changed_files_fingerprint(["./b.py", "a.py", "a.py"]) == changed_files_fingerprint(["a.py", "b.py"])
    assert changed_files_fingerprint(["a.py"]) != changed_files_fingerprint(["b.py"])
    assert changed_files_fingerprint(["", "  "]) is None


def test_normalisation_is_linear_on_large_hostile_output() -> None:
    import time

    hostile = ("a.b" + ":1" * 50 + " 0x" + "f" * 40 + " /tmp/" + "x" * 200 + " 1.5s " + "\x1b[" + "1;" * 100 + "\n") * 2000
    start = time.perf_counter()
    sig = error_signature("run_command", "COMMAND_FAILED", hostile)
    extract_failing_tests(hostile)
    assert time.perf_counter() - start < 5.0 and sig.digest
